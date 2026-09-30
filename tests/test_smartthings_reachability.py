"""SmartThings reachability comes from the health endpoint, not from device status.

The status endpoint keeps answering for a device that has dropped off the cloud,
with its last values frozen: the living-room TV lost its SmartThings link and read
`switch: on` for eighteen days, so the TV-follows-light rule never saw a change and
nothing flagged the device. Health is the only field that says the values are live.
"""

from __future__ import annotations

import pytest
from dida_adapter_smartthings.adapter import ReauthNeeded, SmartThingsAdapter
from dida_core import ReachabilityEvent


class _Bus:
    def __init__(self) -> None:
        self.reach: list[ReachabilityEvent] = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)

    async def publish_entity(self, info) -> None:
        pass

    async def publish_state(self, update) -> None:
        pass


class _Health:
    def __init__(self, state: str) -> None:
        self.state = state


class _ST:
    def __init__(self, health: dict[str, str], devices: list[str]) -> None:
        self.health = health
        self.devices = devices

    async def get_raw_devices(self):
        return [{"items": [{"deviceId": d, "label": d} for d in self.devices]}]

    async def get_raw_device_status(self, device_id):
        return {"components": {"main": {"switch": {"switch": {"value": "on"}}}}}

    async def get_device_health(self, device_id):
        state = self.health[device_id]
        if isinstance(state, Exception):
            raise state
        return _Health(state)


def _adapter(st: _ST) -> tuple[SmartThingsAdapter, _Bus]:
    a = SmartThingsAdapter()
    a._bus = bus = _Bus()
    a._st = st
    return a, bus


async def test_an_offline_device_is_unreachable_even_though_status_still_answers():
    a, bus = _adapter(_ST({"tv": "OFFLINE"}, ["tv"]))
    await a._poll_once()
    assert [(e.device_key, e.reachable) for e in bus.reach] == [("smartthings:tv", False)]
    assert "offline" in bus.reach[0].detail


async def test_verdicts_are_published_only_on_a_change():
    st = _ST({"tv": "ONLINE"}, ["tv"])
    a, bus = _adapter(st)
    await a._poll_once()
    await a._poll_once()
    st.health["tv"] = "UNHEALTHY"
    await a._poll_once()
    st.health["tv"] = "ONLINE"
    await a._poll_once()
    assert [e.reachable for e in bus.reach] == [True, False, True]


async def test_a_failed_health_read_is_no_verdict():
    a, bus = _adapter(_ST({"tv": RuntimeError("timeout")}, ["tv"]))
    await a._poll_once()
    assert bus.reach == []


async def test_an_auth_failure_on_health_asks_for_reconnect():
    a, _ = _adapter(_ST({"tv": RuntimeError("401 Unauthorized")}, ["tv"]))
    with pytest.raises(ReauthNeeded):
        await a._poll_once()


async def test_a_device_gone_from_the_account_is_forgotten():
    st = _ST({"tv": "OFFLINE"}, ["tv"])
    a, bus = _adapter(st)
    await a._poll_once()
    st.devices = []
    await a._poll_once()
    st.devices = ["tv"]
    await a._poll_once()
    assert [e.reachable for e in bus.reach] == [False, False]
