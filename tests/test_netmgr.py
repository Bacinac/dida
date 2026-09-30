"""netmgr — the VLAN reconcile primitives. Pure logic only (no Docker, no ip(8)):
the parts where a mistake either strands DIDA off the IoT VLAN or publishes an
address that isn't real.
"""
from dida_netmgr.manager import NetManager, _dhcp_hostname, _valid_vlan


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
