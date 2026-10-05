"""ClickHouse history writer — the append-only event firehose (PROPOSAL §5).

Every validated state update is also written to ClickHouse for history/graphs.
This is the SECONDARY path: it must never hold up state projection (the primary
job). So writes are buffered and flushed in batches (ClickHouse hates per-row
inserts), and a ClickHouse outage just grows a bounded buffer + logs loudly —
the engine keeps projecting to Postgres regardless. Fail loud, degrade only the
non-critical path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

from dida_core import (
    apply_ch_migrations,
    apply_house_day,
    apply_retention,
    ch_client,
    house_timezone,
)
from dida_core.capabilities import Value

log = logging.getLogger("dida.engine.history")

COLUMNS = ["ts", "entity_id", "capability", "adapter", "value_num", "value_str"]
CMD_COLUMNS = ["ts", "entity_id", "capability", "command", "source", "args"]

BUFFER_CAP = 50_000   # hard cap: a CH outage drops oldest rows, never OOMs
BATCH_SIZE = 10_000

# A schema failure becomes a FAULT only once it has failed this many times AND kept
# failing for this long. Both, because `_ensure` is called from the flush loop about
# once a second: a count alone would be spent in five seconds, so a ClickHouse that
# accepts connections before it will accept DDL — normal during a cold start — would
# trip it and roll a deploy back over nothing. The clock is what makes it mean
# "still broken", not "not ready yet".
SCHEMA_FAIL_LIMIT = 5
SCHEMA_FAIL_SECONDS = 120

# The house zone is configuration a person can change at any time; the daily rollup
# follows it at the next check rather than at the next engine restart.
DAY_TZ_CHECK_S = 300

# Capabilities projected to Postgres `current_state` (the live snapshot) but NOT
# appended to the history firehose: ephemeral cursors / opaque blobs with no
# time-series value. The media playhead, the track length and the album-art URL
# would only bloat ClickHouse — the live value in Postgres is all the UI needs.
# Meaningful media history (transport, volume, mute, title/artist/album → a
# "what did I listen to" log) IS kept.
NON_HISTORIZED: frozenset[str] = frozenset(
    {"media_position", "media_duration", "media_art", "media_quality", "source_options", "media_favorites"}
)

# Readings mirrored from a peer DIDA are LIVE here and archived THERE. Writing them
# into this installation's firehose would fork the other house's history into a
# second, partial copy that silently disagrees with the original after any outage.
MIRRORED_ADAPTER = "peer"


class HistoryWriter:
    def __init__(self, pool) -> None:
        self._pool = pool
        self._client = None
        # deque, not list: at cap during a CH outage every enqueue drops the
        # oldest row — list.pop(0) is O(n) on a 50k buffer, popleft() is O(1).
        self._buf: deque[tuple] = deque()
        self._pending: tuple[tuple, ...] = ()
        self._pending_token: str | None = None
        self._flush_lock = asyncio.Lock()
        self._dropped = 0
        self._schema_ready = False
        self._schema_failures = 0
        self._schema_first_failure: float | None = None
        self._day_tz: str | None = None
        self._next_day_tz_check = 0.0

    def stats(self) -> dict:
        """Live buffer health for the observability surface (never blocks)."""
        return {
            "buffered": len(self._buf) + len(self._pending),
            "in_flight": len(self._pending),
            "cap": BUFFER_CAP,
            "dropped": self._dropped,
            "connected": self._client is not None,
            "schema_failures": self._schema_failures,
            "schema_failing_for_s": (
                round(time.monotonic() - self._schema_first_failure)
                if self._schema_first_failure is not None else 0
            ),
            "schema_broken": self.schema_broken,
        }

    async def _ensure(self) -> bool:
        """Lazily (re)connect and, once, own the ClickHouse schema. Returns connected?"""
        if self._client is None:
            try:
                self._client = await ch_client()
            except Exception as exc:
                self._client = None
                log.warning("clickhouse not ready (%s); buffering history, will retry", exc, exc_info=True)
                return False
        if not self._schema_ready:
            # The engine is the single ClickHouse writer, so it owns the schema:
            # apply migrations (rollup tables + MVs), cut the daily rollup at the
            # house's midnight, then sync retention TTLs from Postgres. Idempotent;
            # gated so an outage reconnect won't re-run it.
            try:
                await apply_ch_migrations(self._client)
                tz = (await house_timezone(self._pool)).key
                await apply_house_day(self._client, tz)
                self._day_tz = tz
                self._next_day_tz_check = time.monotonic() + DAY_TZ_CHECK_S
                await apply_retention(self._client, self._pool)
                self._schema_ready = True
                self._schema_failures = 0
                self._schema_first_failure = None
                log.info("clickhouse history schema ready")
            except Exception as exc:
                await self._close_client()
                self._schema_failures += 1
                if self._schema_first_failure is None:
                    self._schema_first_failure = time.monotonic()
                # A CONNECTION failure is transient and buffering through it is
                # correct — that is handled above. Getting HERE means ClickHouse
                # answered and then refused the DDL, which retrying does not fix:
                # bad SQL, a permission, an incompatible server. It used to log a
                # warning and return False forever, so the engine stayed healthy
                # while every historical value was silently discarded. A few
                # attempts absorb a server still starting up; past that it is a
                # fault, and it says so.
                if self.schema_broken:
                    log.error("clickhouse schema setup has failed %d times (%s) — "
                              "HISTORY IS NOT BEING WRITTEN; reporting unhealthy",
                              self._schema_failures, exc, exc_info=True)
                else:
                    log.warning("clickhouse schema setup failed (%s); will retry", exc, exc_info=True)
                return False
        return True

    @property
    def schema_broken(self) -> bool:
        """True once the schema has failed enough times to be a fault, not a wait.

        Deliberately does NOT stop the engine: state projection, automations and the
        house keep working without history. It only makes the engine report unhealthy,
        which is what surfaces it — in the deploy health gate, in `docker ps`, and on
        the System page — instead of a warning in a log nobody reads at boot."""
        if self._schema_failures < SCHEMA_FAIL_LIMIT or self._schema_first_failure is None:
            return False
        return time.monotonic() - self._schema_first_failure >= SCHEMA_FAIL_SECONDS

    async def _close_client(self) -> None:
        """Drop the client, closing its aiohttp session so it doesn't leak."""
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def enqueue(self, entity_id: str, capability: str, adapter: str, value: Value, ts_ns: int) -> None:
        if capability in NON_HISTORIZED:
            return  # live-only cap; lives in Postgres current_state, never in the firehose
        if adapter == MIRRORED_ADAPTER:
            return  # the peer installation is the system of record for its own history
        value_num: float | None = None
        value_str: str | None = None
        if isinstance(value, bool):
            value_num = 1.0 if value else 0.0
        elif isinstance(value, int | float):
            value_num = float(value)
        else:
            value_str = str(value)
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
        if len(self._buf) + len(self._pending) >= BUFFER_CAP:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.error("history buffer full — dropped %d rows (clickhouse down?)", self._dropped)
            if not self._buf:
                return
            self._buf.popleft()
        self._buf.append((ts, entity_id, capability, adapter, value_num, value_str))

    async def insert_command(
        self, entity_id: str, capability: str, command: str, source: str, args: dict, ts_ns: int
    ) -> None:
        """Write ONE command audit row SYNCHRONOUSLY, raising on failure.

        Unlike the state firehose, the audit path is NOT buffered: commands are low
        volume, and the durable consumer's whole promise is that an audit row
        survives a crash. Buffering broke that — the consumer acked before the row
        was durable, so a crash lost it and a redelivery duplicated it. Here the
        consumer's ack waits for this INSERT, so a crash or ClickHouse outage naks
        and the message redelivers (at-least-once). The redelivered command forms a
        byte-identical single-row block, which the non_replicated_deduplication_window
        (ch migration 0005) drops — so redelivery is idempotent, no duplicate row."""
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
        row = [ts, entity_id, capability, command, source,
               json.dumps(args, ensure_ascii=False, default=str) if args else ""]
        if not await self._ensure():
            raise RuntimeError("clickhouse unavailable — command audit will redeliver")
        try:
            await self._client.insert("command_history", [row], column_names=CMD_COLUMNS)
        except Exception:
            await self._close_client()  # force a reconnect on the next attempt
            raise

    async def flush(self) -> bool:
        async with self._flush_lock:
            if not self._buf and not self._pending:
                return False
            if not self._pending and self._schema_ready and time.monotonic() >= self._next_day_tz_check:
                self._next_day_tz_check = time.monotonic() + DAY_TZ_CHECK_S
                if (await house_timezone(self._pool)).key != self._day_tz:
                    self._schema_ready = False
            if not await self._ensure():
                return False
            if not self._pending:
                self._pending = tuple(self._buf.popleft() for _ in range(min(len(self._buf), BATCH_SIZE)))
                self._pending_token = uuid4().hex
            return await self._flush_pending()

    async def _flush_pending(self) -> bool:
        try:
            await self._client.insert("state_history", self._pending, column_names=COLUMNS,
                                      settings={"insert_deduplicate": 1,
                                                "deduplicate_blocks_in_dependent_materialized_views": 1,
                                                "insert_deduplication_token": self._pending_token})
        except asyncio.CancelledError:
            await self._close_client()
            raise
        except Exception:
            await self._close_client()
            log.exception("clickhouse state_history insert failed; retaining %d rows for retry", len(self._pending))
            return False
        self._pending = ()
        self._pending_token = None
        return True

    async def close(self) -> None:
        while await self.flush():
            pass
        if self._client is not None:
            await self._client.close()
            self._client = None
