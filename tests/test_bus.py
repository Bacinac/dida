"""Tests for the NATS bus wrapper (dida_core.bus) BEYOND the pure subject
builders already covered by test_bus_subjects.py.

Nothing here touches a real broker: a recording FAKE nats client is injected
into `Bus._nc`, so we exercise the wire codec + the publish/subscribe/consume
wiring that surrounds it. Covered:

  * msgspec msgpack encode → decode ROUNDTRIP for the three wire structs
    (StateUpdate / EntityInfo / Command), including the poison-frame reject path
    the callbacks depend on.
  * publish_* helpers — correct subject + an EXACTLY-decodable payload for each,
    plus publish_command's source-stamping (empty → client name, explicit kept).
  * subscribe_commands / subscribe_events — right subject registered, a delivered
    msg is decoded and dispatched, an undecodable frame is dropped (handler never
    runs, no raise).
  * consume_stream at-least-once settle discipline over a fake pull consumer:
    ack on success, nak(delay) on handler failure, term on an undecodable frame.
  * ensure_streams (add + BadRequestError→update align), pending backlog, the
    nc-before-connect RuntimeError, and close draining then clearing the client.

A LIVE broker is still required to test the real reconnection callbacks
(Bus.connect) and end-to-end JetStream delivery — those are integration surface,
not unit-testable here.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_bus.py"
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
from types import SimpleNamespace

import msgspec
import nats.js.errors
import pytest
from dida_core.bus import (
    COMMANDS_STREAM,
    EVENTS_STREAM,
    JOURNAL_STREAM,
    LOGS_STREAM,
    STATE_STREAM,
    Bus,
    _command_decoder,
    _encoder,
    _entity_decoder,
    _journal_decoder,
    _log_decoder,
    _state_decoder,
)
from dida_core.events import (
    ENGINE_EVENTS_SUBJECT,
    Command,
    EntityInfo,
    JournalEvent,
    LogRecord,
    ReachabilityEvent,
    StateUpdate,
    command_subject,
    entity_subject,
    journal_subject,
    logs_subject,
    reachability_subject,
    state_subject,
)

# 0xc1 is msgpack's "never used" opcode — a deterministic poison frame that no
# decoder can ever accept (verified: raises msgspec.DecodeError).
POISON = b"\xc1"


# --- fakes ------------------------------------------------------------------


class FakeNats:
    """Records publish/subscribe calls; hands out a per-test jetstream stub.

    Only the methods bus.py actually calls on the real NATS client are stubbed —
    the connection itself is never established."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[tuple[str, object]] = []
        self.drained = False
        self.js: object | None = None

    async def publish(self, subject, payload):
        self.published.append((subject, payload))

    async def subscribe(self, subject, cb=None):
        self.subscriptions.append((subject, cb))
        return object()

    async def drain(self):
        self.drained = True

    def jetstream(self):  # real nats-py returns the JS context synchronously
        return self.js


def make_bus(name: str = "test-svc") -> tuple[Bus, FakeNats]:
    bus = Bus("nats://localhost:4222", name=name, user="mqtt")
    fake = FakeNats()
    bus._nc = fake  # inject the fake connection past Bus.connect()
    return bus, fake


class FakeMsg:
    """The subset of nats.aio.msg.Msg the callbacks read (.subject / .data)."""

    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data


# --- sample wire objects ----------------------------------------------------


def a_state() -> StateUpdate:
    return StateUpdate(
        entity_id="mqtt:kitchen_light",
        capability="brightness",
        value=80,
        adapter="mqtt",
        ts_ns=1_700_000_000_000_000,
    )


def a_command(**kw) -> Command:
    base = {
        "entity_id": "mqtt:kitchen_light",
        "capability": "brightness",
        "command": "set_brightness",
        "ts_ns": 42,
    }
    base.update(kw)
    return Command(**base)


# --- codec roundtrips -------------------------------------------------------


def test_state_update_roundtrip_minimal():
    update = a_state()
    assert _state_decoder.decode(_encoder.encode(update)) == update, \
        "a minimal StateUpdate survives encode→decode identically"


def test_state_update_roundtrip_all_optionals():
    # omit_defaults=True drops defaults on the wire but restores them on decode —
    # so a fully-populated struct must still round-trip byte-for-value identical.
    update = StateUpdate(
        entity_id="ecowitt:outdoor",
        capability="temperature",
        value=21.5,
        adapter="ecowitt",
        ts_ns=99,
        unit="°C",
        name="Vanjski senzor",
        diagnostic=True,
        category="diagnostic",
        device="dev:weather",
        device_name="Meteo",
    )
    assert _state_decoder.decode(_encoder.encode(update)) == update


def test_entity_info_roundtrip():
    info = EntityInfo(
        entity_id="mqtt:kitchen_light",
        adapter="mqtt",
        capabilities=["on_off", "brightness"],
        name="Kitchen Light",
        device="dev:mqtt-1",
        device_name="Lamp",
        device_type="light",
    )
    assert _entity_decoder.decode(_encoder.encode(info)) == info


def test_command_roundtrip_with_args_and_source():
    cmd = a_command(args={"value": 80}, source="user:alex")
    assert _command_decoder.decode(_encoder.encode(cmd)) == cmd


def test_decoder_rejects_poison_frame():
    with pytest.raises(msgspec.DecodeError):
        _command_decoder.decode(POISON)


def test_decoder_rejects_wrong_shape():
    # A valid-msgpack but wrong-shape payload (missing required fields) must raise
    # a ValidationError — which subclasses DecodeError, the type the bus catches.
    with pytest.raises(msgspec.DecodeError):
        _state_decoder.decode(_encoder.encode({"not": "a state update"}))


# --- publish helpers --------------------------------------------------------


async def test_publish_state_subject_and_payload():
    bus, fake = make_bus()
    update = a_state()
    await bus.publish_state(update)
    assert len(fake.published) == 1
    subject, payload = fake.published[0]
    assert subject == state_subject("mqtt", "mqtt:kitchen_light") == "dida.state.mqtt.mqtt:kitchen_light"
    assert _state_decoder.decode(payload) == update, "payload is the encoded update"


async def test_publish_entity_subject_and_payload():
    bus, fake = make_bus()
    info = EntityInfo(entity_id="mqtt:kitchen_light", adapter="mqtt", capabilities=["on_off"])
    await bus.publish_entity(info)
    subject, payload = fake.published[0]
    assert subject == entity_subject("mqtt", "mqtt:kitchen_light") == "dida.entity.mqtt.mqtt:kitchen_light"
    assert _entity_decoder.decode(payload) == info


async def test_journal_reachability_and_logs_go_out_under_their_publisher():
    bus, fake = make_bus()
    await bus.publish_journal(JournalEvent(ts_ns=1, kind="online", source="adapter:mqtt"))
    await bus.publish_reachability(ReachabilityEvent(ts_ns=1, device_key="d", adapter="mqtt", reachable=True))
    await bus.publish_log(LogRecord(ts_ns=1, service="adapter:mqtt", level="INFO", logger="x", message="m"))
    assert [subject for subject, _ in fake.published] == [
        "dida.journal.mqtt", "dida.reachability.mqtt", "dida.logs.mqtt"]
    assert _log_decoder.decode(fake.published[2][1]).message == "m"


async def test_a_heartbeat_goes_out_for_every_namespace_spoken_for():
    bus, fake = make_bus()
    bus._speaks_for.update({"presence", "unifi"})
    task = asyncio.create_task(bus._heartbeat_loop(period=3600))
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert fake.published == [("dida.heartbeat.presence", b""), ("dida.heartbeat.unifi", b"")]


async def test_subscribe_heartbeats_hands_over_the_name_the_subject_carries():
    bus, fake = make_bus()
    heard: list[str] = []

    async def handler(adapter):
        heard.append(adapter)

    await bus.subscribe_heartbeats(handler)
    subject, cb = fake.subscriptions[0]
    assert subject == "dida.heartbeat.*"
    await cb(FakeMsg("dida.heartbeat.esphome", b""))
    assert heard == ["esphome"]


async def test_a_reachability_verdict_in_another_adapters_name_is_dropped():
    bus, fake = make_bus()
    heard: list[ReachabilityEvent] = []

    async def handler(event):
        heard.append(event)

    await bus.subscribe_reachability(handler)
    subject, cb = fake.subscriptions[0]
    assert subject == "dida.reachability.*"
    forged = ReachabilityEvent(ts_ns=1, device_key="d", adapter="shelly", reachable=False)
    await cb(FakeMsg(reachability_subject("esphome"), _encoder.encode(forged)))
    assert heard == [], "esphome may not speak for shelly's devices"
    await cb(FakeMsg(reachability_subject("shelly"), _encoder.encode(forged)))
    assert heard == [forged]


async def test_connect_logs_in_as_its_identity_with_the_keys_services_password(tmp_path, monkeypatch):
    (tmp_path / "nats").write_text("s3cret\n")
    monkeypatch.setenv("DIDA_NATS_PASSWORD_FILE", str(tmp_path / "nats"))
    seen: dict = {}

    async def fake_connect(url, **kw):
        seen.update(kw, url=url)
        return FakeNats()

    monkeypatch.setattr("dida_core.bus.nats.connect", fake_connect)
    bus = Bus("nats://nats:4222", name="dida-adapter-esphome", user="esphome")
    await bus.connect()
    assert (seen["user"], seen["password"]) == ("esphome", "s3cret")
    assert seen["inbox_prefix"] == "_INBOX.esphome", "replies come back where only it may listen"


async def test_connect_without_its_password_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("DIDA_NATS_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="bus password not readable"):
        await Bus("nats://nats:4222", name="x", user="esphome").connect()


async def test_publish_command_subject_and_payload():
    bus, fake = make_bus()
    cmd = a_command(source="user:alex")
    await bus.publish_command(cmd)
    subject, payload = fake.published[0]
    assert subject == command_subject("mqtt:kitchen_light") == "dida.command.mqtt.mqtt:kitchen_light"
    assert _command_decoder.decode(payload) == cmd


async def test_publish_command_stamps_source_when_empty():
    bus, fake = make_bus(name="api")
    await bus.publish_command(a_command())  # source defaults to ""
    decoded = _command_decoder.decode(fake.published[0][1])
    assert decoded.source == "api", "an unstamped command inherits the publishing client's name"
    assert decoded.command == "set_brightness", "the rest of the command is untouched"


async def test_publish_command_preserves_explicit_source():
    bus, fake = make_bus(name="api")
    await bus.publish_command(a_command(source="automation:7:night"))
    decoded = _command_decoder.decode(fake.published[0][1])
    assert decoded.source == "automation:7:night", "an explicit source is never overwritten"


async def test_publish_raw_is_passthrough():
    bus, fake = make_bus()
    await bus.publish_raw("dida.status.mqtt", b"opaque")
    assert fake.published == [("dida.status.mqtt", b"opaque")], \
        "publish_raw forwards subject+bytes verbatim, no encoding"


# --- subscribe wiring -------------------------------------------------------


async def test_an_adapter_subscribes_to_its_own_namespace_only():
    bus, fake = make_bus()
    received: list[Command] = []

    async def handler(cmd):
        received.append(cmd)

    await bus.subscribe_commands(handler, "mqtt")
    assert len(fake.subscriptions) == 1
    subject, cb = fake.subscriptions[0]
    assert subject == "dida.command.mqtt.>", "a command for another adapter never reaches this one"

    cmd = a_command(source="user:alex")
    await cb(FakeMsg("dida.command.mqtt.mqtt:kitchen_light", _encoder.encode(cmd)))
    assert received == [cmd], "a delivered frame is decoded and handed to the handler"


async def test_subscribe_commands_drops_undecodable():
    bus, fake = make_bus()
    received: list[Command] = []

    async def handler(cmd):
        received.append(cmd)

    await bus.subscribe_commands(handler, "mqtt")
    _, cb = fake.subscriptions[0]
    await cb(FakeMsg("dida.command.mqtt.mqtt:x", POISON))  # must NOT raise
    assert received == [], "an undecodable command is dropped, the handler never runs"


async def test_a_command_for_another_namespace_never_reaches_the_adapter(caplog):
    """A commander may send on a namespace it is granted with an entity from one
    it is not; the subject is what the server checked, so the entity must match it."""
    bus, fake = make_bus()
    received: list[Command] = []

    async def handler(cmd):
        received.append(cmd)

    await bus.subscribe_commands(handler, "mqtt")
    _, cb = fake.subscriptions[0]
    foreign = a_command(entity_id="shelly:kitchen")
    await cb(FakeMsg("dida.command.mqtt.shelly:kitchen", _encoder.encode(foreign)))
    await cb(FakeMsg("dida.command.mqtt.mqtt:hall", _encoder.encode(a_command(entity_id="mqtt:kitchen_light"))))
    own = a_command(entity_id="mqtt:hall")
    await cb(FakeMsg("dida.command.mqtt.mqtt:hall", _encoder.encode(own)))
    assert received == [own], "only the entity the subject names is handed over"
    assert "dropping a command for shelly:kitchen" in caplog.text


async def test_subscribe_events_registers_engine_subject_and_dispatches():
    bus, fake = make_bus()
    received: list[StateUpdate] = []

    async def handler(update):
        received.append(update)

    await bus.subscribe_events(handler)
    subject, cb = fake.subscriptions[0]
    assert subject == ENGINE_EVENTS_SUBJECT

    update = a_state()
    await cb(FakeMsg(ENGINE_EVENTS_SUBJECT, _encoder.encode(update)))
    assert received == [update]


async def test_subscribe_events_drops_undecodable():
    bus, fake = make_bus()
    received: list[StateUpdate] = []

    async def handler(update):
        received.append(update)

    await bus.subscribe_events(handler)
    _, cb = fake.subscriptions[0]
    await cb(FakeMsg(ENGINE_EVENTS_SUBJECT, POISON))
    assert received == [], "an undecodable event is dropped, the handler never runs"


# --- durable consume: the at-least-once settle discipline -------------------


class FakeJSMsg:
    """A JetStream msg exposing ack/nak/term so we can assert which fired."""

    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data
        self.acked = False
        self.nak_delay: float | None = None
        self.termed = False
        self.metadata = SimpleNamespace(timestamp=datetime.datetime.now(datetime.UTC))

    async def ack(self):
        self.acked = True

    async def nak(self, delay=None):
        self.nak_delay = delay

    async def term(self):
        self.termed = True


class FakePullSub:
    """Serves one batch, then blocks so the loop goes idle until the test cancels."""

    def __init__(self, first_batch: list, idle: asyncio.Event) -> None:
        self._first_batch = first_batch
        self._idle = idle
        self._served = False

    async def fetch(self, batch, timeout=None):
        if not self._served:
            self._served = True
            return self._first_batch
        self._idle.set()  # signal: the first batch is fully settled
        await asyncio.sleep(3600)  # park until task.cancel()


class FakeConsumeJS:
    def __init__(self, pull_sub: FakePullSub, existing=None) -> None:
        self._sub = pull_sub
        self._existing = existing
        self.pull_subscribe_args: tuple | None = None
        self.refiltered = None

    async def consumer_info(self, stream, durable):
        if self._existing is None:
            raise nats.js.errors.NotFoundError
        return SimpleNamespace(config=self._existing)

    async def add_consumer(self, stream, config=None):
        self.refiltered = config

    async def pull_subscribe(self, filter_subject, durable=None, stream=None, config=None):
        self.pull_subscribe_args = (filter_subject, durable, stream)
        self.pull_subscribe_config = config
        return self._sub


async def _run_consumer(bus, fake, *, decoder, handler, msgs):
    """Start consume_stream, wait until it has settled `msgs` and gone idle, then
    cancel and join. Returns the FakeConsumeJS for pull_subscribe assertions."""
    idle = asyncio.Event()
    js = FakeConsumeJS(FakePullSub(msgs, idle))
    fake.js = js
    task = await bus.consume_stream(
        stream=COMMANDS_STREAM,
        durable="audit",
        filter_subject="dida.command.>",
        decoder=decoder,
        handler=handler,
    )
    await asyncio.wait_for(idle.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return js


async def test_consume_stream_acks_on_success():
    bus, fake = make_bus()
    received: list[Command] = []

    async def handler(obj):
        received.append(obj)

    cmd = a_command(source="user:alex")
    msg = FakeJSMsg("dida.command.mqtt.mqtt:kitchen_light", _encoder.encode(cmd))
    js = await _run_consumer(bus, fake, decoder=_command_decoder, handler=handler, msgs=[msg])

    assert js.pull_subscribe_args == ("dida.command.>", "audit", COMMANDS_STREAM), \
        "the durable binds the requested subject/durable/stream"
    assert received == [cmd], "the decoded command reached the handler"
    assert msg.acked, "a handler that returns → ack (work committed)"
    assert msg.nak_delay is None and not msg.termed


async def test_consume_stream_stamps_when_the_bus_stored_a_state_update():
    bus, fake = make_bus()
    received: list[StateUpdate] = []

    async def handler(obj):
        received.append(obj)

    stored = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.UTC)
    fresh = FakeJSMsg("dida.state.peer", _encoder.encode(StateUpdate(
        entity_id="peer:door", capability="contact", value=True, adapter="peer", ts_ns=1)))
    fresh.metadata = SimpleNamespace(timestamp=stored)
    relayed = FakeJSMsg("dida.events", _encoder.encode(StateUpdate(
        entity_id="peer:door", capability="contact", value=False, adapter="peer", ts_ns=2,
        received_ns=5)))
    relayed.metadata = SimpleNamespace(timestamp=stored)
    await _run_consumer(bus, fake, decoder=_state_decoder, handler=handler, msgs=[fresh, relayed])

    assert received[0].received_ns == int(stored.timestamp() * 1e9)
    assert received[0].ts_ns == 1, "the source's own clock is kept as reported"
    assert received[1].received_ns == 5, "the engine's stamp survives the events stream"


async def test_consume_stream_naks_on_handler_failure():
    bus, fake = make_bus()

    async def handler(obj):
        raise RuntimeError("DB down")

    msg = FakeJSMsg("dida.command.mqtt.mqtt:x", _encoder.encode(a_command()))
    await _run_consumer(bus, fake, decoder=_command_decoder, handler=handler, msgs=[msg])

    assert msg.nak_delay == 3.0, "a raising handler → nak with the default delay (redeliver)"
    assert not msg.acked and not msg.termed


async def test_consume_stream_terms_undecodable():
    bus, fake = make_bus()
    received: list = []

    async def handler(obj):
        received.append(obj)

    msg = FakeJSMsg("dida.command.mqtt.mqtt:x", POISON)
    await _run_consumer(bus, fake, decoder=_command_decoder, handler=handler, msgs=[msg])

    assert msg.termed, "a poison frame → term (redelivery can never help)"
    assert not msg.acked and msg.nak_delay is None
    assert received == [], "the handler is never invoked for an undecodable frame"


# --- the typed consume_*_stream wrappers bind the right stream+subject ------
# Each is a thin delegator to consume_stream; drive it end-to-end (deliver one
# frame) and assert BOTH the pull_subscribe binding and that the frame decodes
# through the wrapper's chosen decoder into the handler.


async def _drive(fake, start, msgs, existing=None):
    idle = asyncio.Event()
    js = FakeConsumeJS(FakePullSub(msgs, idle), existing)
    fake.js = js
    task = await start()  # started only after fake.js is in place
    await asyncio.wait_for(idle.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return js


def _collector(received: list):
    async def handler(obj):
        received.append(obj)

    return handler


async def test_consume_state_stream_wrapper():
    bus, fake = make_bus()
    received: list[StateUpdate] = []
    update = a_state()
    js = await _drive(
        fake,
        lambda: bus.consume_state_stream(_collector(received), durable="engine"),
        [FakeJSMsg(state_subject(update.adapter, update.entity_id), _encoder.encode(update))],
    )
    assert js.pull_subscribe_args == ("dida.state.>", "engine", STATE_STREAM)
    assert [msgspec.structs.replace(r, received_ns=None) for r in received] == [update], \
        "decoded via the StateUpdate decoder"
    assert received[0].received_ns is not None, "stamped with the bus's receipt time"


async def test_consume_entity_stream_wrapper():
    bus, fake = make_bus()
    received: list[EntityInfo] = []
    info = EntityInfo(entity_id="mqtt:x", adapter="mqtt", capabilities=["on_off"])
    js = await _drive(
        fake,
        lambda: bus.consume_entity_stream(_collector(received), durable="engine"),
        [FakeJSMsg(entity_subject(info.adapter, info.entity_id), _encoder.encode(info))],
    )
    assert js.pull_subscribe_args == ("dida.entity.>", "engine", STATE_STREAM), \
        "entity stream shares the STATE stream but filters the dida.entity.> subject"
    assert received == [info], "decoded via the EntityInfo decoder"


async def test_a_state_published_under_another_name_is_terminated():
    # What the server enforces is the subject; a payload naming someone else was
    # made by hand to pass for them, and must never reach the engine.
    bus, fake = make_bus()
    received: list[StateUpdate] = []
    forged = StateUpdate(entity_id="shelly:x", capability="on_off", value=True, adapter="shelly", ts_ns=1)
    honest = a_state()
    msgs = [FakeJSMsg(state_subject("mqtt", forged.entity_id), _encoder.encode(forged)),
            FakeJSMsg(state_subject(honest.adapter, honest.entity_id), _encoder.encode(honest))]
    await _drive(fake, lambda: bus.consume_state_stream(_collector(received), durable="engine"), msgs)
    assert msgs[0].termed and not msgs[0].acked
    assert msgs[1].acked, "the honest twin in the same batch is delivered"
    assert [r.entity_id for r in received] == [honest.entity_id]


async def test_the_journal_consumer_drops_an_event_in_another_publishers_name():
    bus, fake = make_bus()
    received: list[JournalEvent] = []
    forged = JournalEvent(ts_ns=1, kind="offline", source="adapter:shelly")
    msgs = [FakeJSMsg(journal_subject("adapter:mqtt"), _encoder.encode(forged)),
            FakeJSMsg(journal_subject(forged.source), _encoder.encode(forged))]
    js = await _drive(fake, lambda: bus.consume_journal_stream(_collector(received), durable="sink"), msgs)
    assert js.pull_subscribe_args == ("dida.journal.>", "sink", JOURNAL_STREAM)
    assert msgs[0].termed and msgs[1].acked
    assert _journal_decoder.decode(msgs[1].data) == received[0]


async def test_the_log_consumer_drops_a_line_in_another_services_name():
    bus, fake = make_bus()
    batches: list[list[LogRecord]] = []

    async def handler(batch):
        batches.append(batch)

    forged = LogRecord(ts_ns=1, service="engine", level="ERROR", logger="x", message="fake")
    honest = LogRecord(ts_ns=1, service="adapter:mqtt", level="INFO", logger="x", message="real")
    msgs = [FakeJSMsg(logs_subject("adapter:mqtt"), _encoder.encode(forged)),
            FakeJSMsg(logs_subject(honest.service), _encoder.encode(honest))]
    js = await _drive(fake, lambda: bus.consume_log_stream(handler, durable="sink"), msgs)
    assert js.pull_subscribe_args == ("dida.logs.>", "sink", LOGS_STREAM)
    assert msgs[0].termed and msgs[1].acked
    assert batches == [[honest]]


async def test_a_durable_made_for_an_older_subject_shape_is_refiltered():
    from nats.js.api import ConsumerConfig
    bus, fake = make_bus()
    old = ConsumerConfig(durable_name="sink", filter_subject="dida.journal", max_deliver=8)
    js = await _drive(fake, lambda: bus.consume_journal_stream(_collector([]), durable="sink"), [], existing=old)
    assert js.refiltered is not None, "left alone it would filter for a subject nobody publishes"
    assert js.refiltered.filter_subject == "dida.journal.>"
    assert js.refiltered.max_deliver == 8, "only the filter changes"


async def test_a_durable_already_on_the_right_subject_is_left_alone():
    from nats.js.api import ConsumerConfig
    bus, fake = make_bus()
    cur = ConsumerConfig(durable_name="sink", filter_subject="dida.journal.>")
    js = await _drive(fake, lambda: bus.consume_journal_stream(_collector([]), durable="sink"), [], existing=cur)
    assert js.refiltered is None


async def test_consume_events_stream_wrapper():
    bus, fake = make_bus()
    received: list[StateUpdate] = []
    update = a_state()
    js = await _drive(
        fake,
        lambda: bus.consume_events_stream(_collector(received), durable="automation"),
        [FakeJSMsg(ENGINE_EVENTS_SUBJECT, _encoder.encode(update))],
    )
    assert js.pull_subscribe_args == (ENGINE_EVENTS_SUBJECT, "automation", EVENTS_STREAM)
    assert [msgspec.structs.replace(r, received_ns=None) for r in received] == [update]


async def test_consume_command_stream_wrapper():
    bus, fake = make_bus()
    received: list[Command] = []
    cmd = a_command(source="user:alex")
    js = await _drive(
        fake,
        lambda: bus.consume_command_stream(_collector(received), durable="audit"),
        [FakeJSMsg(command_subject("x"), _encoder.encode(cmd))],
    )
    assert js.pull_subscribe_args == ("dida.command.>", "audit", COMMANDS_STREAM)
    assert received == [cmd], "audit consumer decodes via the Command decoder"


# --- stream management + lifecycle ------------------------------------------


class RecordingStreamJS:
    def __init__(self, *, fail_add: bool = False) -> None:
        self._fail_add = fail_add
        self.added: list = []
        self.updated: list = []

    async def add_stream(self, cfg):
        if self._fail_add:
            raise nats.js.errors.BadRequestError(
                code=400, err_code=10058, description="stream name already in use"
            )
        self.added.append(cfg)

    async def update_stream(self, cfg):
        self.updated.append(cfg)


# Named explicitly, not derived from _STREAMS: deriving would make the assertion
# tautological (a stream silently DROPPED from the config would still pass). The
# second half cross-checks against _STREAMS so a stream ADDED without touching
# this test fails here instead of shipping uncovered.
ALL_STREAMS = {STATE_STREAM, EVENTS_STREAM, COMMANDS_STREAM, JOURNAL_STREAM, LOGS_STREAM}


def test_every_configured_stream_is_named_in_this_suite():
    from dida_core.bus import _STREAMS
    assert {name for name, _ in _STREAMS} == ALL_STREAMS, \
        "a new JetStream stream must be added to ALL_STREAMS, not left untested"


async def test_ensure_streams_creates_them_all():
    bus, fake = make_bus()
    js = RecordingStreamJS()
    fake.js = js
    await bus.ensure_streams()
    names = {cfg.name for cfg in js.added}
    assert names == ALL_STREAMS, "a fresh store gets every JetStream stream created"
    assert not js.updated


async def test_ensure_streams_aligns_existing_via_update():
    bus, fake = make_bus()
    js = RecordingStreamJS(fail_add=True)  # every add_stream says 'already exists'
    fake.js = js
    await bus.ensure_streams()
    assert not js.added
    names = {cfg.name for cfg in js.updated}
    assert names == ALL_STREAMS, \
        "a BadRequestError falls through to update_stream to align config"


async def test_pending_returns_backlog():
    bus, fake = make_bus()

    class _Info:
        num_pending = 7

    class _JS:
        args = None

        async def consumer_info(self, stream, durable):
            _JS.args = (stream, durable)
            return _Info()

    fake.js = _JS()
    assert await bus.pending(STATE_STREAM, "engine") == 7
    assert _JS.args == (STATE_STREAM, "engine")


async def test_pending_none_is_zero():
    bus, fake = make_bus()

    class _Info:
        num_pending = None

    class _JS:
        async def consumer_info(self, stream, durable):
            return _Info()

    fake.js = _JS()
    assert await bus.pending(STATE_STREAM, "engine") == 0, "a None backlog reads as 0"


def test_nc_property_raises_before_connect():
    bus = Bus("nats://localhost:4222", name="x", user="x")
    with pytest.raises(RuntimeError, match="connect"):
        _ = bus.nc


async def test_close_drains_and_clears_client():
    bus, fake = make_bus()
    await bus.close()
    assert fake.drained, "close drains the underlying connection"
    with pytest.raises(RuntimeError):
        _ = bus.nc  # the client is gone after close


async def test_close_is_noop_when_never_connected():
    bus = Bus("nats://localhost:4222", name="x", user="x")
    await bus.close()  # must not raise even though no connection was ever made
