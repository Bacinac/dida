"""LAN discovery helpers shared by adapters that can auto-find their devices.

`mdns_browse` returns (instance_label, ipv4) for an mDNS service type — used by
the Shelly / ESPHome / Harmony adapters to populate the UI "Scan network" list.
Needs the `zeroconf` package (adapters that call it declare the dep) and host
networking to reach multicast. Broadlink/SSDP use their own broadcast instead.

The discovery result each adapter returns over its `dida.discover.<name>` control
subject is a list of devices, each describing how a click applies to the form:

  {"devices": [
    {"label": "shelly1g4-… (192.0.2.21)", "appendCsv": {"hosts": "192.0.2.21"}},
    {"label": "RM4 Pro (192.0.2.91)", "set": {"host": "…", "mac": "…", "type": "0x649b"}},
    {"label": "PMIS Room (192.0.2.51)", "appendJson": {"config": {"name": "…", "host": "…"}}},
  ]}

  * set        — replace these field values
  * appendCsv  — append the value to a comma-separated field (deduped)
  * appendJson — push the object into a JSON-array field (e.g. esphome nodes)
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable

log = logging.getLogger("dida.discovery")

_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")

# A house network: a /22 is four times anything here. Wider is a typo (a /16 is
# 65,534 probes) and is refused, never cut down to its first block.
MAX_SCAN_ADDRESSES = 1024


async def answer_discover(bus, msg, name: str,
                          discover: Callable[[list[str]], Awaitable[dict]]) -> None:
    """Answer a `dida.discover.<name>` request with what `discover(subnets)` found;
    a failure goes back to the asker as {"error": …}."""
    try:
        req = json.loads(msg.data) if msg.data else {}
        result = await discover(req.get("subnets") or [])
    except Exception as exc:
        log.warning("%s discover failed: %s", name, exc, exc_info=True)
        result = {"error": str(exc)}
    if msg.reply and bus is not None:
        await bus.nc.publish(msg.reply, json.dumps(result).encode())


def subnet_hosts(subnets: Iterable[str]) -> list[str]:
    """Every host address in the subnets, in order, each once. ValueError names a
    malformed subnet, or subnets that together exceed MAX_SCAN_ADDRESSES."""
    nets = []
    for cidr in subnets:
        try:
            nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            raise ValueError(f"not a subnet: {cidr!r}") from None
    size = sum(n.num_addresses for n in nets)
    if size > MAX_SCAN_ADDRESSES:
        raise ValueError(f"{', '.join(map(str, nets))} spans {size} addresses; "
                         f"a network scan covers at most {MAX_SCAN_ADDRESSES}")
    return list(dict.fromkeys(str(h) for n in nets for h in n.hosts()))


async def probe_hosts[T](hosts: Iterable[str], probe: Callable[[str], Awaitable[T | None]],
                         concurrency: int = 64) -> list[T]:
    """Run `probe` on every host, `concurrency` at a time; the non-None answers, in
    host order."""
    sem = asyncio.Semaphore(concurrency)

    async def one(ip: str) -> T | None:
        async with sem:
            return await probe(ip)

    return [r for r in await asyncio.gather(*(one(h) for h in hosts)) if r is not None]


async def local_ip(source=None) -> str:
    """DIDA's own IP on the network the AV devices live on — the NIC SSDP must bind
    to. Shared by the AV/media adapters (dlna, denon, heos) and the cast panel URL.

    Read from `lan_ip`, which every installation declares as precisely this (the
    DB setting first, the env var behind it). It used to be read out of the media
    base URL, back when this host also served music; the music left, the address
    stayed, under the name of what it is.

    The route-source fallback (below) answers a subtly different question — "the
    source IP of the DEFAULT ROUTE" — which equals the LAN only on a single-homed
    host, by coincidence rather than by definition. The prod host is multi-homed and
    routes by default over the IoT VLAN, so the fallback picks that leg and SSDP
    multicast never reaches the LAN the renderers are actually on: measured
    2026-07-15, an M-SEARCH from the VLAN leg found NOTHING while the same search
    from the LAN leg found every renderer (iFi, Marantz, TV). It is kept only for a
    host whose base URL is a DNS name rather than an IP literal.
    """
    from dida_core.db import host_setting

    host = (await host_setting(source, "lan_ip")).strip()
    if _IPV4.fullmatch(host):
        return host
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))  # no packet is sent — a connected UDP socket resolves the route
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def ssdp_msearch(
    iface_ip: str,
    targets: list[str],
    timeout: float = 3.0,
    *,
    stop_on_first: bool = False,
) -> list[tuple[str, bytes]]:
    """Blocking SSDP M-SEARCH on `iface_ip` for each ST in `targets`, returning
    (sender_ip, raw_response) for every reply. Each target is sent twice (UDP is
    lossy) and replies are collected until `timeout`; `stop_on_first` returns after
    the first reply — enough for a single-host lookup. Callers parse the LOCATION
    header (or use the sender IP) they need.

    BLOCKING (socket loop up to `timeout`) — call via asyncio.to_thread. Needs host
    networking to reach the 239.255.255.250 multicast group. The AV/media adapters
    (denon, heos, dlna) share this rather than lean on a library's shifting search
    API — it's a dozen lines and version-proof."""
    msgs = [
        (
            "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
            f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n'
        ).encode()
        for st in targets
    ]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
        s.bind((iface_ip, 0))
    except OSError as exc:
        # Fall back to every interface rather than not search at all — but SAY SO: a
        # mistyped/stale iface_ip otherwise searches the whole host and quietly works,
        # hiding the misconfiguration until the day the wrong NIC wins.
        log.warning("ssdp: cannot bind %s (%s) — searching ALL interfaces instead", iface_ip, exc)
        s.bind(("", 0))
    s.settimeout(0.5)
    for msg in msgs:  # one M-SEARCH per target; UDP is lossy so each is sent twice
        for _ in range(2):
            with contextlib.suppress(OSError):
                s.sendto(msg, ("239.255.255.250", 1900))
    out: list[tuple[str, bytes]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = s.recvfrom(65535)
        except (TimeoutError, OSError):
            continue
        out.append((addr[0], data))
        if stop_on_first:
            break
    s.close()
    return out


async def mdns_browse(service_type: str, seconds: float = 5.0) -> list[tuple[str, str]]:
    """Browse an mDNS service type for `seconds`, returning (label, ipv4) pairs.

    Deduped by IP. Best-effort: a resolve miss just drops that instance."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: list[str] = []

    # zeroconf fires handlers with keyword args (zeroconf=, service_type=, name=,
    # state_change=) — the parameter names MUST match exactly or the call raises
    # TypeError inside zeroconf's dispatch and the scan silently returns nothing.
    def on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added:
            names.append(name)

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, service_type, handlers=[on_change])
    try:
        await asyncio.sleep(seconds)
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in dict.fromkeys(names):  # preserve order, dedupe
            info = AsyncServiceInfo(service_type, name)
            try:
                if not await info.async_request(azc.zeroconf, 2500):
                    continue
            except Exception:
                log.debug("mdns: service info not resolved", exc_info=True)
                continue
            addrs = [a for a in info.parsed_addresses() if ":" not in a]  # IPv4 only
            if not addrs or addrs[0] in seen:
                continue
            seen.add(addrs[0])
            out.append((name.split(".")[0], addrs[0]))
        return out
    finally:
        await browser.async_cancel()
        await azc.async_close()
