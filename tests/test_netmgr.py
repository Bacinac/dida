"""netmgr — the VLAN reconcile primitives. Pure logic only (no Docker, no ip(8)):
the parts where a mistake either strands DIDA off the IoT VLAN or publishes an
address that isn't real.
"""
import json
from unittest.mock import AsyncMock

import pytest
from dida_netmgr import manager
from dida_netmgr.manager import CONTAINER, NetManager, _dhcp_hostname, _valid_vlan


def _mgr() -> NetManager:
    return NetManager.__new__(NetManager)


def test_valid_vlan_range():
    assert _valid_vlan(1) and _valid_vlan(20) and _valid_vlan(4094)
    for bad in (0, -1, 4095, 9999):
        assert not _valid_vlan(bad), f"{bad} is not a VLAN id"


def test_dummy_subnet_is_unique_per_vlan():
    m = _mgr()
    seen = {m._dummy_subnet(v) for v in range(1, 500)}
    assert len(seen) == 499, "two VLANs sharing a placeholder subnet would collide in Docker IPAM"


def test_placeholder_is_never_mistaken_for_a_real_lease():
    # Docker re-applies its IPAM placeholder to the iface on every reattach. Reading
    # it as a lease would publish a bogus 10.x address as DIDA's VLAN presence.
    m = _mgr()
    ph = m._dummy_subnet(20)                      # '10.240.20.0/24'
    assert m._is_placeholder(20, ph.replace(".0/24", ".1/24"))
    assert not m._is_placeholder(20, "192.168.20.57/24"), "a real DHCP lease is not a placeholder"
    assert not m._is_placeholder(20, "10.240.21.1/24"), "another VLAN's placeholder is not ours"


def test_dhcp_hostname_cannot_inject_dhclient_directives():
    # The name is written into a dhclient CONFIG as: send host-name "<name>";
    # A quote or semicolon would close that statement and let the rest become
    # further directives.
    assert _dhcp_hostname('dida"; request subnet-mask; #') == "didarequestsubnet-mask"
    for ch in ('"', ";", "\n", " ", "{", "}"):
        assert ch not in _dhcp_hostname(f"di{ch}da")
    # …while a legitimate hostname survives untouched.
    assert _dhcp_hostname("dida-home.lan") == "dida-home.lan"
    assert _dhcp_hostname("") == ""
    assert _dhcp_hostname("---") == "", "nothing but separators is no hostname at all"
    assert len(_dhcp_hostname("x" * 200)) == 63, "a DNS label maxes out at 63 chars"


@pytest.mark.parametrize("attached", [True, False])
async def test_parent_change_recreates_attached_and_detached_networks(monkeypatch, attached):
    m = NetManager(None)
    network = {"Driver": "macvlan", "Options": {"parent": "eth0.20"},
               "Containers": {"self": {"Name": CONTAINER}} if attached else {}}
    calls = []

    async def docker(method, path, body=None):
        nonlocal network
        calls.append((method, path, body))
        if method == "GET":
            return (200, json.dumps(network)) if network else (404, "missing")
        if method == "DELETE":
            network = None
            return 204, ""
        if path == "/networks/create":
            network = body | {"Containers": {}}
            return 201, "{}"
        return 200, ""

    monkeypatch.setattr(manager, "parent_nic", AsyncMock(return_value="eth1"))
    monkeypatch.setattr(manager.asyncio, "sleep", AsyncMock())
    m._docker = docker
    m._desired = AsyncMock(return_value={20})
    m._vlan_config = AsyncMock(return_value={"20": {"mac": "02:00:00:00:00:20"}})
    m._existing = AsyncMock(return_value={20} if attached else set())
    m._current_macs = AsyncMock(return_value={20: "02:00:00:00:00:20"})
    m._iface_for = AsyncMock(return_value="eth2" if attached else "")
    m._dhcp_release = AsyncMock()
    m._iface_names = AsyncMock(return_value=set())
    m._ensure_dhcp = AsyncMock()
    m._publish_status = AsyncMock()
    await m.reconcile()
    assert network["Options"]["parent"] == "eth1.20"
    mutations = [(method, path) for method, path, _ in calls if method != "GET"]
    assert mutations == ([('POST', '/networks/dida-vlan20/disconnect')] if attached else []) + [
        ('DELETE', '/networks/dida-vlan20'), ('POST', '/networks/create'), ('POST', '/networks/dida-vlan20/connect')]
    connect = next(body for _, path, body in calls if path.endswith("/connect"))
    assert connect["EndpointConfig"]["MacAddress"] == "02:00:00:00:00:20"
    calls.clear()
    m._existing.return_value = {20}
    await m.reconcile()
    assert all(method == "GET" for method, _, _ in calls)


async def test_foreign_endpoint_is_rejected_before_lease_or_network_changes():
    m = NetManager(None)
    m._network = AsyncMock(return_value={"Containers": {"foreign": {"Name": "other-project"}}})
    m._docker = AsyncMock()
    m._iface_for = AsyncMock()
    with pytest.raises(RuntimeError, match="foreign endpoints"):
        await m._remove(20)
    m._docker.assert_not_awaited()
    m._iface_for.assert_not_awaited()


async def test_failed_network_removal_does_not_report_success():
    m = NetManager(None)
    m._network = AsyncMock(return_value={"Containers": {}})
    m._iface_for = AsyncMock(return_value="")
    m._docker = AsyncMock(return_value=(500, "storage unavailable"))
    with pytest.raises(RuntimeError, match=r"Remove.*500"):
        await m._remove(20)


async def test_failed_inspection_does_not_look_like_an_empty_network():
    m = NetManager(None)
    m._docker = AsyncMock(return_value=(500, "daemon unavailable"))
    with pytest.raises(RuntimeError, match="Inspect"):
        await m._existing()
    with pytest.raises(RuntimeError, match="Inspect"):
        await m._network(20)
