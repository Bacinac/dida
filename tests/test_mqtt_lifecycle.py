"""The MQTT adapter's connection, which 296 of this house's entities hang off.

The mapping tests next door prove a zigbee2mqtt payload becomes the right
capability. They say nothing about whether the payload arrives. That is this
file: connect, notice the connection died, reconnect, and tear the old one down
before the new one starts — the part that decides whether those 296 entities
exist at all after the broker hiccups.

Two of the properties here are scars. A failed connect must leave the supervisor
wanting to connect, whether it failed at boot or on a reconnect after a live
connection dropped — both once left the house's zigbee lights unreachable until
someone restarted the container by hand (the second time on 3 October 2026,
when a mosquitto recreate made the first retry miss its DNS name). And `_apply`
must await the old consume task before opening the new client, because two loops
on the same subscription briefly coexisting means frames go to the one that is
about to be thrown away.

The whole lifecycle is a supervisor tick: read config, compute a connection key,
and act only if the key changed or there is no live consumer. Everything below is that
sentence, taken apart.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import typing

import pytest
from dida_adapter_mqtt.adapter import MqttAdapter

PREFIX = "zigbee2mqtt"


class _Cfg:
    """Stands in for AdapterConfig — the values the UI writes."""

    def __init__(self, **vals) -> None:
        self.vals = {"mqtt_url": "mqtt://mosquitto:1883", "topic_prefix": PREFIX, **vals}
        self.loads = 0

    async def load(self) -> None:
        self.loads += 1

    def get(self, key, default=""):
        return self.vals.get(key, default)

    def int(self, key, default=0):
        return int(self.vals.get(key, default))


class _Msg:
    def __init__(self, topic, payload) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class _FakeClient:
    """An aiomqtt client that records what happened to it."""

    instances: typing.ClassVar[list] = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.subscribed: list[str] = []
        self.entered = self.exited = False
        self.fail_connect = False
        self.queue: list = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        if self.fail_connect:
            raise ConnectionRefusedError("broker down")
        self.entered = True
        return self

    async def __aexit__(self, *a):
        self.exited = True

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    @property
    def messages(self):
        async def _gen():
            for m in self.queue:
                yield m
            # Stay open unless the test drained it, so a consume task does not
            # "die" merely because the fixture ran out of frames.
            await asyncio.sleep(3600)
        return _gen()


@pytest.fixture(autouse=True)
def fake_aiomqtt(monkeypatch):
    _FakeClient.instances.clear()
    mod = types.ModuleType("aiomqtt")
    mod.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "aiomqtt", mod)
    return mod


class _Status:
    """What `adapter_runner` attaches at startup — the adapter never builds its
    own, so a bare instance has none. Recorded here rather than stubbed silently:
    the badge IS the onboarding signal, so which state gets set matters."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def idle(self, detail=""):
        self.calls.append(("idle", detail))

    def connecting(self, detail=""):
        self.calls.append(("connecting", detail))

    def ok(self, detail=""):
        self.calls.append(("ok", detail))

    def error(self, detail=""):
        self.calls.append(("error", detail))

    def last(self):
        return self.calls[-1] if self.calls else (None, None)


def _adapter(cfg=None):
    a = MqttAdapter()
    a._cfg = cfg or _Cfg()
    a._bus = object()
    a.status = _Status()
    return a


# --- what counts as "the same connection" ----------------------------------------


def test_the_same_settings_produce_the_same_key():
    """The supervisor ticks every ten seconds. If an unchanged config produced a
    new key, every tick would tear down a working connection and 296 entities
    would flap for as long as the adapter ran."""
    a = _adapter()
    assert a._conn_key() == a._conn_key()


@pytest.mark.parametrize("field,value", [
    ("mqtt_url", "mqtt://other-broker:1883"),
    ("mqtt_url", "mqtt://mosquitto:8883"),
    ("username", "marko"),
    ("password", "s3cret"),
    ("topic_prefix", "z2m"),
])
def test_changing_any_connection_setting_changes_the_key(field, value):
    """Each of these needs a new socket or a new subscription. A key that ignored
    one would leave the adapter connected to the old broker while Settings shows
    the new one."""
    before = _adapter()._conn_key()
    after = _adapter(_Cfg(**{field: value}))._conn_key()
    assert before != after


def test_an_unset_broker_is_no_connection_at_all():
    """Not an error: a fresh installation has not been told where the broker is."""
    assert _adapter(_Cfg(mqtt_url=""))._conn_key() is None
    assert _adapter(_Cfg(mqtt_url="   "))._conn_key() is None


def test_a_url_without_a_host_is_no_connection():
    """`mqtt://` parses cleanly and names nothing. Treating it as configured would
    put the adapter in a connect-fail loop instead of saying it is unconfigured."""
    assert _adapter(_Cfg(mqtt_url="mqtt://"))._conn_key() is None


def test_the_default_port_and_prefix_are_applied():
    a = _adapter(_Cfg(mqtt_url="mqtt://broker"))
    host, port, _u, _p, prefix = a._conn_key()
    assert (host, port, prefix) == ("broker", 1883, PREFIX)


# --- connecting ------------------------------------------------------------------


async def test_a_successful_connect_subscribes_to_the_prefix():
    a = _adapter()
    await a._apply(a._conn_key())
    client = _FakeClient.instances[-1]
    assert client.entered
    assert client.subscribed == [f"{PREFIX}/#"]
    assert a._active_key == a._conn_key()
    assert a.status.last()[0] == "ok", "a live broker must show as connected"
    a._consume_task.cancel()


async def test_the_credentials_reach_the_client():
    a = _adapter(_Cfg(username="marko", password="s3cret"))
    await a._apply(a._conn_key())
    assert _FakeClient.instances[-1].kw["username"] == "marko"
    assert _FakeClient.instances[-1].kw["password"] == "s3cret"
    a._consume_task.cancel()


async def test_a_FAILED_connect_at_boot_is_retried(monkeypatch):
    """The first scar: a broker that was down at boot stayed disconnected until
    somebody restarted the container."""
    async def _boom(self):
        raise ConnectionRefusedError("down")
    monkeypatch.setattr(_FakeClient, "__aenter__", _boom)
    a = _adapter()
    key = a._conn_key()
    await a._apply(key)
    assert a._client is None
    assert a._consume_task is None
    assert a._needs_apply(key), "a failed connect at boot wedged the supervisor"


async def test_a_FAILED_reconnect_after_a_live_connection_is_retried(monkeypatch):
    """The second scar (3 October 2026). The connection was live, so the active key
    already equalled the configured one; the broker went away, the first retry
    failed, and with no consume task left nothing ever looked dead again."""
    a = _adapter()
    key = a._conn_key()
    await a._apply(key)
    a._consume_task.cancel()
    await asyncio.gather(a._consume_task, return_exceptions=True)
    assert a._needs_apply(key), "a lost connection was not noticed"

    async def _boom(self):
        raise OSError(-2, "Name or service not known")
    monkeypatch.setattr(_FakeClient, "__aenter__", _boom)
    await a._apply(key)
    assert a._needs_apply(key), "a failed reconnect wedged the supervisor"


async def test_a_live_connection_is_left_alone():
    """Ticking every ten seconds, a supervisor that reapplied a healthy connection
    would flap every zigbee entity in the house."""
    a = _adapter()
    key = a._conn_key()
    await a._apply(key)
    assert not a._needs_apply(key)
    a._consume_task.cancel()


async def test_an_unconfigured_broker_is_not_retried():
    a = _adapter(_Cfg(mqtt_url=""))
    await a._apply(None)
    assert not a._needs_apply(None)


async def test_a_changed_broker_is_applied():
    a = _adapter()
    await a._apply(a._conn_key())
    assert a._needs_apply(_adapter(_Cfg(mqtt_url="mqtt://other-broker:1883"))._conn_key())
    a._consume_task.cancel()


async def test_a_failed_connect_closes_the_half_open_client(monkeypatch):
    """Otherwise every retry leaks a socket, once per ten seconds, for as long as
    the broker is down."""
    async def _boom(self):
        raise ConnectionRefusedError("down")
    monkeypatch.setattr(_FakeClient, "__aenter__", _boom)
    a = _adapter()
    await a._apply(a._conn_key())
    assert _FakeClient.instances[-1].exited


async def test_a_failed_connect_is_visible_as_an_error(monkeypatch):
    """Silence here reads as "connected" on the adapters page."""
    a = _adapter()

    async def _boom(self):
        raise ConnectionRefusedError("broker down")
    monkeypatch.setattr(_FakeClient, "__aenter__", _boom)
    await a._apply(a._conn_key())
    state, detail = a.status.last()
    assert state == "error" and "connect failed" in detail


async def test_no_broker_configured_is_idle_not_error():
    """A fresh install has not been told where the broker is; a red badge there
    sends someone looking for a fault that does not exist."""
    a = _adapter(_Cfg(mqtt_url=""))
    await a._apply(None)
    state, detail = a.status.last()
    assert state == "idle" and "no broker" in detail
    assert a._active_key is None


# --- reconnecting ----------------------------------------------------------------


async def test_the_old_consumer_is_awaited_before_the_new_client_opens():
    """Two consume loops on the same subscription must never coexist: frames would
    go to the one about to be discarded, and those frames are device state."""
    a = _adapter()
    await a._apply(a._conn_key())
    first_task, first_client = a._consume_task, _FakeClient.instances[-1]

    await a._apply(a._conn_key())
    assert first_task.done(), "the previous consume task was left running"
    assert first_client.exited, "the previous client was left open"
    assert a._consume_task is not first_task
    a._consume_task.cancel()


async def test_reconnecting_replaces_the_client_rather_than_reusing_it():
    a = _adapter()
    await a._apply(a._conn_key())
    first = _FakeClient.instances[-1]
    await a._apply(a._conn_key())
    assert _FakeClient.instances[-1] is not first
    a._consume_task.cancel()


async def test_a_dead_consumer_is_what_makes_the_supervisor_reconnect():
    """The connection key does not change when a broker drops — the settings are
    still the same. The only signal is that the consume task finished, and that is
    the condition the supervisor loop tests."""
    a = _adapter()
    await a._apply(a._conn_key())
    task = a._consume_task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    dead = a._consume_task is not None and a._consume_task.done()
    assert dead, "a finished consume task must be observable as dead"


async def test_a_lost_connection_is_reported_not_swallowed():
    """"Connection lost" on the badge is what distinguishes a quiet house from a
    disconnected adapter — both look like no state arriving."""
    a = _adapter()

    async def _boom():
        raise OSError("broker went away")
    a._consume_loop = _boom
    a._client = object()
    await a._consume()
    state, detail = a.status.last()
    assert state == "error" and "connection lost" in detail


async def test_a_cancelled_consumer_does_not_report_a_fault():
    """Cancellation is how a deliberate reconnect starts. Logging it as an error
    would make every settings change look like a failure."""
    a = _adapter()

    async def _cancelled():
        raise asyncio.CancelledError
    a._consume_loop = _cancelled
    a._client = object()
    with pytest.raises(asyncio.CancelledError):
        await a._consume()
    assert not [c for c in a.status.calls if c[0] == "error"]


# --- which topics are state ------------------------------------------------------


async def _drain(a, msgs):
    """Run the consume loop over a fixed set of frames and collect what it ingested."""
    seen = {"devices": [], "state": []}

    async def _ingest(raw):
        seen["devices"].append(raw)
    a._ingest_devices = _ingest

    class _C:
        @property
        def messages(self):
            async def _gen():
                for m in msgs:
                    yield m
            return _gen()
    a._client = _C()
    a._prefix = PREFIX
    # The loop inlines state handling, so the bus is where the result shows up.
    class _Bus:
        async def publish_state(self, u):
            seen["state"].append(u)

        async def publish_entity(self, i):
            seen.setdefault("info", []).append(i)
    a._bus = _Bus()
    await a._consume_loop()
    return seen


async def test_the_device_list_topic_is_routed_to_the_device_ingest():
    """`bridge/devices` is how the adapter learns what exists at all — including
    the ieee_address that makes a rename a rename instead of a duplicate."""
    a = _adapter()
    seen = await _drain(a, [_Msg(f"{PREFIX}/bridge/devices", [{"friendly_name": "x"}])])
    assert len(seen["devices"]) == 1


@pytest.mark.parametrize("topic", [
    f"{PREFIX}/bridge",
    f"{PREFIX}/bridge/state",
    f"{PREFIX}/lamp/availability",
    f"{PREFIX}/lamp/set",
    f"{PREFIX}/",
    "othertopic/lamp",
    "homeassistant/sensor/x/config",
])
async def test_topics_that_are_not_device_state_are_skipped(topic):
    """`/set` is DIDA's own command echoing back. Reading it as state would make
    the adapter believe its own instruction before the device confirmed it — and
    on a device that fails to act, the house would show the wrong thing forever."""
    a = _adapter()
    seen = await _drain(a, [_Msg(topic, {"state": "ON"})])
    assert seen["state"] == []
    assert seen["devices"] == []


async def test_a_plain_device_topic_is_state():
    a = _adapter()
    seen = await _drain(a, [_Msg(f"{PREFIX}/lamp", {"state": "ON"})])
    assert seen["state"], "a device state frame was skipped"


# --- shutting down ---------------------------------------------------------------


async def test_stop_closes_the_consumer_and_the_client():
    """The container gets SIGTERM on every deploy, and there were 30-odd of those
    today. A client left open holds the broker session until it times out."""
    a = _adapter()
    await a._apply(a._conn_key())
    client = _FakeClient.instances[-1]
    await a.stop()
    assert a._consume_task is None
    assert a._client is None
    assert client.exited


# --- a command the device refused ------------------------------------------------
#
# Found live on the Cabin installation: three ceiling lights, nine presses, and the
# only record anywhere was inside zigbee2mqtt's own container log. DIDA published
# the command correctly, zigbee2mqtt accepted it correctly, and the Zigbee radio
# got no acknowledgement from the bulbs — so no new state was ever reported and
# DIDA kept showing the last one it knew, which was three hours old and said "on".
#
# DIDA was not lying: the lights really were on. What it never did was say that
# the instruction had been refused. zigbee2mqtt announces exactly that, as prose,
# on its own log topic — the one topic the adapter had no reason to read.


def _log(message, level="error"):
    return _Msg(f"{PREFIX}/bridge/logging", {"level": level, "message": message})


_REAL = ("z2m: Publish 'set' 'state' to 'Ceiling Upstairs' failed: 'Error: ZCL command "
         "0x60b647fffea277e9/1 genOnOff.off({}, {\"timeout\":10000}) failed "
         "(Data request failed with error: 'MAC_NO_ACK' (0xe9))'")


async def _journalled(a, msgs):
    events = []

    class _Bus:
        async def publish_state(self, u):
            pass

        async def publish_entity(self, i):
            pass
    a._bus = _Bus()

    class _C:
        @property
        def messages(self):
            async def _gen():
                for m in msgs:
                    yield m
            return _gen()
    a._client = _C()
    a._prefix = PREFIX
    import dida_adapter_mqtt.adapter as mod

    async def _emit(bus, kind, **kw):
        events.append({"kind": kind, **kw})
    real, mod.emit_journal = mod.emit_journal, _emit
    try:
        await a._consume_loop()
    finally:
        mod.emit_journal = real
    return events


async def test_a_refused_command_lands_on_the_device_that_refused_it():
    """The timeline of that light is where a person looks when the light did not
    change. Anywhere else is somewhere they will not look."""
    ev = await _journalled(_adapter(), [_log(_REAL)])
    assert len(ev) == 1
    assert ev[0]["entity_id"] == "mqtt:ceiling_upstairs"
    assert ev[0]["severity"] == "error"


async def test_the_reason_is_a_sentence_not_four_hundred_characters_of_zcl():
    """The raw text names a cluster, a timeout struct and an error code. What the
    reader needs is which device and why."""
    ev = await _journalled(_adapter(), [_log(_REAL)])
    assert ev[0]["message"] == "Ceiling Upstairs: device did not respond"


@pytest.mark.parametrize("code,expect", [
    ("MAC_NO_ACK", "device did not respond"),
    ("NO_NETWORK_ROUTE", "no route to the device"),
    ("MAC_CHANNEL_ACCESS_FAILURE", "the radio channel was busy"),
    ("MAC_TRANSACTION_EXPIRED", "the device did not wake in time"),
    ("Timeout waiting for response", "timed out"),
])
async def test_each_radio_failure_is_named_in_words(code, expect):
    """These send a person to different places: a flat battery, a hole in the mesh,
    a neighbour's microwave. "Failed" sends them nowhere."""
    ev = await _journalled(_adapter(), [_log(
        f"z2m: Publish 'set' 'state' to 'Lamp' failed: 'Error: ... ({code}) ...'")])
    assert ev[0]["message"] == f"Lamp: {expect}"


async def test_an_unrecognised_reason_is_passed_through_truncated():
    """A code nobody has seen yet must still reach the person, just bounded."""
    ev = await _journalled(_adapter(), [_log(
        "z2m: Publish 'set' 'state' to 'Lamp' failed: '" + "X" * 400 + "'")])
    assert ev[0]["message"].startswith("Lamp: XXX")
    assert len(ev[0]["message"]) < 200


@pytest.mark.parametrize("entry", [
    ("z2m: Publish 'set' 'state' to 'Lamp' failed: boom", "info"),
    ("z2m: Publish 'set' 'state' to 'Lamp' failed: boom", "warning"),
])
async def test_only_errors_are_journalled(entry):
    """zigbee2mqtt narrates itself on this topic at every level. A timeline that
    filled with routine chatter is one nobody reads."""
    msg, level = entry
    assert await _journalled(_adapter(), [_log(msg, level=level)]) == []


@pytest.mark.parametrize("message", [
    "z2m: Connected to MQTT server",
    "z2m: Device 'Lamp' left the network",
    "z2m: Failed to ping 'Lamp'",
    "",
])
async def test_log_lines_that_are_not_a_refused_command_are_left_alone(message):
    """This is zigbee2mqtt talking about itself. It belongs in its container log,
    not on a device's timeline."""
    assert await _journalled(_adapter(), [_log(message)]) == []


async def test_a_malformed_log_frame_does_not_stop_the_consume_loop():
    """The loop carries every device's state. A bad frame on the log topic must
    not be able to end it."""
    ev = await _journalled(_adapter(), [
        _Msg(f"{PREFIX}/bridge/logging", b"not json"),
        _Msg(f"{PREFIX}/bridge/logging", b'"a string"'),
        _log(_REAL),
    ])
    assert len(ev) == 1
