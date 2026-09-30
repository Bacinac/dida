"""A verdict nobody heard is a device that stays wrong forever.

Reachability rides core NATS, on the stated grounds that "the latest verdict wins
and a missed one is corrected by the next". That was false for an edge-triggered
producer: an adapter publishes only on a CHANGE and caches it, so there is no next
one. On 29.08 the denon adapter reached the Marantz seven seconds before the engine
had subscribed; the device sat marked unreachable for 54 minutes with a live telnet
session open, and the alert fired. Every deploy did this to whatever reconnected
first. These pin the loop that makes the sentence true.

Run in any DIDA image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_reachability_reassert.py"
"""
import asyncio

import pytest
from dida_core import reachability


class _Bus:
    def __init__(self, fail_first: bool = False):
        self.sent: list[tuple[str, bool, str]] = []
        self._fail = fail_first

    async def publish_reachability(self, ev):
        if self._fail:
            self._fail = False
            raise RuntimeError("nobody listening")
        self.sent.append((ev.device_key, ev.reachable, ev.detail))


@pytest.fixture(autouse=True)
def _clean():
    reachability._LAST.clear()
    yield
    reachability._LAST.clear()


@pytest.mark.asyncio
async def test_a_verdict_is_remembered_so_it_can_be_said_again():
    bus = _Bus()
    await reachability.set_reachable(bus, "192.168.1.40", "denon", True)
    await reachability.set_reachable(bus, "kuhinja", "mqtt", False, detail="availability offline")
    bus.sent.clear()

    task = asyncio.create_task(reachability.reassert_loop(bus, period=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    said = {(d, r) for d, r, _ in bus.sent}
    assert ("192.168.1.40", True) in said, "the verdict the engine missed is said again"
    assert ("kuhinja", False) in said, "and so is an unreachable one"
    assert ("kuhinja", False, "availability offline") in bus.sent, "with its reason intact"


@pytest.mark.asyncio
async def test_a_publish_that_fails_is_still_remembered():
    """The first send is exactly the one that gets lost. If a failure dropped the
    memory of it, the loop would have nothing to correct with."""
    bus = _Bus(fail_first=True)
    await reachability.set_reachable(bus, "192.168.1.40", "denon", True)
    assert bus.sent == [], "the send failed"
    assert ("192.168.1.40", "denon") in reachability._LAST, "…and was remembered anyway"


@pytest.mark.asyncio
async def test_a_device_that_is_gone_stops_being_insisted_on():
    bus = _Bus()
    await reachability.set_reachable(bus, "old-cam", "baba", False)
    reachability.forget_reachable("old-cam", "baba")
    bus.sent.clear()

    task = asyncio.create_task(reachability.reassert_loop(bus, period=0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.sent == [], "nothing is asserted about a device this adapter no longer has"


@pytest.mark.asyncio
async def test_the_loop_survives_a_bus_that_is_down():
    """Never raises: a status ping that cannot go out must not take the adapter with
    it — the next tick tries again."""
    class Broken(_Bus):
        async def publish_reachability(self, ev):
            raise RuntimeError("bus down")

    bus = Broken()
    await reachability.set_reachable(_Bus(), "x", "y", True)
    task = asyncio.create_task(reachability.reassert_loop(bus, period=0.01))
    await asyncio.sleep(0.04)
    assert not task.done(), "still running after repeated failures"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
