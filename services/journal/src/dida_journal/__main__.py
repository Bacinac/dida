"""Journal service — the durable sink for discrete device events.

Its own process for the reason every DIDA service is: if it dies, the house does
not notice. State projection, automations and the API keep running; the events
simply queue in JetStream and are written when it comes back. The engine stays
what it always was — validate → project → persist — and does not grow a second
job.

Delivery is at-least-once with ack-after-insert, the same discipline as the
command audit: the consumer acks only once the ClickHouse row is durable, so a
crash redelivers instead of losing the event, and the redelivered row forms a
byte-identical single-row block that the dedup window (ch migration 0006) drops.

The ENGINE owns the ClickHouse schema (it applies ch/migrations at boot, single
writer of DDL). This service only INSERTs. On a fresh install it may therefore
start before `device_events` exists — the insert fails, the message naks, and it
lands once the engine has migrated. Loud, self-healing, no second migration
runner racing the first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import UTC, datetime

from dida_core import (
    CORE,
    Bus,
    JournalEvent,
    LogRecord,
    attach_log_bus,
    ch_client,
    run_service,
    setup_logging,
)
from home_core.health import HealthMarker

setup_logging()
log = logging.getLogger("dida.journal.service")

COLUMNS = ["ts", "entity_id", "device_key", "source", "kind", "severity", "message", "data"]
LOG_COLUMNS = ["ts", "service", "level", "logger", "entity_id", "message", "exc"]


class JournalWriter:
    """One row per event, written synchronously so the ack means 'durable'.

    Not batched, unlike the state firehose: events are discrete and low volume (a
    few thousand a day against millions of state points), and batching would put
    an in-memory buffer between the ack and the disk — exactly the hole that made
    the command audit lose rows on a crash before it was made synchronous."""

    def __init__(self) -> None:
        self._client = None
        self._written = 0
        self._failed = 0
        # TWO consumers share this writer (events and log batches), so connecting
        # is a critical section. Without it both can see `_client is None`, both
        # build one, and the loser is overwritten and never closed — aiohttp then
        # reports "Unclosed client session" from the GC and the orphaned session's
        # insert queue shuts down under whichever consumer still held it. Found on
        # the first production run, by the log viewer this same commit added.
        self._connecting = asyncio.Lock()

    def stats(self) -> dict:
        return {"written": self._written, "failed": self._failed, "connected": self._client is not None}

    async def _ensure(self):
        if self._client is not None:
            return self._client
        async with self._connecting:
            if self._client is None:          # another consumer may have won the race
                self._client = await ch_client()
        return self._client

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def write(self, event: JournalEvent) -> None:
        """Raises on failure — that is the point: the caller naks and redelivers."""
        row = [
            datetime.fromtimestamp(event.ts_ns / 1e9, tz=UTC),
            event.entity_id, event.device_key, event.source,
            event.kind, event.severity, event.message, event.data,
        ]
        try:
            client = await self._ensure()
            await client.insert("device_events", [row], column_names=COLUMNS)
        except Exception:
            self._failed += 1
            await self._close_client()  # force a reconnect on the redelivery
            raise
        self._written += 1

    async def write_logs(self, records: list[LogRecord]) -> None:
        """A whole batch in ONE insert, raising on failure so the batch naks.

        Logs are the high-volume firehose, and a ClickHouse insert costs the same
        round trip for 1 row as for 500 — per-row here would turn a flush into
        hundreds of them. Redelivery of an identical batch is dropped by the
        block-hash dedup window (ch migration 0007)."""
        if not records:
            return
        rows = [
            [datetime.fromtimestamp(r.ts_ns / 1e9, tz=UTC), r.service, r.level,
             r.logger, r.entity_id, r.message, r.exc]
            for r in records
        ]
        try:
            client = await self._ensure()
            await client.insert("app_logs", rows, column_names=LOG_COLUMNS)
        except Exception:
            self._failed += len(rows)
            await self._close_client()
            raise
        self._written += len(rows)

    async def close(self) -> None:
        await self._close_client()


async def main() -> None:
    bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-journal", user=CORE)
    await bus.connect()
    writer = JournalWriter()

    async def on_event(event: JournalEvent) -> None:
        await writer.write(event)

    async def on_logs(records: list[LogRecord]) -> None:
        await writer.write_logs(records)

    await bus.ensure_streams()
    consumers = [
        await bus.consume_journal_stream(on_event, durable="journal-events"),
        await bus.consume_log_stream(on_logs, durable="journal-logs"),
    ]
    # This service publishes its own logs too — it is a normal service, and a
    # sink that cannot be diagnosed from the same place as everything else is a
    # blind spot. The MUTED_LOGGERS guard is what stops the obvious loop.
    # `dida.bus` muted HERE only: those are this consumer's own insert failures,
    # and publishing them onto the stream this consumer reads is a feedback loop.
    # They stay on stdout, which is where you look when the sink itself is broken.
    attach_log_bus(bus, "journal", mute=("dida.bus",))
    log.info("journal up — dida.journal -> device_events, dida.logs -> app_logs")

    health = HealthMarker("dida", "journal")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    last_log = 0.0
    while not stop.is_set():
        health.touch()
        now = loop.time()
        if now - last_log > 300:
            log.info("alive — %s", writer.stats())
            last_log = now
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5)

    log.info("shutting down")
    for c in consumers:
        c.cancel()
    await asyncio.gather(*consumers, return_exceptions=True)
    await writer.close()
    await bus.close()


if __name__ == "__main__":
    run_service(main())
