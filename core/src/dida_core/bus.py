"""Thin NATS wrapper — the isolation boundary between adapters and the engine.

Nothing in DIDA talks to another service directly; everything goes through the
bus. That decoupling is what makes a crashing adapter a non-event for the
engine: the bus stays up, the engine keeps consuming, and the dead adapter's
entities simply stop refreshing.

Reconnection is infinite by default (max_reconnect_attempts=-1): a NATS blip or
restart must never permanently wedge a service.

Delivery model is DUAL-MODE:

  * `dida.state.>` + `dida.entity.>` + `dida.events` are captured by JetStream
    streams (DIDA_STATE / DIDA_EVENTS) and consumed by the engine/automation via
    DURABLE pull consumers with explicit ack — at-least-once. The consumer acks
    only after its work (the DB write) succeeded; a failure naks for redelivery,
    and a restart resumes from the last acked position. Publishers stay plain
    `nc.publish` — the stream captures core-NATS publishes on its subjects, so
    adapters need no changes; core subscribers (api WS, matter-bridge) also keep
    receiving live copies unchanged.
  * Commands, status, discovery stay CORE NATS — at-most-once is the correct
    semantic there (a stale "turn on" must be dropped, never replayed). The
    DIDA_COMMANDS stream additionally CAPTURES `dida.command.>` for the audit
    trail only: adapters keep consuming live core copies (a replay can never
    re-execute a command), while the engine's durable audit consumer survives
    its own restarts without losing "who did what" rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import msgspec
import nats
import nats.errors
import nats.js.errors
from home_core.tasks import spawn
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)

from dida_core.events import (
    COMMAND_SUBJECT_PREFIX,
    ENGINE_EVENTS_SUBJECT,
    ENTITY_SUBJECT_PREFIX,
    HEARTBEAT_S,
    HEARTBEAT_SUBJECT,
    JOURNAL_SUBJECT,
    LOGS_SUBJECT,
    REACHABILITY_SUBJECT,
    STATE_SUBJECT_PREFIX,
    Command,
    EntityInfo,
    JournalEvent,
    LogRecord,
    ReachabilityEvent,
    StateUpdate,
    command_subject,
    entity_subject,
    heartbeat_subject,
    journal_subject,
    logs_subject,
    namespace_commands,
    publisher_of,
    reachability_subject,
    state_subject,
    vouched,
)

log = logging.getLogger("dida.bus")

# JetStream stream layout. Retention is limits-based (age+size), NOT work-queue:
# messages persist until they age out regardless of acks, so any number of
# durable consumers can track their own positions over the same stream.
STATE_STREAM = "DIDA_STATE"
EVENTS_STREAM = "DIDA_EVENTS"
COMMANDS_STREAM = "DIDA_COMMANDS"
JOURNAL_STREAM = "DIDA_JOURNAL"
LOGS_STREAM = "DIDA_LOGS"
_STREAMS: tuple[tuple[str, list[str]], ...] = (
    (STATE_STREAM, [f"{STATE_SUBJECT_PREFIX}.>", f"{ENTITY_SUBJECT_PREFIX}.>"]),
    (EVENTS_STREAM, [ENGINE_EVENTS_SUBJECT]),
    (COMMANDS_STREAM, [f"{COMMAND_SUBJECT_PREFIX}.>"]),
    (JOURNAL_STREAM, [f"{JOURNAL_SUBJECT}.>"]),
    (LOGS_STREAM, [f"{LOGS_SUBJECT}.>"]),
)
_MAX_AGE_S = 48 * 3600          # nothing in a house needs replay older than 2 days
_MAX_BYTES = 512 * 1024 * 1024  # per stream; nats.conf caps the whole store at 4 GB

# Redeliveries before JetStream drops a message. Handlers are idempotent, so the
# real reason to bound it is a decodable message whose handler ALWAYS raises — nak
# it a few times, then dead-letter it loudly rather than loop on it forever.
_MAX_DELIVER = 8

_encoder = msgspec.msgpack.Encoder()
_state_decoder = msgspec.msgpack.Decoder(StateUpdate)
_entity_decoder = msgspec.msgpack.Decoder(EntityInfo)
_command_decoder = msgspec.msgpack.Decoder(Command)
_journal_decoder = msgspec.msgpack.Decoder(JournalEvent)
_reachability_decoder = msgspec.msgpack.Decoder(ReachabilityEvent)
_log_decoder = msgspec.msgpack.Decoder(LogRecord)

# Where a bus client's password is: the keys service derives one per identity and
# writes it into the directory only that identity's containers mount. Unset (the
# gate's throwaway server) → no credentials.
_PASSWORD_FILE = "DIDA_NATS_PASSWORD_FILE"

_CRED_RE = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")


def _forged(subject: str, claim: str) -> bool:
    """A message whose payload names another publisher than its subject does. The
    server decides who may publish under which name, so the subject is the fact and
    the payload only a claim; such a message was made by hand to pass for someone."""
    if vouched(subject, claim):
        return False
    log.error("dropping a message on %s that claims to be from %r", subject, claim)
    return True


def _redact_url(url: str) -> str:
    """Mask the password in a ``scheme://user:pass@host`` NATS URL so it never
    lands in logs. Handles comma-separated server lists too."""
    return _CRED_RE.sub(r"\1***\2", url)


_REJECTED = object()
Settle = Callable[[Callable[[], Awaitable[None]], str, str], Awaitable[None]]


def _settler(stream: str, durable: str) -> Settle:
    async def settle(action: Callable[[], Awaitable[None]], kind: str, subject: str) -> None:
        # A NATS blip DURING ack/nak/term (reconnect window, closed connection
        # mid-restart) must not escape the loop and permanently kill the
        # consumer — the container would stay healthy while the engine goes
        # deaf. The message simply stays unacked and redelivers; every handler
        # is idempotent (upsert), so a double-delivery is harmless.
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s failed on %s (%s/%s): %s", kind, subject, stream, durable, exc, exc_info=True)
    return settle


async def _fetch(sub, batch: int, stream: str, durable: str) -> list:
    try:
        return await sub.fetch(batch, timeout=5)
    except (TimeoutError, nats.errors.TimeoutError):
        return []  # idle — nothing pending
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("js fetch (%s/%s): %s", stream, durable, exc, exc_info=True)
        await asyncio.sleep(1)
        return []


async def _admit(msg, decoder: msgspec.msgpack.Decoder, claim: Callable[[Any], str] | None,
                 settle: Settle) -> object:
    """The decoded frame, or _REJECTED once it has been termed."""
    try:
        obj = decoder.decode(msg.data)
    except msgspec.DecodeError as exc:
        log.warning("terminating undecodable msg on %s: %s", msg.subject, exc)
        await settle(msg.term, "term", msg.subject)
        return _REJECTED
    if claim is not None and _forged(msg.subject, claim(obj)):
        await settle(msg.term, "term", msg.subject)
        return _REJECTED
    return obj


def _failure_line(msg) -> tuple[str, int]:
    """Out of redeliveries, JetStream drops the message now. Say so loudly
    instead of letting it vanish (a decodable message whose handler always
    fails is a real, findable bug)."""
    delivered = 0
    with contextlib.suppress(Exception):
        delivered = msg.metadata.num_delivered
    if delivered >= _MAX_DELIVER:
        return "DEAD-LETTER on %s after %d deliveries: %s", delivered
    return "handler failed on %s (delivery %d, will redeliver): %s", delivered


class Bus:
    def __init__(self, url: str, *, name: str, user: str) -> None:
        self._url = url
        self._name = name
        self._user = user
        self._nc: NATSClient | None = None
        self._speaks_for: set[str] = set()
        self._beat: asyncio.Task | None = None

    async def connect(self) -> None:
        # Explicit connection callbacks: without them nats-py swallows handler
        # exceptions and connection churn into a context-free default logger —
        # reconnects and dropped messages were invisible (audit M5).
        async def _on_error(exc: Exception) -> None:
            log.warning("nats error (%s): %s", self._name, exc, exc_info=exc)

        async def _on_disconnect() -> None:
            log.warning("nats disconnected (%s)", self._name)

        async def _on_reconnect() -> None:
            log.info("nats reconnected (%s)", self._name)

        async def _on_closed() -> None:
            log.error("nats connection CLOSED (%s) — no further delivery", self._name)

        auth: dict[str, str] = {}
        if pw_file := os.environ.get(_PASSWORD_FILE):
            try:
                password = await asyncio.to_thread(Path(pw_file).read_text)
                auth = {"user": self._user, "password": password.strip()}
            except OSError as exc:
                raise RuntimeError(f"bus password not readable at {pw_file} — is the keys service up?") from exc
        self._nc = await nats.connect(
            self._url,
            name=self._name,
            # Replies come back on this client's own inboxes: the server lets an
            # adapter listen on its own and nobody else's.
            inbox_prefix=f"_INBOX.{self._user}",
            **auth,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            error_cb=_on_error,
            disconnected_cb=_on_disconnect,
            reconnected_cb=_on_reconnect,
            closed_cb=_on_closed,
        )
        log.info("bus connected: %s as %r (%s)", _redact_url(self._url), self._name, self._user)

    async def ensure_streams(self) -> None:
        """Create/align the JetStream streams (idempotent; call at consumer boot).
        Publishers don't need this — but a consumer binding a durable needs the
        stream to exist, and the first boot on a fresh NATS store creates it."""
        js = self.nc.jetstream()
        for name, subjects in _STREAMS:
            cfg = StreamConfig(
                name=name,
                subjects=subjects,
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.OLD,
                max_age=_MAX_AGE_S,
                max_bytes=_MAX_BYTES,
            )
            try:
                await js.add_stream(cfg)
                log.info("jetstream stream %s ready (subjects=%s)", name, subjects)
            except nats.js.errors.BadRequestError:
                # Exists with an older config (e.g. changed limits) — align it.
                await js.update_stream(cfg)
                log.info("jetstream stream %s updated", name)

    async def consume_stream(
        self,
        *,
        stream: str,
        durable: str,
        filter_subject: str,
        decoder: msgspec.msgpack.Decoder,
        handler: Callable[[object], Awaitable[None]],
        claim: Callable[[Any], str] | None = None,
        batch: int = 64,
        nak_delay: float = 3.0,
    ) -> asyncio.Task:
        """Run a durable pull consumer as a background task (returned; cancel to
        stop). Ack discipline is exception-driven and is THE at-least-once core:

          * handler returns          → ack   (work committed — done forever)
          * handler raises           → nak(delay) (transient failure, e.g. DB
                                       down → redelivered until it succeeds)
          * message won't decode     → term  (poison frame; redelivery can never
                                       help — drop loudly, don't loop)
          * claims another publisher → term  (see `_forged`)
        """
        sub = await self._durable(stream, durable, filter_subject)
        settle = _settler(stream, durable)

        async def _loop() -> None:
            while True:
                for msg in await _fetch(sub, batch, stream, durable):
                    obj = await _admit(msg, decoder, claim, settle)
                    if obj is _REJECTED:
                        continue
                    if isinstance(obj, StateUpdate) and obj.received_ns is None:
                        obj = msgspec.structs.replace(
                            obj, received_ns=int(msg.metadata.timestamp.timestamp() * 1e9))
                    try:
                        await handler(obj)
                    except asyncio.CancelledError:
                        # Shutdown mid-message: leave it unacked; redelivered next boot.
                        raise
                    except Exception as exc:
                        line, delivered = _failure_line(msg)
                        log.error(line, msg.subject, delivered, exc, exc_info=exc)
                        await settle(lambda m=msg: m.nak(delay=nak_delay), "nak", msg.subject)
                        continue
                    await settle(msg.ack, "ack", msg.subject)

        # spawn() supervises: if _loop ever dies with an exception it logs a loud
        # ERROR (never silent). The caller keeps the handle to cancel at shutdown.
        return spawn(_loop(), log=log, name=f"js consumer {stream}/{durable}")

    async def consume_stream_batched(
        self,
        *,
        stream: str,
        durable: str,
        filter_subject: str,
        decoder: msgspec.msgpack.Decoder,
        handler: Callable[[list], Awaitable[None]],
        claim: Callable[[Any], str] | None = None,
        batch: int = 256,
        nak_delay: float = 3.0,
    ) -> asyncio.Task:
        """Same at-least-once contract as consume_stream, but the handler is given
        the WHOLE batch and the batch is acked together.

        For a sink whose cost is per-CALL rather than per-ROW — a ClickHouse insert
        is one HTTP round trip whether it carries 1 row or 500 — per-message acking
        turns a cheap flush into hundreds of them. Undecodable frames are still
        termed individually (one poison message must not take a batch down with
        it); a handler that raises naks everything that decoded, so the batch
        redelivers whole and the sink's block-hash dedup drops the duplicate.
        """
        sub = await self._durable(stream, durable, filter_subject)
        settle = _settler(stream, durable)

        async def _loop() -> None:
            while True:
                good, objs = [], []
                for msg in await _fetch(sub, batch, stream, durable):
                    obj = await _admit(msg, decoder, claim, settle)
                    if obj is not _REJECTED:
                        objs.append(obj)
                        good.append(msg)
                if not good:
                    continue
                try:
                    await handler(objs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("batch handler failed (%s/%s, %d msgs, will redeliver): %s",
                              stream, durable, len(good), exc, exc_info=exc)
                    for msg in good:
                        await settle(lambda m=msg: m.nak(delay=nak_delay), "nak", msg.subject)
                    continue
                for msg in good:
                    await settle(msg.ack, "ack", msg.subject)

        return spawn(_loop(), log=log, name=f"js batch consumer {stream}/{durable}")

    async def _durable(self, stream: str, durable: str, filter_subject: str):
        """Bind a pull subscription to `durable`, creating it with a bounded
        redelivery (max_deliver) on a fresh install. pull_subscribe leaves an
        existing durable's config alone, so a durable made when its subjects were
        shaped differently would go on filtering for the old shape and hear
        nothing, silently: its filter is realigned here."""
        js = self.nc.jetstream()
        config = ConsumerConfig(
            durable_name=durable,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=60,          # seconds; per-message, ample for a DB txn / CH insert
            max_deliver=_MAX_DELIVER,
            filter_subject=filter_subject,
        )
        try:
            info = await js.consumer_info(stream, durable)
        except nats.js.errors.NotFoundError:
            pass
        else:
            if info.config.filter_subject != filter_subject:
                await js.add_consumer(stream, config=dataclasses.replace(
                    info.config, filter_subject=filter_subject))
                log.warning("jetstream consumer %s/%s refiltered %s -> %s",
                            stream, durable, info.config.filter_subject, filter_subject)
        return await js.pull_subscribe(filter_subject, durable=durable, stream=stream, config=config)

    async def pending(self, stream: str, durable: str) -> int:
        """This durable's unprocessed backlog (observability; heartbeat lines)."""
        info = await self.nc.jetstream().consumer_info(stream, durable)
        return int(info.num_pending or 0)

    @property
    def nc(self) -> NATSClient:
        if self._nc is None:
            raise RuntimeError("Bus.connect() not called")
        return self._nc

    async def close(self) -> None:
        if self._beat is not None:
            self._beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beat
            self._beat = None
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

    # --- publish helpers ---------------------------------------------------

    async def publish_state(self, update: StateUpdate) -> None:
        await self.nc.publish(state_subject(update.adapter, update.entity_id), _encoder.encode(update))
        self.speaks_for(update.adapter)

    async def publish_entity(self, info: EntityInfo) -> None:
        """Announce an entity's existence/metadata (catalog), no value."""
        await self.nc.publish(entity_subject(info.adapter, info.entity_id), _encoder.encode(info))
        self.speaks_for(info.adapter)

    def speaks_for(self, adapter: str) -> None:
        """Whoever publishes for a namespace vouches for it from then on: a
        heartbeat goes out for every namespace this process has spoken for."""
        if adapter in self._speaks_for:
            return
        self._speaks_for.add(adapter)
        if self._beat is None:
            self._beat = spawn(self._heartbeat_loop(), log=log, name=f"heartbeat {self._name}")

    async def _heartbeat_loop(self, period: float = HEARTBEAT_S) -> None:
        while True:
            try:
                for adapter in sorted(self._speaks_for):
                    await self.nc.publish(heartbeat_subject(adapter), b"")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("heartbeat publish failed (%s): %s", self._name, exc, exc_info=True)
            await asyncio.sleep(period)

    async def subscribe_heartbeats(self, handler: Callable[[str], Awaitable[None]]) -> None:
        """Every namespace still spoken for, by name, as its heartbeats arrive."""

        async def _cb(msg: Msg) -> None:
            try:
                await handler(publisher_of(msg.subject))
            except Exception as exc:
                log.warning("bad heartbeat on %s: %s", msg.subject, exc, exc_info=True)

        await self.nc.subscribe(f"{HEARTBEAT_SUBJECT}.*", cb=_cb)

    async def publish_command(self, command: Command) -> None:
        if not command.source:
            # No command is anonymous in the audit trail: an unstamped publish
            # at least carries the publishing client's identity.
            command = msgspec.structs.replace(command, source=self._name)
        await self.nc.publish(command_subject(command.entity_id), _encoder.encode(command))

    @staticmethod
    def encode_event(update: StateUpdate) -> bytes:
        """The exact bytes an engine event puts on the wire. For a caller that must
        PERSIST an event before publishing it (the engine's transactional outbox) and
        hand the payload to publish_raw later."""
        return _encoder.encode(update)

    async def publish_journal(self, event: JournalEvent) -> None:
        """Record that something HAPPENED. Fire-and-forget from the caller's side:
        the journal is diagnostic, so a service must never fail its real work
        because an event couldn't be published."""
        await self.nc.publish(journal_subject(event.source), _encoder.encode(event))

    async def publish_log(self, record: LogRecord) -> None:
        await self.nc.publish(logs_subject(record.service), _encoder.encode(record))

    def encode_log(self, record: LogRecord) -> bytes:
        """Encode without publishing — the log handler runs on ARBITRARY threads
        and cannot await, so it hands the bytes to the loop rather than the bus."""
        return _encoder.encode(record)

    async def publish_reachability(self, event: ReachabilityEvent) -> None:
        """Announce a device's reachability. Core NATS, not JetStream: this is
        current status, not history — the latest verdict wins and a missed one is
        corrected by the next, so it needs no durable replay."""
        await self.nc.publish(reachability_subject(event.adapter), _encoder.encode(event))
        self.speaks_for(event.adapter)

    async def subscribe_reachability(
        self, handler: Callable[[ReachabilityEvent], Awaitable[None]]
    ) -> None:
        """Subscribe to every adapter's reachability verdicts (engine side)."""

        async def _cb(msg: Msg) -> None:
            try:
                event = _reachability_decoder.decode(msg.data)
            except msgspec.DecodeError as exc:
                log.warning("dropping undecodable reachability on %s: %s", msg.subject, exc)
                return
            if _forged(msg.subject, event.adapter):
                return
            await handler(event)

        await self.nc.subscribe(f"{REACHABILITY_SUBJECT}.*", cb=_cb)

    async def publish_raw(self, subject: str, payload: bytes) -> None:
        await self.nc.publish(subject, payload)

    async def flush(self, timeout_s: float = 5.0) -> None:
        """Block until everything published so far is actually on the wire. `publish`
        only buffers in the client, so a caller that deletes its durable copy of an
        event (the outbox relay) must flush FIRST or a crash would drop it."""
        await self.nc.flush(timeout=timeout_s)

    # --- subscribe helpers -------------------------------------------------

    async def subscribe_commands(
        self, handler: Callable[[Command], Awaitable[None]], namespace: str
    ) -> None:
        """Subscribe to the commands for one namespace (adapter side): an adapter
        never receives, let alone decodes, a command meant for another."""

        async def _cb(msg: Msg) -> None:
            try:
                command = _command_decoder.decode(msg.data)
            except msgspec.DecodeError as exc:
                log.warning("dropping undecodable command on %s: %s", msg.subject, exc)
                return
            if command_subject(command.entity_id) != msg.subject:
                log.error("dropping a command for %s sent on %s", command.entity_id, msg.subject)
                return
            await handler(command)

        await self.nc.subscribe(namespace_commands(namespace), cb=_cb)

    async def subscribe_events(
        self, handler: Callable[[StateUpdate], Awaitable[None]]
    ) -> None:
        """Subscribe to the validated engine events stream (api/automation side)."""

        async def _cb(msg: Msg) -> None:
            try:
                update = _state_decoder.decode(msg.data)
            except msgspec.DecodeError as exc:
                log.warning("dropping undecodable event: %s", exc)
                return
            await handler(update)

        await self.nc.subscribe(ENGINE_EVENTS_SUBJECT, cb=_cb)

    # --- durable (JetStream) consumers --------------------------------------
    # At-least-once counterparts of the subscribe_* helpers above, for the
    # services where a lost message = corrupted view of reality (engine) or a
    # missed trigger (automation). handler raising = nak + redelivery.

    async def consume_state_stream(
        self, handler: Callable[[StateUpdate], Awaitable[None]], *, durable: str
    ) -> asyncio.Task:
        return await self.consume_stream(
            stream=STATE_STREAM, durable=durable,
            filter_subject=f"{STATE_SUBJECT_PREFIX}.>",
            decoder=_state_decoder, handler=handler, claim=lambda u: u.adapter,
        )

    async def consume_entity_stream(
        self, handler: Callable[[EntityInfo], Awaitable[None]], *, durable: str
    ) -> asyncio.Task:
        return await self.consume_stream(
            stream=STATE_STREAM, durable=durable,
            filter_subject=f"{ENTITY_SUBJECT_PREFIX}.>",
            decoder=_entity_decoder, handler=handler, claim=lambda i: i.adapter,
        )

    async def consume_events_stream(
        self, handler: Callable[[StateUpdate], Awaitable[None]], *, durable: str
    ) -> asyncio.Task:
        return await self.consume_stream(
            stream=EVENTS_STREAM, durable=durable,
            filter_subject=ENGINE_EVENTS_SUBJECT,
            decoder=_state_decoder, handler=handler,
        )

    async def subscribe_journal(
        self, handler: Callable[[JournalEvent], Awaitable[None]]
    ) -> None:
        """Live copy of the journal, core NATS. Separate from the durable consumer
        the sink uses: this is for surfaces that want to SEE events happen (the API
        fanning them to open browsers), where a replay after a restart would push
        yesterday's outage onto a screen as if it were now."""

        async def _cb(msg: Msg) -> None:
            try:
                event = _journal_decoder.decode(msg.data)
            except msgspec.DecodeError as exc:
                log.warning("dropping undecodable journal event: %s", exc)
                return
            if _forged(msg.subject, event.source):
                return
            await handler(event)

        await self.nc.subscribe(f"{JOURNAL_SUBJECT}.*", cb=_cb)

    async def consume_log_stream(
        self,
        handler: Callable[[list[LogRecord]], Awaitable[None]],
        *,
        durable: str,
        batch: int = 256,
    ) -> asyncio.Task:
        """Durable consumer for application logs, delivered in BATCHES.

        Logs are the highest-volume of the three firehoses and ClickHouse hates
        per-row inserts, so unlike the journal this hands the handler a whole
        batch and acks the batch only once it returns — the same ack-after-durable
        contract, one round trip instead of hundreds."""
        return await self.consume_stream_batched(
            stream=LOGS_STREAM, durable=durable,
            filter_subject=f"{LOGS_SUBJECT}.>",
            decoder=_log_decoder, handler=handler,  # type: ignore[arg-type]
            claim=lambda r: r.service, batch=batch,
        )

    async def consume_journal_stream(
        self,
        handler: Callable[[JournalEvent], Awaitable[None]],
        *,
        durable: str,
    ) -> asyncio.Task:
        """Durable consumer for the device-event journal — at-least-once, acked
        after the row is durable, same discipline as the command audit."""
        return await self.consume_stream(
            stream=JOURNAL_STREAM, durable=durable,
            filter_subject=f"{JOURNAL_SUBJECT}.>",
            decoder=_journal_decoder, handler=handler,  # type: ignore[arg-type]
            claim=lambda e: e.source,
        )

    async def consume_command_stream(
        self, handler: Callable[[Command], Awaitable[None]], *, durable: str
    ) -> asyncio.Task:
        """Audit-side command consumer (engine). Adapters must NOT use this —
        they stay on the live core subscription (subscribe_commands), where a
        replayed command can never re-execute on a device."""
        return await self.consume_stream(
            stream=COMMANDS_STREAM, durable=durable,
            filter_subject=f"{COMMAND_SUBJECT_PREFIX}.>",
            decoder=_command_decoder, handler=handler,
        )
