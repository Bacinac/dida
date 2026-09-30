from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import signal
import time

import asyncpg
from dida_core import (
    AREA_REFERENCES,
    CORE,
    ENGINE_EVENTS_SUBJECT,
    SETTING_REFERENCES,
    WALKED_COLUMNS,
    Bus,
    CapabilityError,
    Command,
    EntityInfo,
    ReachabilityEvent,
    StateUpdate,
    apply_migrations,
    attach_log_bus,
    classify_device_type,
    emit_journal,
    entity_references,
    path_references,
    pg_pool,
    resolve_device_type,
    rewrite_paths,
    rewrite_references,
    rewrite_setting,
    run_service,
    setting_references,
    setup_logging,
    validate_state,
)
from dida_core.events import HEARTBEAT_S
from dida_core.journal import JournalKind
from home_core.health import HealthMarker
from home_core.tasks import spawn

from dida_engine.history import HistoryWriter

setup_logging()
log = logging.getLogger("dida.engine")

# The adapter that registered a device owns it. Another adapter may group its own
# entity into it (heos files its player under the AVR the denon adapter registered)
# without taking it over: ownership decides whose silence and whose reachability
# verdict the device follows, and two owners took turns at both.
_UPSERT_DEVICE = """
INSERT INTO devices (device_key, adapter, name, native_key, site, last_seen)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (device_key) DO UPDATE SET
    last_seen = now(),
    name = COALESCE(devices.name, EXCLUDED.name),
    site = COALESCE(EXCLUDED.site, devices.site),
    -- Learn the native key when the adapter starts sending one; never unlearn it.
    -- The bridge list that carries it is retained but arrives after the first state
    -- topic, so a NULL here means "not known yet", never "this device has none".
    native_key = CASE WHEN devices.adapter = EXCLUDED.adapter
                      THEN COALESCE(EXCLUDED.native_key, devices.native_key)
                      ELSE devices.native_key END
"""

# An entity id's namespace names the adapter that speaks for it. The one namespace
# several write is a person's presence: GPS (the presence adapter, the api's
# OwnTracks and web endpoints) and the Wi-Fi association unifi sees resolve into
# the same `presence:<user>` entity.
_SHARED_NAMESPACES = {"presence": frozenset({"presence", "unifi"})}


def _owns(adapter: str, entity_id: str) -> bool:
    ns = entity_id.split(":", 1)[0]
    return adapter == ns or adapter in _SHARED_NAMESPACES.get(ns, ())


def _json(body):
    return json.loads(body) if isinstance(body, str) else body

# State path: create the entity on first sight and accumulate the capability seen
# into its `capabilities` set (union). `exposed` defaults lean — a diagnostic (or
# energy-counter, which the adapter flags diagnostic) field starts hidden; the
# user re-exposes per field in Settings → Adapters. Only set on INSERT; the
# user's later choice is preserved (never in the ON CONFLICT clause).
_UPSERT_ENTITY = """
INSERT INTO entities (entity_id, name, adapter, diagnostic, category, device_key, capabilities, exposed, last_seen)
VALUES ($1, $2, $3, $4, $7, $5, $6::jsonb, NOT $4, now())
ON CONFLICT (entity_id) DO UPDATE SET
    last_seen = now(),
    adapter = EXCLUDED.adapter,
    name = COALESCE(EXCLUDED.name, entities.name),
    diagnostic = EXCLUDED.diagnostic,
    category = EXCLUDED.category,
    device_key = COALESCE(EXCLUDED.device_key, entities.device_key),
    capabilities = CASE
        WHEN entities.capabilities @> EXCLUDED.capabilities THEN entities.capabilities
        ELSE entities.capabilities || EXCLUDED.capabilities END
"""

# Announce path: an adapter declares an entity EXISTS with its full metadata,
# with no state. `capabilities` is authoritative (the device's whole field set);
# `exposed` defaults lean on insert (hidden for diagnostic/energy fields) and
# keeps the user's choice on update.
#
# `voice_exposed` (push to Google/Matter) is deliberately NOT set here: migration
# 0062 made it TRI-STATE — NULL follows the rule (computed once by the generated
# `voice_effective` column), true/false are the user's explicit overrides. Writing
# a concrete value on insert would stamp a deliberate override on first sight, and
# a new controllable switch or cover would land as `false` = "keep out" and never
# reach the assistant — the exact chore 0062 exists to eliminate. Leave it NULL and
# let the rule decide; the user still overrides per entity in Settings → Adapters.
#
# device_type is DIDA's canonical kind — SEEDED here (a concrete, non-null value
# resolved from the adapter hint or the capabilities), then OWNED by DIDA: the
# COALESCE keeps the existing value, so a re-announce only fills it when still
# null and never overwrites a seed or a user's explicit choice. The adapter can
# change what it *thinks* a device is; DIDA's type does not follow it.
_UPSERT_ENTITY_INFO = """
INSERT INTO entities (entity_id, name, adapter, diagnostic, category, device_key, device_type, capabilities, exposed, last_seen)
VALUES ($1, $2, $3, $4, $8, $5, $6, $7::jsonb, NOT $4, now())
ON CONFLICT (entity_id) DO UPDATE SET
    last_seen = now(),
    adapter = EXCLUDED.adapter,
    name = COALESCE(EXCLUDED.name, entities.name),
    diagnostic = EXCLUDED.diagnostic,
    category = EXCLUDED.category,
    device_key = COALESCE(EXCLUDED.device_key, entities.device_key),
    device_type = COALESCE(entities.device_type, EXCLUDED.device_type),
    capabilities = EXCLUDED.capabilities
"""

# Ordering guard: only accept an update STRICTLY newer than what is stored. Two
# cases fall into the skip branch, both correctly: an out-of-order redelivery (a
# nak'd OLD value arriving after a NEWER one) can't regress the row, and an EXACT
# redelivery (same message, same ts_ns — JetStream at-least-once) is a no-op
# instead of re-applying (which would republish the event, double-fire momentary
# BUTTON automations, and double-count history). Equal ts_ns can only be a
# duplicate: every adapter stamps time.time_ns() at receive, so distinct reports
# never collide. RETURNING lets on_state see whether the write applied — a
# skipped update must NOT republish (it would fire a phantom transition).
_UPSERT_STATE = """
INSERT INTO current_state (entity_id, capability, value, unit, ts_ns, updated_at, changed_at)
VALUES ($1, $2, $3::jsonb, $4, $5, now(), now())
ON CONFLICT (entity_id, capability)
DO UPDATE SET value = EXCLUDED.value, unit = EXCLUDED.unit,
              ts_ns = EXCLUDED.ts_ns, updated_at = now(),
              changed_at = CASE WHEN current_state.value IS DISTINCT FROM EXCLUDED.value
                                THEN now() ELSE current_state.changed_at END
    WHERE current_state.ts_ns < EXCLUDED.ts_ns
RETURNING 1
"""


_INSERT_OUTBOX = "INSERT INTO state_outbox (payload) VALUES ($1)"
# Postgres NOTIFY channel the engine rings on commit so the relay drains at once.
OUTBOX_CHANNEL = "dida_state_outbox"
# Rows drained per round trip. Only ever a backlog after downtime — the steady state
# is one row per notify.
_OUTBOX_BATCH = 256
# Fallback drain cadence. The NOTIFY is the fast path; this is what makes a missed
# one (listener connection dropped, engine restarted with rows already committed) a
# latency blip instead of a lost event.
_OUTBOX_POLL = 1.0
# No drain completed in this long: the bus or the database refuses, and committed
# events wait unannounced. The engine reports unhealthy until one completes.
_OUTBOX_STUCK_S = 30.0


_MAX_META = 512  # bound on identifier/label strings (entity_id, name, unit, device[_name])
_MAX_FUTURE_NS = 5 * 60 * 1_000_000_000  # reject a ts_ns more than 5 min ahead of now (clock skew)
# Four missed heartbeats: one lost message or a slow reconnect is not a death.
ADAPTER_SILENCE_S = 4 * HEARTBEAT_S
_JOURNAL_THROTTLE_S = 60.0  # per (kind, entity/capability): one journal row a minute, not per message


def _oversized_meta(obj) -> str | None:
    """The first metadata string on a StateUpdate/EntityInfo that exceeds the bound,
    or None. validate_state guards `value`; these sibling strings land in unbounded
    TEXT columns (entity_id is the PK / FK target) and a buggy adapter could stream
    a multi-MB one into the DB — reject it at the boundary, same fail-loud contract."""
    for field in ("entity_id", "name", "unit", "device", "device_name"):
        v = getattr(obj, field, None)
        if isinstance(v, str) and len(v) > _MAX_META:
            return field
    return None


def _effective_category(diagnostic: bool, category: str) -> str:
    """A diagnostic reading belongs in the 'diagnostic' bucket even when the adapter
    left `category` at its 'control' default — so UI filters that hide diagnostics
    (e.g. the automation entity picker) actually hide it. An explicit 'config' is
    left as the adapter set it."""
    return "diagnostic" if diagnostic and category == "control" else category


class OutboxRelay:
    """Drains committed state events from `state_outbox` onto the events bus.

    The engine's projection writes the event and the row that says "announce this" in
    ONE transaction; this is the other half — it publishes those rows and only then
    forgets them. Splitting it this way is what removes the last silent drop: nothing
    can be in current_state without a durable instruction to announce it.

    Ordering: the engine's state consumer is a single sequential loop, so rows are
    committed in `id` order and draining in `id` order replays them in the order they
    were projected. (One engine — a second writer would need SKIP LOCKED and would
    lose that guarantee.)

    Delivery is AT-LEAST-ONCE, deliberately. A crash between the flush and the delete
    replays those events; a duplicate is visible and mostly inert (triggers fire on
    transitions), whereas the loss it replaces was silent and permanent. Same trade
    the bus itself already makes.
    """

    def __init__(self, bus: Bus, pool: asyncpg.Pool) -> None:
        self._bus = bus
        self._pool = pool
        self._wake = asyncio.Event()
        self._published = 0
        self._replayed = 0  # rows drained on a poll (a notify was missed / restart backlog)
        self._drained_at = time.monotonic()

    def wake(self) -> None:
        self._wake.set()

    def _notify(self, *_) -> None:
        """asyncpg LISTEN callback. A bound method (not a lambda) so it can be
        passed to remove_listener at shutdown."""
        self._wake.set()

    async def drain_once(self) -> int:
        """Publish one batch; return how many rows were drained. Deletes ONLY after
        the flush confirms the bus has them — publish alone just buffers in the
        client, so deleting first would reintroduce the very drop this exists to
        stop."""
        rows = await self._pool.fetch(
            "SELECT id, payload FROM state_outbox ORDER BY id LIMIT $1", _OUTBOX_BATCH
        )
        if not rows:
            self._drained_at = time.monotonic()
            return 0
        for r in rows:
            await self._bus.publish_raw(ENGINE_EVENTS_SUBJECT, r["payload"])
        await self._bus.flush()
        await self._pool.execute("DELETE FROM state_outbox WHERE id <= $1", rows[-1]["id"])
        self._published += len(rows)
        self._drained_at = time.monotonic()
        return len(rows)

    def stuck(self) -> bool:
        return time.monotonic() - self._drained_at > _OUTBOX_STUCK_S

    async def run(self, stop: asyncio.Event) -> None:
        """Drain on every notify, and on a slow tick regardless — the tick is what
        turns a missed notify (dropped listener, rows committed while we were down)
        into a late event instead of a lost one."""
        while not stop.is_set():
            # Clear BEFORE draining, never after: a notify that lands mid-drain must
            # leave the flag set, or we would clear away a wakeup for a row we had not
            # yet read and sit out the poll interval holding it.
            self._wake.clear()
            try:
                while await self.drain_once() == _OUTBOX_BATCH:
                    pass  # a backlog: keep going until it is drained
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let a bad round kill the relay: the rows are still committed,
                # so the next tick retries them. Loud, because a persistent failure
                # here means events ARE piling up unannounced.
                log.error("outbox drain failed (rows stay queued, retrying): %s", exc, exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=_OUTBOX_POLL)

    async def backlog(self) -> int:
        return await self._pool.fetchval("SELECT count(*) FROM state_outbox") or 0


class Engine:
    def __init__(self, bus: Bus, pool: asyncpg.Pool, history: HistoryWriter) -> None:
        self._bus = bus
        self._pool = pool
        self._history = history
        self._accepted = 0
        self._rejected = 0
        self._stale = 0  # updates dropped as out-of-order or duplicate (ts_ns not newer than stored)
        self._removed: set[str] = set()  # user-removed device/entity keys (drop their updates)
        # Journal throttle: (kind, key) -> [next_allowed_monotonic, suppressed_count].
        self._journal_gate: dict[tuple[str, str], list] = {}
        # Adapter liveness: last heartbeat per namespace, and the verdict last written.
        self._beats: dict[str, float] = {}
        self._alive: dict[str, bool] = {}
        self._started = time.monotonic()

    async def load_removed(self) -> None:
        rows = await self._pool.fetch("SELECT key FROM removed_devices")
        self._removed = {r["key"] for r in rows}

    async def seed_missing_types(self) -> None:
        """Give every type-less entity a concrete canonical device_type. Covers
        adapters that never send a type hint (their EntityInfo/state leaves it
        null) and the post-migration null rows. Idempotent: only touches nulls,
        and once seeded the upsert never reverts them, so this converges to a
        no-op. Classified from the stored capability set."""
        rows = await self._pool.fetch(
            "SELECT entity_id, capabilities FROM entities WHERE device_type IS NULL"
        )
        for r in rows:
            caps = r["capabilities"]
            if isinstance(caps, str):
                caps = json.loads(caps)
            await self._pool.execute(
                "UPDATE entities SET device_type = $2 WHERE entity_id = $1",
                r["entity_id"], classify_device_type(caps),
            )
        if rows:
            log.info("seeded device_type for %d type-less entities", len(rows))

    async def _journal(self, kind: JournalKind, key: str, **kw) -> None:
        """Emit a journal event, at most once a minute per (kind, key).

        Rejections are the loudest thing the engine has to say, and a single
        misbehaving adapter can produce them at line rate — unthrottled that is
        tens of millions of ClickHouse rows for one broken device, which drowns
        the very timeline it should be lighting up. So the FIRST occurrence goes
        out immediately (nothing is delayed) and a burst behind it is collapsed
        into the next window's `suppressed` count: still loud, still complete in
        what it claims, just not per-message.
        """
        gate = self._journal_gate.setdefault((kind, key), [0.0, 0])
        now = time.monotonic()
        if now < gate[0]:
            gate[1] += 1
            return
        suppressed, gate[1] = gate[1], 0
        gate[0] = now + _JOURNAL_THROTTLE_S
        data = dict(kw.pop("data", None) or {})
        if suppressed:
            data["suppressed"] = suppressed
        await emit_journal(self._bus, kind, source="engine", data=data or None, **kw)

    async def on_forget(self, key: str) -> None:
        """The API removed a device — block its key so re-publishes are dropped.
        Also delete its rows HERE (idempotent): a state message already past the
        _forgotten check can re-insert the entity after the API's DELETE, and
        with the key now blocked nothing would ever clean that orphan up."""
        self._removed.add(key)
        await self._journal("removed", key, device_key=key, severity="notice",
                            message="device removed by the user")
        try:
            await self._pool.execute(
                "DELETE FROM entities WHERE device_key = $1 OR entity_id = $1", key
            )
        except Exception as exc:
            log.warning("forget %s: cleanup delete failed: %s", key, exc, exc_info=True)

    async def on_restore(self, key: str) -> None:
        """The API un-removed a device. Dropping the tombstone row is not enough:
        the block is this in-memory set, so without this the key stays deaf until
        the engine happens to restart — which looks exactly like a restore that
        silently did nothing. The device itself comes back on its adapter's next
        announce; nothing is re-created here."""
        self._removed.discard(key)
        await self._journal("restored", key, device_key=key, severity="notice",
                            message="device restored by the user")

    async def on_reachability(self, event: ReachabilityEvent) -> None:
        """An adapter's verdict on whether a device is reachable. Recorded on the
        device row so every entity of it inherits the answer. `reachable_since` is
        stamped only on a CHANGE — the alert's hold and the floor plan's "down for
        20 min" read it, so bumping it on every repeat would reset that clock. A
        verdict for a device the engine has never seen is dropped: the row is created
        by state/announce, and conjuring one from a status ping would leave an
        entity-less ghost. So is one from an adapter that only groups an entity into
        a device another adapter owns: the owner's verdict is the device's. On a transition it journals online/offline against the
        device, which is where "when did this stop reporting" gets answered."""
        if event.device_key in self._removed:
            return
        was = await self._pool.fetchval(
            "WITH prev AS (SELECT reachable AS was FROM devices WHERE device_key = $1 AND adapter = $3) "
            "UPDATE devices d SET reachable = $2, "
            "  reachable_since = CASE WHEN d.reachable IS DISTINCT FROM $2 THEN now() ELSE d.reachable_since END "
            "FROM prev WHERE d.device_key = $1 RETURNING prev.was",
            event.device_key, event.reachable, event.adapter,
        )
        if was is None:
            return  # no such device row, or not this adapter's — nothing to attach the verdict to
        if was != event.reachable:
            await self._journal(
                "online" if event.reachable else "offline", event.device_key,
                device_key=event.device_key,
                severity="info" if event.reachable else "warning",
                message=(f"reachable again (via {event.adapter})" if event.reachable
                         else f"unreachable{f' — {event.detail}' if event.detail else ''} (via {event.adapter})"),
            )

    async def on_heartbeat(self, adapter: str) -> None:
        self._beats[adapter] = time.monotonic()
        if self._alive.get(adapter) is not True:
            await self._set_alive(adapter, True)

    async def judge_liveness(self) -> None:
        """An adapter namespace that owns devices and has not been heard from for
        ADAPTER_SILENCE_S is silent: its devices keep the last verdict it gave, and
        that verdict is no longer anybody's. Nothing is judged until the engine has
        itself been up that long, so a restart of the engine is not a silence."""
        now = time.monotonic()
        if now - self._started < ADAPTER_SILENCE_S:
            return
        owners = {r["adapter"] for r in await self._pool.fetch("SELECT DISTINCT adapter FROM devices")}
        for adapter in owners | self._beats.keys():
            alive = now - self._beats.get(adapter, float("-inf")) < ADAPTER_SILENCE_S
            if self._alive.get(adapter) is not alive:
                await self._set_alive(adapter, alive)

    async def _set_alive(self, adapter: str, alive: bool) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            was = await conn.fetchval("SELECT alive FROM adapter_liveness WHERE adapter = $1", adapter)
            await conn.execute(
                "INSERT INTO adapter_liveness (adapter, alive) VALUES ($1, $2) "
                "ON CONFLICT (adapter) DO UPDATE SET alive = $2, since = now() "
                "WHERE adapter_liveness.alive IS DISTINCT FROM $2",
                adapter, alive,
            )
            keys = [] if was == alive or (was is None and alive) else [
                r["device_key"] for r in await conn.fetch(
                    "SELECT device_key FROM devices WHERE adapter = $1", adapter)
            ]
        self._alive[adapter] = alive
        if keys:
            log.warning("adapter %s %s", adapter, "is heard again" if alive else "went silent")
        for key in keys:
            await self._journal(
                "online" if alive else "offline", key, device_key=key,
                severity="info" if alive else "warning",
                message=f"adapter {adapter} is heard again" if alive
                else f"adapter {adapter} went silent",
            )

    def _forgotten(self, update: StateUpdate) -> bool:
        # A device is keyed by its grouping key (device) or, ungrouped, its entity_id.
        return (update.device or update.entity_id) in self._removed or update.entity_id in self._removed

    async def _refuse_foreign(self, adapter: str, entity_id: str, device_key: str | None) -> None:
        self._rejected += 1
        ns = entity_id.split(":", 1)[0][:40]
        log.warning("rejected %s from adapter=%s: namespace %s is not its own",
                    entity_id[:80], adapter, ns)
        await self._journal(
            "validation_rejected", f"{entity_id[:80]}/owner",
            entity_id=entity_id[:80], device_key=device_key, severity="warning",
            message=f"adapter {adapter} wrote into namespace {ns}, which is not its own",
            data={"adapter": adapter},
        )

    async def on_state(self, update: StateUpdate) -> None:
        if self._forgotten(update):
            return  # user removed this device; ignore the adapter's re-publish
        if not _owns(update.adapter, update.entity_id):
            await self._refuse_foreign(update.adapter, update.entity_id, update.device)
            return
        # Boundary validation. A bad value is dropped here, loudly — it never
        # reaches current_state, so a misbehaving adapter can't corrupt us.
        try:
            value = validate_state(update.capability, update.value)
        except CapabilityError as exc:
            self._rejected += 1
            log.warning(
                "rejected %s/%s from adapter=%s: %s",
                update.entity_id, update.capability, update.adapter, exc,
            )
            await self._journal(
                "validation_rejected", f"{update.entity_id}/{update.capability}",
                entity_id=update.entity_id, device_key=update.device, severity="warning",
                message=str(exc),
                data={"capability": update.capability, "adapter": update.adapter,
                      "value": repr(update.value)[:200]},
            )
            return
        bad = _oversized_meta(update)
        if bad is not None:
            self._rejected += 1
            log.warning("rejected %s from adapter=%s: metadata %r exceeds %d chars",
                        update.entity_id[:80], update.adapter, bad, _MAX_META)
            await self._journal(
                "validation_rejected", f"{update.entity_id[:80]}/meta",
                entity_id=update.entity_id[:80], device_key=update.device, severity="warning",
                message=f"metadata {bad!r} exceeds {_MAX_META} chars",
                data={"field": bad, "adapter": update.adapter},
            )
            return
        # ts_ns is stamped by adapters on many hosts (the baba/peer remotes, the
        # seaside node). One host booting with its clock far in the future would
        # poison current_state.ts_ns and make EVERY subsequent correct update look
        # stale — the entity wedges until wall-clock catches up. Reject an
        # implausibly-future timestamp at the same fail-loud boundary that rejects
        # bad values; a small skew is left alone (the ordering guard handles it).
        if update.ts_ns - time.time_ns() > _MAX_FUTURE_NS:
            self._rejected += 1
            skew_s = (update.ts_ns - time.time_ns()) / 1e9
            log.warning("rejected %s/%s from adapter=%s: ts_ns %.0fs in the future (clock skew?)",
                        update.entity_id, update.capability, update.adapter, skew_s)
            await self._journal(
                "validation_rejected", f"{update.entity_id}/clock",
                entity_id=update.entity_id, device_key=update.device, severity="warning",
                message=f"timestamp {skew_s:.0f}s in the future (clock skew?)",
                data={"capability": update.capability, "adapter": update.adapter,
                      "skew_s": round(skew_s, 1)},
            )
            return

        # The event the api/automations will see: the validated, normalised value.
        # Carry `device` too so a live UI can group a just-added device's entities
        # into its card without waiting for a full reload.
        event = StateUpdate(
            entity_id=update.entity_id,
            capability=update.capability,
            value=value,
            adapter=update.adapter,
            ts_ns=update.ts_ns,
            unit=update.unit,
            name=update.name,
            diagnostic=update.diagnostic,
            category=_effective_category(update.diagnostic, update.category),
            device=update.device,
            device_name=update.device_name,
            received_ns=update.received_ns,
        )

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _UPSERT_ENTITY, update.entity_id, update.name, update.adapter,
                update.diagnostic, update.device, json.dumps([update.capability]),
                _effective_category(update.diagnostic, update.category),
            )
            if update.device:
                # Keep the devices row alive from the state path too — an adapter
                # that groups entities but never sends EntityInfo otherwise leaves
                # the card headerless (no devices row → no name/label to edit).
                # device_name (if supplied) names the card; COALESCE keeps the
                # first non-null, so it never fights an EntityInfo announce.
                # No native key on the STATE path — only the announce carries one.
                # NULL here never unlearns it: the upsert COALESCEs onto what is
                # stored, so a device seen on a state topic between announces keeps
                # the identity that makes a rename recognisable.
                await conn.execute(_UPSERT_DEVICE, update.device, update.adapter,
                                   update.device_name, None, None)
            applied = await conn.fetchval(
                _UPSERT_STATE,
                update.entity_id,
                update.capability,
                json.dumps(value),
                update.unit,
                update.ts_ns,
            )
            if applied:
                # THE point of the outbox: the projection and the intent to announce
                # it commit together or not at all. Publishing after the commit was
                # the last silent-drop in the core — the redelivery that should cover
                # a failed publish is deduped away by the ts_ns guard above, so a lost
                # publish left current_state holding a value nothing ever heard about.
                await conn.execute(_INSERT_OUTBOX, self._bus.encode_event(event))
                # Wake the relay the moment this commits (a NOTIFY fires on commit,
                # never on rollback) — the drain is a fallback poll otherwise, and the
                # motion→light path should not wait out a poll interval.
                await conn.execute("SELECT pg_notify($1, '')", OUTBOX_CHANNEL)

        if not applied:
            # An equal-or-newer value is already stored (exact duplicate or
            # out-of-order redelivery). Keep it: don't republish (a phantom
            # transition would fire automation on the old value, and a duplicate
            # would double-fire momentary BUTTONs) or append a superseded point
            # to history. Still handled → the consumer acks; the redelivered
            # message is correctly done. This is what makes redelivery idempotent.
            self._stale += 1
            return

        # The event is now committed to the outbox — OutboxRelay owns publishing it.
        # Append to the history firehose (buffered; never blocks projection).
        self._history.enqueue(update.entity_id, update.capability, update.adapter, value, update.ts_ns)
        self._accepted += 1

    async def on_command(self, command: Command) -> None:
        """Audit-mirror every bus command into command_history (WHO asked WHAT).
        Pure observer: adapters execute from their own live core subscription —
        this durable consumer only records, so a replay after engine downtime
        back-fills audit rows and can never re-run a command. The insert is
        synchronous and may raise: the consumer then naks and redelivers (the row
        is idempotent via the dedup window), so the audit trail is not lost on a
        crash the way a buffered write was."""
        await self._history.insert_command(
            command.entity_id, command.capability, command.command,
            command.source, dict(command.args), command.ts_ns,
        )

    async def _rename_device(self, old_key: str, new_key: str, adapter: str) -> bool:
        """The same physical device came back under a different name. Move it.

        entity_id is derived from the device's NAME, so a rename in zigbee2mqtt or
        the Tuya app produces a NEW id. Without this the old entity simply goes
        stale beside a fresh duplicate, and every automation, scene, area and
        schedule still names the id that stopped updating — they keep loading, keep
        evaluating, and never fire. Measured on this installation: one rename of a
        commonly-referenced entity would silently kill ten rules.

        ONE transaction, or none of it. A half-applied rename — entities moved,
        references not — is strictly worse than the duplicate it replaces, because
        the rules would then point at an id that no longer exists at all.

        This writes to automation-owned tables, which the engine otherwise never
        touches. That is deliberate: the rename and the references it invalidates
        have to commit together, and the engine is the only place that holds both."""
        old_prefix = f"{adapter}:{old_key}"
        new_prefix = f"{adapter}:{new_key}"

        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT entity_id FROM entities WHERE entity_id = $1 OR entity_id LIKE $2",
                old_prefix, old_prefix + ":%")
            mapping = {r["entity_id"]: new_prefix + r["entity_id"][len(old_prefix):]
                       for r in rows}
            if not mapping:
                return False

            # A slug collision means the new name already exists as a DIFFERENT
            # device. Renaming into it would merge two devices' state; refuse and
            # say so, leaving the duplicate to be reported by the orphan scan.
            clash = await conn.fetchval(
                "SELECT count(*) FROM entities WHERE entity_id = ANY($1::text[])",
                list(mapping.values()))
            if clash:
                log.error("refusing to rename %s -> %s: the new id already exists",
                          old_prefix, new_prefix)
                return False

            # `current_state` follows via ON UPDATE CASCADE (migration 0070).
            for old_id, new_id in mapping.items():
                await conn.execute(
                    "UPDATE entities SET entity_id = $2, device_key = $3 WHERE entity_id = $1",
                    old_id, new_id, new_key)
            await conn.execute(
                "UPDATE devices SET device_key = $2 WHERE device_key = $1", old_key, new_key)

            # A rename has to move every reference or it has moved nothing: a rule
            # left pointing at the old id keeps loading, keeps evaluating, and
            # never fires again.
            moved = 0
            for table, column in WALKED_COLUMNS:
                for row in await conn.fetch(
                        f"SELECT id, {column} AS body FROM {table} WHERE {column} IS NOT NULL"):  # noqa: S608
                    body = _json(row["body"])
                    if not (entity_references(body) & mapping.keys()):
                        continue
                    await conn.execute(
                        f"UPDATE {table} SET {column} = $2 WHERE id = $1",  # noqa: S608
                        row["id"], json.dumps(rewrite_references(body, mapping)))
                    moved += 1
            for row in await conn.fetch(f"SELECT id, {', '.join(AREA_REFERENCES)} FROM areas"):  # noqa: S608
                for column, paths in AREA_REFERENCES.items():
                    body = _json(row[column])
                    if not (path_references(body, paths) & mapping.keys()):
                        continue
                    await conn.execute(
                        f"UPDATE areas SET {column} = $2 WHERE id = $1",  # noqa: S608
                        row["id"], json.dumps(rewrite_paths(body, paths, mapping)))
                    moved += 1
            for row in await conn.fetch(
                    "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
                    [key for key, paths in SETTING_REFERENCES.items() if paths]):
                if not (setting_references(row["key"], row["value"]) & mapping.keys()):
                    continue
                await conn.execute(
                    "UPDATE app_settings SET value = $2, updated_at = now() WHERE key = $1",
                    row["key"], rewrite_setting(row["key"], row["value"], mapping))
                moved += 1

        log.warning("device renamed: %s -> %s (%d entities, %d references rewritten)",
                    old_prefix, new_prefix, len(mapping), moved)
        await self._journal(
            "device_renamed", new_prefix, device_key=new_key, severity="warning",
            message=f"{old_prefix} -> {new_prefix}",
            data={"entities": len(mapping), "references": moved, "adapter": adapter},
        )
        return True

    async def on_entity_info(self, info: EntityInfo) -> None:
        """An adapter announced an entity's catalog metadata (no state). Register
        it so the UI can list a device's full field set — including fields with no
        value yet (a write-only button) or that DIDA can't map (capabilities empty,
        shown but not exposable). Never touches current_state or `exposed`."""
        if (info.device or info.entity_id) in self._removed or info.entity_id in self._removed:
            return  # user removed this device
        if not _owns(info.adapter, info.entity_id):
            await self._refuse_foreign(info.adapter, info.entity_id, info.device)
            return
        bad = _oversized_meta(info)
        if bad is not None:
            log.warning("rejected EntityInfo %s from adapter=%s: metadata %r exceeds %d chars",
                        info.entity_id[:80], info.adapter, bad, _MAX_META)
            await self._journal(
                "validation_rejected", f"{info.entity_id[:80]}/announce",
                entity_id=info.entity_id[:80], device_key=info.device, severity="warning",
                message=f"announce metadata {bad!r} exceeds {_MAX_META} chars",
                data={"field": bad, "adapter": info.adapter},
            )
            return
        # A device we already know, under a new name? Move it FIRST.
        #
        # Order is the whole feature: the upsert below creates `ren:new_name`, and a
        # rename into an id that already exists is refused (it would merge two
        # devices). Run this after the upsert and every rename is declined by the
        # row the upsert just made — the guard fires on its own side effect, the
        # duplicate appears anyway, and the unique index on (adapter, native_key)
        # then rejects the write outright.
        native_key = info.native_key
        if info.device and native_key:
            known = await self._pool.fetchval(
                "SELECT device_key FROM devices WHERE adapter = $1 AND native_key = $2",
                info.adapter, native_key)
            # A refused rename (the target id already exists — two devices whose
            # names slug to the same thing) must not carry the key forward:
            # recording it would violate the one-device-per-native-key index and
            # reject the whole announce, taking a working device offline over a
            # naming collision. Register it un-keyed instead — it behaves as it did
            # before this feature, and the collision is in the log, not a crash loop.
            if (known and known != info.device
                    and not await self._rename_device(known, info.device, info.adapter)):
                native_key = None

        # Seed a concrete canonical type from the adapter's hint or the caps. The
        # upsert only applies it when the entity has none yet (never clobbers).
        seed_type = resolve_device_type(info.device_type, info.capabilities)
        await self._pool.execute(
            _UPSERT_ENTITY_INFO, info.entity_id, info.name, info.adapter,
            info.diagnostic, info.device, seed_type, json.dumps(info.capabilities),
            _effective_category(info.diagnostic, info.category),
        )
        # The device's friendly name lives once, in `devices` — not on the entity.
        if info.device:
            await self._pool.execute(
                _UPSERT_DEVICE, info.device, info.adapter, info.device_name,
                native_key, info.site,
            )


# How often the removed-device tombstones are re-read. A named constant so the
# service suite can shrink it — an invariant that only proves itself after half a
# minute is one nothing will ever test.
REMOVED_RELOAD_S = 30


async def _flush_loop(history: HistoryWriter, stop: asyncio.Event) -> None:
    """Flush the history buffer to ClickHouse ~once a second. Runs apart from
    state projection so ClickHouse latency never touches it."""
    while not stop.is_set():
        await history.flush()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)


async def _on_forget(engine: Engine, msg) -> None:
    key = msg.data.decode().strip()
    if key:
        await engine.on_forget(key)


async def _on_restore(engine: Engine, msg) -> None:
    key = msg.data.decode().strip()
    if key:
        await engine.on_restore(key)


async def _state_backlog(bus: Bus) -> int:
    try:
        return await bus.pending("DIDA_STATE", "engine-state")
    except Exception:
        log.debug("engine: stream backlog unreadable", exc_info=True)
        return -1


async def _on_stats(bus: Bus, engine: Engine, relay: OutboxRelay, history: HistoryWriter,
                    started: float, msg) -> None:
    """Live throughput counters + JetStream backlog + history buffer health, so the
    API/System page (and any metrics scraper) can surface what until now only
    reached the logs."""
    if not msg.reply:
        return
    try:
        queued_events = await relay.backlog()
    except Exception:
        log.debug("engine stats: outbox backlog unreadable", exc_info=True)
        queued_events = -1
    payload = {
        "accepted": engine._accepted,
        "rejected": engine._rejected,
        "stale": engine._stale,
        "backlog": await _state_backlog(bus),
        # Committed events not yet announced. Steady state is 0 — anything that
        # sits here is a bus the engine cannot reach, which used to be invisible.
        "outbox": queued_events,
        "published": relay._published,
        "uptime_s": round(asyncio.get_running_loop().time() - started, 1),
        "history": history.stats(),
    }
    await bus.nc.publish(msg.reply, json.dumps(payload).encode())


class _HealthGate:
    """Health is CONDITIONAL on the history schema and the outbox. The engine keeps
    projecting state either way — the house does not stop because graphs do — but a
    ClickHouse schema that will not apply means every historical value is being
    discarded, and that used to be a single warning line at boot with a green
    container above it. Withholding the marker puts it where problems are already
    looked for: the deploy health gate, `docker ps`, the System page."""

    def __init__(self) -> None:
        self.health = HealthMarker("dida", "engine")
        self.schema_reported = False
        self.outbox_reported = False

    def judge(self, history: HistoryWriter, relay: OutboxRelay) -> None:
        if history.schema_broken:
            if not self.schema_reported:
                log.error("engine reporting UNHEALTHY — clickhouse history schema "
                          "could not be applied; state projection continues")
                self.schema_reported = True
        elif relay.stuck():
            if not self.outbox_reported:
                log.error("engine reporting UNHEALTHY — the outbox has not drained for %.0f s; "
                          "committed state events are not being announced", _OUTBOX_STUCK_S)
                self.outbox_reported = True
        else:
            if self.outbox_reported:
                log.warning("outbox draining again — engine healthy")
                self.outbox_reported = False
            self.health.touch()


async def _open_relay(bus: Bus, pool: asyncpg.Pool) -> tuple[OutboxRelay, asyncpg.Connection]:
    # Outbox relay: the only publisher of engine events. Hold a dedicated connection
    # for LISTEN (a pooled one would be handed back and stop listening); if it ever
    # drops, the relay's own poll keeps draining, so this is latency insurance, not
    # the correctness mechanism.
    relay = OutboxRelay(bus, pool)
    listener = await pool.acquire()
    await listener.add_listener(OUTBOX_CHANNEL, relay._notify)
    queued = await relay.backlog()
    if queued:
        # Events that outlived the process that projected them — exactly what the
        # outbox is for. Say so: silently replaying them would hide that it happened.
        log.warning("outbox: %d event(s) survived a restart — republishing", queued)
    return relay, listener


async def _supervise(bus: Bus, engine: Engine, relay: OutboxRelay, history: HistoryWriter,
                     stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    gate = _HealthGate()
    last_log = 0.0
    last_removed_reload = loop.time()
    while not stop.is_set():
        gate.judge(history, relay)
        now = loop.time()
        # Re-read the removed-device tombstones periodically. `forget` is a core-NATS
        # (at-most-once) message; one published while the engine is mid-reconnect is
        # lost, and load_removed only ran at boot — so without this the engine would
        # keep re-inserting a device the API deleted until an unrelated restart. The
        # set is a handful of rows.
        if now - last_removed_reload > REMOVED_RELOAD_S:
            with contextlib.suppress(Exception):
                await engine.load_removed()
            last_removed_reload = now
        try:
            await engine.judge_liveness()
        except Exception:
            log.exception("adapter liveness check failed")
        # Light heartbeat with throughput counters — the seed of the
        # `dida.stats.*` observability surface (BABA pattern).
        if now - last_log > 60:
            log.info("alive — accepted=%d rejected=%d stale=%d backlog=%d",
                     engine._accepted, engine._rejected, engine._stale, await _state_backlog(bus))
            last_log = now
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5)


async def main() -> None:
    bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-engine", user=CORE)
    pool = await pg_pool(max_size=8)
    await apply_migrations(pool)
    history = HistoryWriter(pool)
    await bus.connect()
    attach_log_bus(bus, "engine")
    engine = Engine(bus, pool, history)
    await engine.load_removed()
    await engine.seed_missing_types()
    # Durable JetStream consumers (at-least-once): ack happens only after the
    # current_state write committed, so a Postgres outage naks + redelivers instead
    # of silently dropping updates, and a restart/deploy resumes from the last acked
    # message — the deploy-window loss is gone FOR THE PROJECTION. History is the one
    # exception: it's enqueued to an in-memory buffer and flushed to ClickHouse ~1s
    # later, so a HARD crash (SIGKILL/OOM/power) can still lose up to one flush
    # interval of history rows — best-effort by design (never blocks projection),
    # not lossless. Commands/forget stay core NATS.
    await bus.ensure_streams()
    consumers = [
        await bus.consume_state_stream(engine.on_state, durable="engine-state"),
        await bus.consume_entity_stream(engine.on_entity_info, durable="engine-entities"),
        # Audit trail: mirror dida.command.> into ClickHouse command_history.
        await bus.consume_command_stream(engine.on_command, durable="engine-commands"),
    ]
    await bus.nc.subscribe("dida.engine.forget", cb=functools.partial(_on_forget, engine))
    await bus.nc.subscribe("dida.engine.restore", cb=functools.partial(_on_restore, engine))
    await bus.subscribe_reachability(engine.on_reachability)
    await bus.subscribe_heartbeats(engine.on_heartbeat)

    relay, listener = await _open_relay(bus, pool)
    log.info("engine up — durable consumers on dida.state.> + dida.entity.>")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await bus.nc.subscribe("dida.engine.stats", cb=functools.partial(
        _on_stats, bus, engine, relay, history, loop.time()))

    flusher = spawn(_flush_loop(history, stop), log=log, name="history flush loop")
    draining = spawn(relay.run(stop), log=log, name="outbox relay")
    await _supervise(bus, engine, relay, history, stop)

    log.info("shutting down")
    # Stop consuming first (in-flight msg stays unacked → redelivered next boot),
    # then flush history: cancel + AWAIT the flusher so a batch suspended inside
    # flush() isn't lost to a CancelledError bypassing its re-buffer path.
    for task in consumers:
        task.cancel()
    await asyncio.gather(*consumers, return_exceptions=True)
    flusher.cancel()
    await asyncio.gather(flusher, return_exceptions=True)
    # Drain the outbox before the bus goes: nothing is LOST either way (the rows are
    # committed and replay on the next boot), but announcing now spares the api and
    # automations a restart's worth of stale state. `stop` already ended relay.run().
    draining.cancel()
    await asyncio.gather(draining, return_exceptions=True)
    with contextlib.suppress(Exception):
        while await relay.drain_once():
            pass
    await history.close()
    await bus.close()
    # Release the dedicated LISTEN connection before closing the pool: pool.close()
    # waits until every acquired connection is released, so leaving this one out
    # hangs shutdown forever → SIGKILL on every deploy.
    with contextlib.suppress(Exception):
        await listener.remove_listener(OUTBOX_CHANNEL, relay._notify)
    with contextlib.suppress(Exception):
        await pool.release(listener)
    await pool.close()


if __name__ == "__main__":
    run_service(main())
