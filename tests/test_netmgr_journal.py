"""netmgr's VLAN/lease journal — transitions only, and never at the cost of the VLAN.

netmgr owns this installation's foot on the IoT VLAN. The journal hangs off
`_publish_status`, which is ALREADY the change detector (it exists because the
status is only worth writing when it differs), so these tests pin the two
properties that follow from that: a first reconcile is a boot and stays silent,
and a steady state produces nothing at all.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src:/w/services/netmgr/src python -m pytest tests/test_netmgr_journal.py"
"""
from __future__ import annotations

import pytest
from dida_core.events import JournalEvent
from dida_netmgr.manager import NetManager


class FakeBus:
    def __init__(self) -> None:
        self.published: list[JournalEvent] = []

    async def publish_journal(self, event: JournalEvent) -> None:
        self.published.append(event)


def _mgr(bus, last):
    m = NetManager(pool=None, bus=bus)
    m._last_status = last
    return m


@pytest.mark.asyncio
async def test_first_reconcile_is_a_boot_not_a_transition():
    # Without this, every restart would claim every VLAN had just come up.
    bus = FakeBus()
    await _mgr(bus, None)._journal_status_change({"20": {"iface": "eth1", "address": "192.168.20.11"}})
    assert bus.published == []


@pytest.mark.asyncio
async def test_unchanged_status_says_nothing():
    bus = FakeBus()
    same = {"20": {"iface": "eth1", "address": "192.168.20.11", "mac": "aa"}}
    await _mgr(bus, same)._journal_status_change(dict(same))
    assert bus.published == []


@pytest.mark.asyncio
async def test_vlan_joining_and_leaving():
    bus = FakeBus()
    await _mgr(bus, {})._journal_status_change({"20": {"iface": "eth1", "address": "192.168.20.11"}})
    assert [e.kind for e in bus.published] == ["vlan_up"]
    assert bus.published[0].source == "netmgr"

    bus = FakeBus()
    await _mgr(bus, {"20": {"iface": "eth1", "address": "192.168.20.11"}})._journal_status_change({})
    assert [e.kind for e in bus.published] == ["vlan_down"]


@pytest.mark.asyncio
async def test_a_changed_lease_is_the_event_that_matters():
    # DIDA's own address on the IoT VLAN moving under it is exactly what "the
    # device stopped answering" looks like from the other side.
    bus = FakeBus()
    await _mgr(bus, {"20": {"address": "192.168.20.11"}})._journal_status_change(
        {"20": {"address": "192.168.20.57"}})
    (e,) = bus.published
    assert e.kind == "dhcp_lease" and e.severity == "info"
    assert "192.168.20.11" in e.message and "192.168.20.57" in e.message


@pytest.mark.asyncio
async def test_losing_a_lease_altogether_is_a_warning():
    bus = FakeBus()
    await _mgr(bus, {"20": {"address": "192.168.20.11"}})._journal_status_change({"20": {"address": ""}})
    (e,) = bus.published
    assert e.kind == "dhcp_lease" and e.severity == "warning", \
        "an address that went away is not an info-level fact"


@pytest.mark.asyncio
async def test_no_bus_is_silent_not_broken():
    # netmgr must come up on a box whose NATS is down — the VLAN is its real job.
    await _mgr(None, {})._journal_status_change({"20": {"address": "192.168.20.11"}})  # must not raise
