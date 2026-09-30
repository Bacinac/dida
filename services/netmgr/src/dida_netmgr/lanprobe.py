"""LAN self-probe — reports DIDA's OWN presence on the base LAN (the network the AV
devices are on): the read-only counterpart to NetManager's VLAN presence.

Why a separate, tiny, always-on container instead of a function in the API: only
from the HOST network namespace (`network_mode: host`) can DIDA see its real
interfaces, their MACs, and the name the router registers. The API and netmgr live
in the bridge netns, where every address is the 172.x bridge one.

The address itself is not discovered: it is the configured `lan_ip` (Settings →
Network). The default route's source picks the wrong leg on a multi-homed host —
at home that is the IoT VLAN — so the probe reports the interface that holds
`lan_ip` as the LAN one, and none when no interface holds it.

Every interface the host itself addressed is listed, so a multi-homed host shows
each leg: on an LXC the VLAN tag lives on the Proxmox bridge and the container
sees only a plain veth, so the leg is identified by its address, not by a tag.

Result lands in `app_settings.lan_status = {address, hostname, interfaces: [{iface,
address (CIDR), mac, lan, default_route}]}`; the API serves it and Settings → Network
shows it read-only.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import struct

from dida_core import app_setting, pg_pool, run_service, set_app_setting, setup_logging
from home_core.health import HealthMarker
from home_core.tasks import spawn

setup_logging()
log = logging.getLogger("dida.lanprobe")

POLL = 30.0  # the base-LAN address rarely changes, so a light poll is plenty
_SIOCGIFADDR = 0x8915  # ioctl: get an interface's IPv4 address
_SIOCGIFNETMASK = 0x891B  # ioctl: get an interface's IPv4 netmask
_DOCKER_OWN = re.compile(r"^(lo|docker\d+|br-[0-9a-f]{12}|veth.+)$")


def _ioctl_ipv4(iface: str, request: int) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), request, struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return ""
    finally:
        s.close()


def _default_route_ifaces() -> set[str]:
    try:
        with open("/proc/net/route") as f:
            rows = [line.split() for line in f.readlines()[1:]]
    except OSError:
        return set()
    return {r[0] for r in rows if len(r) > 7 and r[1] == "00000000" and r[7] == "00000000"}


def _mac(iface: str) -> str:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except OSError:
        return ""


def _hostname() -> str:
    """The name the router registers — the host's /etc/hostname (mounted read-only)
    when available, else this process's own hostname."""
    try:
        with open("/host/etc/hostname") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    return socket.gethostname()


def _probe(address: str) -> dict:
    try:
        names = sorted(n for n in os.listdir("/sys/class/net") if not _DOCKER_OWN.match(n))
    except OSError:
        names = []
    default = _default_route_ifaces()
    interfaces = []
    for name in names:
        ip = _ioctl_ipv4(name, _SIOCGIFADDR)
        if not ip:
            continue
        mask = _ioctl_ipv4(name, _SIOCGIFNETMASK) or "0.0.0.0"
        interfaces.append({
            "iface": name,
            "address": str(ipaddress.ip_interface(f"{ip}/{mask}")),
            "mac": _mac(name),
            "lan": bool(address) and ip == address,
            "default_route": name in default,
        })
    interfaces.sort(key=lambda i: not i["lan"])
    return {"address": address, "hostname": _hostname(), "interfaces": interfaces}


async def main() -> None:
    pool = await pg_pool()

    spawn(HealthMarker("dida", "lanprobe").run_loop(), log=log, name="health loop")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # graceful stop like the other services
        loop.add_signal_handler(sig, stop.set)

    last: dict | None = None
    while not stop.is_set():
        status = _probe((await app_setting(pool, "lan_ip") or "").strip())
        if status != last:  # only write on change (no needless churn)
            await set_app_setting(pool, "lan_status", json.dumps(status))
            if any(i["lan"] for i in status["interfaces"]):
                log.info("lan_status: %s", status)
            elif status["address"]:
                log.error("no interface holds lan_ip %s — set it in Settings → Network", status["address"])
            else:
                log.error("lan_ip is not set — set it in Settings → Network")
            last = status
        with contextlib.suppress(TimeoutError):  # wake early on SIGTERM instead of sleeping through it
            await asyncio.wait_for(stop.wait(), timeout=POLL)
    await pool.close()
    log.info("lanprobe shutting down")


if __name__ == "__main__":
    run_service(main())
