"""A HomeKit accessory that reboots comes back on a new port.

HAP accessories pick a fresh TCP port every boot and announce it over mDNS.
aiohomekit hears the new announcement and does nothing with it: the connection
keeps dialling the address stored in the pairing. On 13.09 the living-room FP2
rebooted onto 62592, DIDA dialled 60119 for more than a day, and its five
presence zones sat on their last value — "empty" — while the music rule switched
the living room off around the people in it.

So the adapter owns the rewrite, persists it (a restart must not forget where the
accessory went), and says the accessory is unreachable while it cannot reach it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohomekit.exceptions import AccessoryNotFoundError
from dida_adapter_homekit import adapter as hk
from dida_adapter_homekit.adapter import HomekitAdapter


class _Controller:
    def __init__(self, found):
        self.found = found

    async def async_find(self, device_id, timeout=30.0):
        if self.found is None:
            raise AccessoryNotFoundError(device_id)
        return SimpleNamespace(description=self.found)


def _adapter(found):
    a = HomekitAdapter()
    a._controller = _Controller(found)
    a.saved = 0

    async def save():
        a.saved += 1

    a._save_pairings = save
    return a


def _pairing(ip="192.168.20.31", port=60119):
    return {"AccessoryPairingID": "91:78:AA:00:53:2F", "AccessoryIP": ip, "AccessoryPort": port}


def _seen(ip="192.168.20.31", port=60119):
    return SimpleNamespace(address=ip, addresses=[ip], port=port)


async def test_a_new_port_is_written_into_the_pairing_and_saved():
    a = _adapter(_seen(port=62592))
    data = _pairing()
    a._pairings["pislivingroom"] = data
    await a._follow_endpoint("pislivingroom", data)
    assert (data["AccessoryIP"], data["AccessoryIPs"], data["AccessoryPort"]) == ("192.168.20.31", ["192.168.20.31"], 62592)
    assert a._pairings["pislivingroom"]["AccessoryPort"] == 62592
    assert a.saved == 1


async def test_a_new_address_is_followed_too():
    a = _adapter(_seen(ip="192.168.20.77"))
    data = _pairing()
    await a._follow_endpoint("pislivingroom", data)
    assert data["AccessoryIP"] == "192.168.20.77"
    assert a.saved == 1


async def test_an_unmoved_accessory_is_not_rewritten():
    a = _adapter(_seen())
    data = {**_pairing(), "AccessoryIPs": ["192.168.20.31"]}
    await a._follow_endpoint("pislivingroom", data)
    assert a.saved == 0


async def test_an_accessory_nobody_announces_keeps_its_last_known_endpoint():
    a = _adapter(None)
    data = _pairing()
    await a._follow_endpoint("pislivingroom", data)
    assert data == _pairing()
    assert a.saved == 0


@pytest.fixture
def verdicts(monkeypatch):
    sent = []

    async def fake(bus, device_key, adapter, reachable, *, detail=""):
        sent.append((device_key, adapter, reachable, detail))

    monkeypatch.setattr(hk, "set_reachable", fake)
    return sent


async def test_reachability_is_said_on_the_change_only(verdicts):
    a = HomekitAdapter()
    await a._set_reach("PISLivingRoom", False, "Connect call failed")
    await a._set_reach("PISLivingRoom", False, "Connect call failed")
    await a._set_reach("PISLivingRoom", True)
    await a._set_reach("PISLivingRoom", True)
    assert verdicts == [
        ("pislivingroom", "homekit", False, "Connect call failed"),
        ("pislivingroom", "homekit", True, ""),
    ]


# --- events are the fast path, the read-back is the truth -----------------------


class _Bus:
    def __init__(self):
        self.states = []

    async def publish_state(self, update):
        self.states.append(update)


def _zoned():
    a = HomekitAdapter()
    a._bus = _Bus()
    a._readings[("pislivingroom", 1, 10)] = hk._Reading(
        "homekit:pislivingroom:seating", "Seating", "occupancy", lambda v: bool(v) if v is not None else None, False)
    a._pairings["pislivingroom"] = {"name": "PISLivingRoom"}
    return a


async def test_a_read_back_that_disagrees_corrects_the_state_and_says_an_event_was_missed(caplog):
    a = _zoned()
    a._emit("pislivingroom", {(1, 10): {"value": 1}})
    await asyncio.sleep(0)
    caplog.set_level("WARNING", logger="dida.adapter.homekit")
    a._reconcile("pislivingroom", {(1, 10): {"value": 0}})
    await asyncio.sleep(0)
    assert [s.value for s in a._bus.states] == [True, False]
    assert any("event missed" in r.message and "Seating" in r.message for r in caplog.records)


async def test_a_read_back_that_agrees_publishes_nothing_and_logs_nothing(caplog):
    a = _zoned()
    a._emit("pislivingroom", {(1, 10): {"value": 1}})
    await asyncio.sleep(0)
    caplog.set_level("WARNING", logger="dida.adapter.homekit")
    a._reconcile("pislivingroom", {(1, 10): {"value": 1}})
    await asyncio.sleep(0)
    assert [s.value for s in a._bus.states] == [True]
    assert not caplog.records


async def test_the_first_reading_is_not_called_a_missed_event(caplog):
    a = _zoned()
    caplog.set_level("WARNING", logger="dida.adapter.homekit")
    a._reconcile("pislivingroom", {(1, 10): {"value": 0}})
    await asyncio.sleep(0)
    assert [s.value for s in a._bus.states] == [False]
    assert not caplog.records
