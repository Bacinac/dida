"""Zigbee onboarding + mesh diagnostics over MQTT — the control channel that lets
DIDA pair a device, map the mesh, and rename/remove without the z2m console.

The value here is the request/response correlation and the event translation: a
bridge request must reach z2m with the right payload, its async answer must be
matched back to the right waiter by transaction (never crossed, never hung), and
a join/interview/leave event must land on the right device's timeline. These are
stubbed against a fake broker on purpose — the wiring is the logic, and a real
z2m would only prove aiomqtt works.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from dida_adapter_mqtt.adapter import MqttAdapter
from dida_adapter_mqtt.mapping import shape_networkmap


class _Client:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, payload) -> None:
        self.published.append((topic, payload))


class _NC:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject, data) -> None:
        self.published.append((subject, data))


class _Bus:
    def __init__(self) -> None:
        self.nc = _NC()
        self.journals: list = []
        self.reach: list = []

    async def publish_journal(self, event) -> None:
        self.journals.append(event)

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)


class _Msg:
    def __init__(self, req: dict, reply: str = "_INBOX.reply") -> None:
        self.data = json.dumps(req).encode()
        self.reply = reply


def _adapter(*, connected=True, native=None) -> MqttAdapter:
    a = MqttAdapter()
    a._bus = _Bus()
    a._prefix = "zigbee2mqtt"
    a._client = _Client() if connected else None
    a._native = native or {}
    return a


async def _drive(a: MqttAdapter, msg: _Msg) -> asyncio.Task:
    """Run _on_ctl until it has published its request and is waiting on the answer."""
    task = asyncio.create_task(a._on_ctl(msg))
    for _ in range(100):
        await asyncio.sleep(0)
        if a._client and a._client.published:
            return task
    return task


def _reply(a: MqttAdapter) -> dict:
    return json.loads(a._bus.nc.published[-1][1])


# --- permit-join -----------------------------------------------------------------


async def test_permit_join_opens_pairing_for_the_requested_time():
    a = _adapter()
    task = await _drive(a, _Msg({"action": "permit_join", "on": True, "seconds": 120}))
    topic, payload = a._client.published[-1]
    assert topic == "zigbee2mqtt/bridge/request/permit_join"
    body = json.loads(payload)
    assert body["time"] == 120, "the pairing window is opened for the asked duration"
    a._resolve_response(json.dumps(
        {"transaction": body["transaction"], "status": "ok", "data": {"time": 120}}).encode())
    await task
    assert _reply(a)["status"] == "ok"


async def test_permit_join_off_closes_the_window():
    a = _adapter()
    task = await _drive(a, _Msg({"action": "permit_join", "on": False}))
    body = json.loads(a._client.published[-1][1])
    assert body["time"] == 0, "closing pairing is time=0, not a missing field"
    a._resolve_response(json.dumps({"transaction": body["transaction"], "status": "ok"}).encode())
    await task


# --- network map -----------------------------------------------------------------


async def test_networkmap_scans_in_the_background_and_caches_a_shaped_graph():
    """The scan outlives a request timeout, so a refresh kicks it off and returns
    'scanning'; a later read returns the cached, shaped map — not a 100s hang."""
    a = _adapter(native={"kuhinja": "0xAA"})
    await a._on_ctl(_Msg({"action": "networkmap", "refresh": True}))
    started = _reply(a)
    assert started["scanning"] is True and started["map"] is None
    for _ in range(100):
        await asyncio.sleep(0)
        if a._client.published:
            break
    topic, payload = a._client.published[-1]
    assert topic == "zigbee2mqtt/bridge/request/networkmap"
    body = json.loads(payload)
    assert body["type"] == "raw", "raw is what we render ourselves"
    a._resolve_response(json.dumps({
        "transaction": body["transaction"], "status": "ok",
        "data": {"type": "raw", "value": {
            "nodes": [{"ieeeAddr": "0xAA", "friendlyName": "kuhinja", "type": "Router"}],
            "links": [{"source": {"ieeeAddr": "0xAA"}, "target": {"ieeeAddr": "0x00"}, "lqi": 200}],
        }},
    }).encode())
    await a._netmap_task
    await a._on_ctl(_Msg({"action": "networkmap"}))  # plain read, no refresh
    res = _reply(a)
    assert res["scanning"] is False
    assert res["map"]["nodes"][0]["slug"] == "kuhinja", "a known ieee ties the node to our device"
    assert res["map"]["nodes"][0]["type"] == "router"
    assert res["map"]["links"][0]["lqi"] == 200


async def test_a_refresh_while_scanning_does_not_start_a_second_scan():
    a = _adapter()
    await a._on_ctl(_Msg({"action": "networkmap", "refresh": True}))
    for _ in range(100):
        await asyncio.sleep(0)
        if a._client.published:
            break
    await a._on_ctl(_Msg({"action": "networkmap", "refresh": True}))
    assert len(a._client.published) == 1, "a scan already in flight is not doubled"
    a._netmap_task.cancel()


# --- availability setting (configure the Zigbee bridge from the UI) ---------------


def test_bridge_info_learns_the_availability_setting():
    a = _adapter()
    a._ingest_info(json.dumps({"config": {"availability": {"enabled": True}}}).encode())
    assert a._z_avail is True
    a._ingest_info(json.dumps({"config": {"availability": {"enabled": False}}}).encode())
    assert a._z_avail is False


async def test_reading_availability_returns_the_known_setting():
    a = _adapter()
    a._z_avail = True
    await a._on_ctl(_Msg({"action": "availability"}))
    assert _reply(a)["enabled"] is True


async def test_setting_availability_sends_options_and_caches_it():
    a = _adapter()
    task = await _drive(a, _Msg({"action": "set_availability", "enabled": True}))
    topic, payload = a._client.published[-1]
    assert topic == "zigbee2mqtt/bridge/request/options"
    body = json.loads(payload)
    assert body["options"]["availability"]["enabled"] is True
    a._resolve_response(json.dumps({"transaction": body["transaction"], "status": "ok"}).encode())
    await task
    assert a._z_avail is True, "the applied setting is cached so a read reflects it at once"


async def test_a_refused_availability_change_does_not_cache():
    a = _adapter()
    task = await _drive(a, _Msg({"action": "set_availability", "enabled": True}))
    body = json.loads(a._client.published[-1][1])
    a._resolve_response(json.dumps(
        {"transaction": body["transaction"], "status": "error", "error": "nope"}).encode())
    await task
    assert a._z_avail is None, "a rejected change must not look applied"


# --- correlation, timeout, broker-down --------------------------------------------


async def test_two_requests_do_not_cross_wires():
    """Concurrent requests are matched by transaction, so the wrong answer can't
    resolve the wrong call — the reason we stamp a transaction at all."""
    a = _adapter()
    t1 = asyncio.create_task(a._zreq("permit_join", {"time": 1}, 5))
    t2 = asyncio.create_task(a._zreq("permit_join", {"time": 2}, 5))
    for _ in range(100):
        await asyncio.sleep(0)
        if len(a._client.published) >= 2:
            break
    txn1 = json.loads(a._client.published[0][1])["transaction"]
    txn2 = json.loads(a._client.published[1][1])["transaction"]
    assert txn1 != txn2
    a._resolve_response(json.dumps({"transaction": txn2, "status": "ok", "n": 2}).encode())
    assert (await t2)["n"] == 2
    assert not t1.done(), "the other call is still waiting for its own answer"
    a._resolve_response(json.dumps({"transaction": txn1, "status": "ok", "n": 1}).encode())
    assert (await t1)["n"] == 1


async def test_a_request_that_is_never_answered_times_out_loud():
    a = _adapter()
    with pytest.raises(TimeoutError):
        await a._zreq("networkmap", {"type": "raw"}, 0.05)
    assert a._pending == {}, "the waiter is cleaned up, not leaked"


async def test_a_ctl_action_with_no_broker_is_a_loud_error_not_a_hang():
    a = _adapter(connected=False)
    await a._on_ctl(_Msg({"action": "permit_join", "on": True}))
    assert "not connected" in _reply(a)["error"]


async def test_an_unknown_action_is_reported():
    a = _adapter()
    await a._on_ctl(_Msg({"action": "nonsense"}))
    assert _reply(a)["error"] == "unknown action"


async def test_a_stray_response_resolves_nothing():
    a = _adapter()
    a._resolve_response(json.dumps({"transaction": "ghost", "status": "ok"}).encode())  # must not raise
    a._resolve_response(b"not json")


# --- availability → reachability (the silent-device fix) --------------------------


async def test_availability_offline_reports_the_device_unreachable():
    """A Zigbee device that stops acknowledging is the exact case that left DIDA
    showing a light 'on' forever. z2m's availability topic is the one signal for
    it; offline must flip the device to unreachable via the reachability bus."""
    a = _adapter()
    await a._ingest_availability("Ceiling Upstairs", json.dumps({"state": "offline"}).encode())
    assert len(a._bus.reach) == 1
    ev = a._bus.reach[0]
    assert ev.reachable is False
    assert ev.device_key == "ceiling_upstairs" and ev.adapter == "mqtt"


async def test_availability_online_reports_the_device_reachable():
    a = _adapter()
    await a._ingest_availability("Bulb", json.dumps({"state": "online"}).encode())
    assert a._bus.reach[0].reachable is True


async def test_availability_accepts_the_legacy_plain_payload():
    """Older z2m (or legacy_availability_payload) publishes a bare 'online'/'offline'
    string, not JSON. Both shapes must land the same verdict."""
    a = _adapter()
    await a._ingest_availability("Bulb", b"offline")
    assert a._bus.reach[0].reachable is False


async def test_availability_is_edge_triggered():
    """z2m re-publishes availability retained and on every change; only a real
    transition is a verdict, or the timeline and bus would see a heartbeat."""
    a = _adapter()
    await a._ingest_availability("Bulb", b"offline")
    await a._ingest_availability("Bulb", json.dumps({"state": "offline"}).encode())
    assert len(a._bus.reach) == 1, "the repeat is not re-published"
    await a._ingest_availability("Bulb", b"online")
    assert len(a._bus.reach) == 2, "the recovery is a new edge"


async def test_availability_with_no_device_is_dropped():
    a = _adapter()
    await a._ingest_availability("", b"offline")
    assert a._bus.reach == []


# --- join / interview / leave events ----------------------------------------------


@pytest.mark.parametrize("evt,kind,severity,needle", [
    ({"type": "device_joined", "data": {"friendly_name": "0x1", "ieee_address": "0x1"}},
     "pairing", "info", "joined"),
    ({"type": "device_interview", "data": {"friendly_name": "Bulb", "status": "started"}},
     "pairing", "info", "interview started"),
    ({"type": "device_interview", "data": {"friendly_name": "Bulb", "status": "successful",
                                           "definition": {"model": "LED1836G9"}}},
     "pairing", "info", "LED1836G9"),
    ({"type": "device_interview", "data": {"friendly_name": "Bulb", "status": "failed"}},
     "pairing_failed", "warning", "interview failed"),
    ({"type": "device_leave", "data": {"friendly_name": "Bulb", "ieee_address": "0x2"}},
     "device_left", "warning", "left the network"),
])
async def test_a_zigbee_event_lands_on_the_device_timeline(evt, kind, severity, needle):
    a = _adapter()
    await a._ingest_event(json.dumps(evt).encode())
    assert len(a._bus.journals) == 1
    j = a._bus.journals[0]
    assert (j.kind, j.severity) == (kind, severity)
    assert needle in j.message
    assert j.device_key and j.entity_id.startswith("mqtt:")


async def test_an_event_without_a_subject_is_dropped():
    a = _adapter()
    await a._ingest_event(json.dumps({"type": "device_joined", "data": {}}).encode())
    await a._ingest_event(b"not json")
    assert a._bus.journals == []


async def test_an_unremarkable_event_is_not_journalled():
    """device_announce and interview 'in-between' states are noise on a timeline."""
    a = _adapter()
    await a._ingest_event(json.dumps({"type": "device_announce", "data": {"friendly_name": "x"}}).encode())
    assert a._bus.journals == []


# --- pure shaping (belongs with the transform, exercised directly) -----------------


def test_shape_networkmap_drops_a_link_missing_an_endpoint():
    value = {"nodes": [], "links": [
        {"source": {"ieeeAddr": "0xAA"}, "lqi": 10},   # no target
        {"source": {"ieeeAddr": "0xAA"}, "target": {"ieeeAddr": "0xBB"}, "lqi": 20},
    ]}
    out = shape_networkmap(value, {})
    assert len(out["links"]) == 1 and out["links"][0]["lqi"] == 20


def test_shape_networkmap_survives_garbage():
    assert shape_networkmap(None, {}) == {"nodes": [], "links": []}
    assert shape_networkmap({"nodes": ["x"], "links": ["y"]}, {}) == {"nodes": [], "links": []}
