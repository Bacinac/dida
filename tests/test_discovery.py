"""Regression tests for the LAN-discovery helpers (dida_core.discovery).

These are the shared, network-touching primitives every auto-discovering adapter
leans on: `local_ip()` (route-source IP for SSDP binding), `ssdp_msearch()` (the
blocking M-SEARCH the AV/media adapters share instead of a library) and
`mdns_browse()` (the zeroconf browse the Shelly/ESPHome/Harmony "Scan network"
uses). No real sockets or multicast are touched — the socket module and the
zeroconf package are stubbed at the boundary so the REAL parsing/dedupe/fallback
logic runs deterministically.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_discovery.py"
"""
from __future__ import annotations

import socket
import sys
import types

import pytest
from dida_core import discovery


class FakeUDP:
    """A stand-in for a UDP `socket.socket`, recording every call the discovery
    helpers make so the tests can assert on real send/bind/recv behaviour."""

    def __init__(
        self,
        *,
        recv_script=None,
        fail_multicast_if=False,
        connect_error=False,
        sockname=("0.0.0.0", 0),
    ):
        self.sent: list[tuple[bytes, tuple]] = []
        self.binds: list[tuple] = []
        self.sockopts: list[tuple] = []
        self.closed = False
        self.timeout = None
        self.connect_addr = None
        self._recv = list(recv_script or [])
        self._fail_multicast_if = fail_multicast_if
        self._connect_error = connect_error
        self._sockname = sockname

    def setsockopt(self, level, opt, val):
        self.sockopts.append((level, opt, val))
        if self._fail_multicast_if and opt == socket.IP_MULTICAST_IF:
            raise OSError("cannot bind multicast interface")

    def bind(self, addr):
        self.binds.append(addr)

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, msg, addr):
        self.sent.append((msg, addr))

    def recvfrom(self, bufsize):
        if not self._recv:
            raise TimeoutError
        item = self._recv.pop(0)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item
        if isinstance(item, BaseException):
            raise item
        return item

    def connect(self, addr):
        self.connect_addr = addr
        if self._connect_error:
            raise OSError("network is unreachable")

    def getsockname(self):
        return self._sockname

    def close(self):
        self.closed = True


def _install_socket(monkeypatch, fake):
    monkeypatch.setattr(discovery.socket, "socket", lambda *a, **k: fake)
    return fake


# --- local_ip -------------------------------------------------------------
# The LAN address is a stored setting now, so these drive it through a stand-in
# pool rather than the environment (which only ever seeds that row, at first boot).


class FakeDB:
    def __init__(self, value: str | None) -> None:
        self.value = value

    async def fetchval(self, _sql: str, *args):
        return self.value


@pytest.fixture(autouse=True)
def _clean_setting_cache():
    from dida_core.db import _setting_cache

    _setting_cache.clear()
    yield
    _setting_cache.clear()


@pytest.mark.asyncio
async def test_local_ip_prefers_the_declared_lan_ip(monkeypatch):
    # The installation states its LAN IP once (Settings → Network). That wins outright:
    # the route fallback would answer "the default route's leg", which on a
    # multi-homed host is the IoT VLAN, not the LAN the renderers are on.
    fake = _install_socket(monkeypatch, FakeUDP(sockname=("203.0.113.11", 51234)))
    db = FakeDB("198.51.100.100")
    assert await discovery.local_ip(db) == "198.51.100.100", \
        "the declared LAN IP wins over the default route's leg"
    assert fake.connect_addr is None, "the route is not even resolved when the LAN IP is declared"


@pytest.mark.asyncio
async def test_local_ip_ignores_a_non_ip_value(monkeypatch):
    # A host name rather than an IP literal cannot name a NIC → fall back.
    _install_socket(monkeypatch, FakeUDP(sockname=("192.0.2.7", 51234)))
    assert await discovery.local_ip(FakeDB("dida.local")) == "192.0.2.7", \
        "a DNS name is not a NIC → route fallback"


@pytest.mark.asyncio
async def test_local_ip_returns_route_source_ip(monkeypatch):
    fake = _install_socket(monkeypatch, FakeUDP(sockname=("192.0.2.7", 51234)))
    assert await discovery.local_ip(FakeDB(None)) == "192.0.2.7", \
        "returns the local end of the connected UDP socket (the LAN route source)"
    assert fake.connect_addr == ("8.8.8.8", 9), "connects to a public addr to resolve the route"
    assert fake.closed is True, "socket is always closed (finally), even on the happy path"


@pytest.mark.asyncio
async def test_local_ip_falls_back_to_zero_on_oserror(monkeypatch):
    fake = _install_socket(monkeypatch, FakeUDP(connect_error=True))
    assert await discovery.local_ip(FakeDB(None)) == "0.0.0.0", "an unreachable route resolves to 0.0.0.0, not a raise"
    assert fake.closed is True, "socket is closed in finally even when connect() raised"


# --- ssdp_msearch ---------------------------------------------------------


def test_ssdp_msearch_sends_each_target_twice_with_correct_headers(monkeypatch):
    fake = _install_socket(monkeypatch, FakeUDP())
    targets = ["urn:schemas-upnp-org:device:MediaServer:1", "roku:ecp"]
    # timeout=0 -> the receive loop never enters (deadline already reached), so we
    # isolate and assert purely on the send behaviour.
    out = discovery.ssdp_msearch("198.51.100.10", targets, timeout=0.0)
    assert out == [], "no replies scripted -> empty result"
    assert len(fake.sent) == 4, "one M-SEARCH per target, each sent twice (UDP is lossy)"
    assert all(addr == ("239.255.255.250", 1900) for _, addr in fake.sent), \
        "every datagram goes to the SSDP multicast group"
    msg0 = fake.sent[0][0]
    assert b"M-SEARCH * HTTP/1.1" in msg0 and b'MAN: "ssdp:discover"' in msg0
    assert b"ST: urn:schemas-upnp-org:device:MediaServer:1\r\n" in fake.sent[0][0], \
        "first pair carries the first target's ST header"
    assert b"ST: roku:ecp\r\n" in fake.sent[2][0], "second pair carries the second target's ST header"


def test_ssdp_msearch_stop_on_first_returns_first_reply(monkeypatch):
    reply = (b"HTTP/1.1 200 OK\r\nLOCATION: http://x\r\n\r\n", ("198.51.100.20", 1900))
    fake = _install_socket(monkeypatch, FakeUDP(recv_script=[reply]))
    out = discovery.ssdp_msearch("198.51.100.10", ["ssdp:all"], stop_on_first=True)
    assert out == [("198.51.100.20", b"HTTP/1.1 200 OK\r\nLOCATION: http://x\r\n\r\n")], \
        "result pairs (sender_ip, raw_response); stop_on_first returns after the first reply"
    assert fake.closed is True, "socket closed before returning"


def test_ssdp_msearch_binds_to_iface_then_falls_back_on_oserror(monkeypatch):
    fake = _install_socket(monkeypatch, FakeUDP(fail_multicast_if=True))
    discovery.ssdp_msearch("198.51.100.10", ["ssdp:all"], timeout=0.0)
    assert fake.binds == [("", 0)], \
        "when setting the multicast interface fails, bind falls back to the wildcard address"


def test_ssdp_msearch_binds_to_iface_when_multicast_ok(monkeypatch):
    fake = _install_socket(monkeypatch, FakeUDP())
    discovery.ssdp_msearch("198.51.100.10", ["ssdp:all"], timeout=0.0)
    assert fake.binds == [("198.51.100.10", 0)], "normal path binds to the requested interface IP"


def test_ssdp_msearch_collects_replies_until_deadline(monkeypatch):
    r1 = (b"resp-one", ("198.51.100.20", 1900))
    r2 = (b"resp-two", ("198.51.100.21", 1900))
    _install_socket(monkeypatch, FakeUDP(recv_script=[r1, r2, TimeoutError]))
    # Drive the deadline deterministically: 1 monotonic() call sets the deadline,
    # then one per while-check. 100.0 trips the deadline and ends the loop.
    seq = iter([0.0, 1.0, 2.0, 3.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(seq))
    out = discovery.ssdp_msearch("198.51.100.10", ["ssdp:all"], timeout=10.0)
    assert out == [("198.51.100.20", b"resp-one"), ("198.51.100.21", b"resp-two")], \
        "collects every reply until the deadline; a mid-loop timeout is skipped, not fatal"


# --- mdns_browse ----------------------------------------------------------


@pytest.fixture
def fake_zeroconf(monkeypatch):
    """Install a stub `zeroconf` / `zeroconf.asyncio` so mdns_browse's real
    dedupe/IPv4-filter/name-split logic runs without the package or a network.

    The returned dict is the test's control surface:
      discovered -> service names the browser 'sees' (fired as Added)
      updated    -> names fired with a non-Added change (must be ignored)
      info       -> per-name {"ok": bool, "addrs": [...]} or {"raise": True}
    """
    state = {"discovered": [], "updated": [], "info": {}, "browsers": [], "zcs": []}

    class ServiceStateChange:
        Added = object()
        Updated = object()

    class AsyncZeroconf:
        def __init__(self):
            self.zeroconf = "ZC_HANDLE"
            self.closed = False
            state["zcs"].append(self)

        async def async_close(self):
            self.closed = True

    class AsyncServiceBrowser:
        def __init__(self, zc, service_type, handlers):
            self.cancelled = False
            state["browsers"].append(self)
            for name in state["discovered"]:
                for h in handlers:
                    h(zeroconf=zc, service_type=service_type, name=name,
                      state_change=ServiceStateChange.Added)
            for name in state["updated"]:
                for h in handlers:
                    h(zeroconf=zc, service_type=service_type, name=name,
                      state_change=ServiceStateChange.Updated)

        async def async_cancel(self):
            self.cancelled = True

    class AsyncServiceInfo:
        def __init__(self, service_type, name):
            self.name = name

        async def async_request(self, zc, timeout_ms):
            entry = state["info"][self.name]
            if entry.get("raise"):
                raise RuntimeError("resolve exploded")
            return entry["ok"]

        def parsed_addresses(self):
            return state["info"][self.name]["addrs"]

    zmod = types.ModuleType("zeroconf")
    zmod.ServiceStateChange = ServiceStateChange
    zasync = types.ModuleType("zeroconf.asyncio")
    zasync.AsyncServiceBrowser = AsyncServiceBrowser
    zasync.AsyncServiceInfo = AsyncServiceInfo
    zasync.AsyncZeroconf = AsyncZeroconf
    monkeypatch.setitem(sys.modules, "zeroconf", zmod)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", zasync)
    return state


async def test_mdns_browse_dedupes_and_filters_ipv4(fake_zeroconf):
    fake_zeroconf["discovered"] = [
        "Shelly-A._http._tcp.local.",
        "Shelly-B._http._tcp.local.",
        "Shelly-Dup._http._tcp.local.",   # same IPv4 as A -> deduped away
        "Shelly-Miss._http._tcp.local.",  # async_request False -> dropped
        "Shelly-V6._http._tcp.local.",    # only IPv6 -> dropped (no IPv4)
    ]
    fake_zeroconf["info"] = {
        "Shelly-A._http._tcp.local.": {"ok": True, "addrs": ["198.51.100.21"]},
        "Shelly-B._http._tcp.local.": {"ok": True, "addrs": ["fe80::1", "198.51.100.22"]},
        "Shelly-Dup._http._tcp.local.": {"ok": True, "addrs": ["198.51.100.21"]},
        "Shelly-Miss._http._tcp.local.": {"ok": False, "addrs": ["198.51.100.99"]},
        "Shelly-V6._http._tcp.local.": {"ok": True, "addrs": ["fe80::2"]},
    }
    out = await discovery.mdns_browse("_http._tcp.local.", seconds=0.0)
    assert out == [("Shelly-A", "198.51.100.21"), ("Shelly-B", "198.51.100.22")], \
        "label is the leading name segment; IPv6 filtered, duplicate IP + resolve-miss dropped"


async def test_mdns_browse_resolve_exception_is_skipped(fake_zeroconf):
    fake_zeroconf["discovered"] = ["Boom._http._tcp.local.", "Good._http._tcp.local."]
    fake_zeroconf["info"] = {
        "Boom._http._tcp.local.": {"raise": True},
        "Good._http._tcp.local.": {"ok": True, "addrs": ["198.51.100.30"]},
    }
    out = await discovery.mdns_browse("_http._tcp.local.", seconds=0.0)
    assert out == [("Good", "198.51.100.30")], \
        "a resolve that raises is best-effort skipped, never fatal to the whole scan"


async def test_mdns_browse_ignores_non_added_events(fake_zeroconf):
    fake_zeroconf["discovered"] = ["Real._http._tcp.local."]
    fake_zeroconf["updated"] = ["Phantom._http._tcp.local."]  # Updated, never registered
    fake_zeroconf["info"] = {"Real._http._tcp.local.": {"ok": True, "addrs": ["198.51.100.40"]}}
    out = await discovery.mdns_browse("_http._tcp.local.", seconds=0.0)
    assert out == [("Real", "198.51.100.40")], "only ServiceStateChange.Added registers a name"


async def test_mdns_browse_cleans_up_browser_and_zeroconf(fake_zeroconf):
    fake_zeroconf["discovered"] = []
    out = await discovery.mdns_browse("_x._tcp.local.", seconds=0.0)
    assert out == [], "no discoveries -> empty result"
    assert fake_zeroconf["browsers"][0].cancelled is True, "browser cancelled in finally"
    assert fake_zeroconf["zcs"][0].closed is True, "AsyncZeroconf closed in finally"


def test_subnet_hosts_lists_every_host_once_in_order():
    hosts = discovery.subnet_hosts(["192.168.20.0/30", " 192.168.20.0/30", "10.0.0.4/31"])
    assert hosts == ["192.168.20.1", "192.168.20.2", "10.0.0.4", "10.0.0.5"]


@pytest.mark.parametrize("subnets, message", [
    (["10.0.0.0/16"], "65536 addresses"),
    (["192.168.0.0/22", "192.168.20.0/24"], "1280 addresses"),
    (["192.168.1.0/24", "not-a-net"], "not a subnet: 'not-a-net'"),
])
def test_subnet_hosts_refuses_rather_than_cuts(subnets, message):
    """A /16 used to be cut to its first 1024 addresses and the rest never scanned,
    with "found none" for everything past the cut."""
    with pytest.raises(ValueError, match=message):
        discovery.subnet_hosts(subnets)


def test_a_house_sized_range_is_scanned_whole():
    assert len(discovery.subnet_hosts(["192.168.0.0/22"])) == discovery.MAX_SCAN_ADDRESSES - 2


async def test_probe_hosts_keeps_the_answers_in_host_order_within_the_limit():
    import asyncio

    running = peak = 0

    async def probe(ip: str):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.001 * (10 - int(ip.rsplit(".", 1)[1]) % 10))
        running -= 1
        return ip if ip.endswith(("3", "7")) else None

    hosts = discovery.subnet_hosts(["10.0.0.0/28"])
    assert await discovery.probe_hosts(hosts, probe, concurrency=4) == ["10.0.0.3", "10.0.0.7", "10.0.0.13"]
    assert peak == 4
