"""The reachability helper an adapter calls — the publishing side.

Small on purpose: it wraps a verdict in the wire event and hands it to the bus,
never raising, because a status ping must not be able to fail an adapter's real
work. What is worth pinning is the two guards — a verdict with no subject is
dropped rather than written against an empty key, and the detail is bounded and
cleared on the reachable side so "reachable" never carries a stale reason.
"""

from __future__ import annotations

from dida_core import ReachabilityEvent, set_reachable


class _Bus:
    def __init__(self, boom=False) -> None:
        self.events: list[ReachabilityEvent] = []
        self.boom = boom

    async def publish_reachability(self, event) -> None:
        if self.boom:
            raise RuntimeError("bus down")
        self.events.append(event)


async def test_a_verdict_is_published_as_a_typed_event():
    bus = _Bus()
    await set_reachable(bus, "kuhinja", "esphome", True)
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert (ev.device_key, ev.adapter, ev.reachable) == ("kuhinja", "esphome", True)
    assert ev.ts_ns > 0


async def test_an_unreachable_verdict_carries_its_reason():
    bus = _Bus()
    await set_reachable(bus, "n", "esphome", False, detail="noise handshake failed")
    assert bus.events[0].detail == "noise handshake failed"


async def test_a_reachable_verdict_never_carries_a_detail():
    """"reachable" plus a leftover reason reads as a half-failure. The helper drops
    the detail on the reachable side so the two can't contradict."""
    bus = _Bus()
    await set_reachable(bus, "n", "esphome", True, detail="was: MQTT offline")
    assert bus.events[0].detail == ""


async def test_the_detail_is_bounded():
    bus = _Bus()
    await set_reachable(bus, "n", "esphome", False, detail="x" * 5000)
    assert len(bus.events[0].detail) <= 200


async def test_a_verdict_with_no_device_is_dropped_not_written():
    """A verdict keyed on "" would land on a nonexistent device row and, worse,
    read as one device in any aggregate. It is a caller bug — drop it."""
    bus = _Bus()
    await set_reachable(bus, "", "esphome", False)
    await set_reachable(bus, "n", "", False)
    assert bus.events == []


async def test_a_dead_bus_does_not_raise():
    """This sits in an adapter's connect/disconnect path. Raising here would turn a
    reconnect into a crash — the status ping is the least important thing happening."""
    await set_reachable(_Bus(boom=True), "n", "esphome", False)  # must not raise
