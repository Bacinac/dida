"""Samsung TV ↔ canonical translation: identity, power, and the wake packet.

Pure functions only, so the decisions the adapter makes from the TV's answers are
testable without a TV on the network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct

from dida_core.ids import slug

NAMESPACE = "samsungtv"

_MAC = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$", re.IGNORECASE)


def entity_id(name: str) -> str:
    return f"{NAMESPACE}:{slug(name, default='tv')}"


def stated_power(info: dict | None) -> bool | None:
    """Power as the TV states it. Models from 2019 on report `PowerState` and keep
    answering in standby, so that field decides; older ones say nothing (None) and
    the adapter has to look for itself."""
    state = ((info or {}).get("device") or {}).get("PowerState")
    return None if state is None else str(state).lower() == "on"


def identity(info: dict | None) -> dict:
    """What the TV says about itself that DIDA keeps across its being off: the
    immutable device id (the rename-proof native key) and the MAC a wake packet
    needs, both of which are only readable while it answers."""
    device = (info or {}).get("device") or {}
    out = {}
    duid = str(device.get("duid") or device.get("id") or "").removeprefix("uuid:")
    if duid:
        out["duid"] = duid
    mac = str(device.get("wifiMac") or device.get("mac") or "").lower()
    if _MAC.match(mac):
        out["mac"] = mac
    return out


def magic_packet(mac: str) -> bytes:
    if not _MAC.match(mac or ""):
        raise ValueError(f"not a MAC address: {mac!r}")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def _route_addr(col: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<I", int(col, 16))))


def broadcast_for(ip: str, route_table: str) -> str | None:
    """Directed broadcast of the directly connected network `ip` sits on, read from
    /proc/net/route. The limited broadcast (255.255.255.255) follows the DEFAULT
    route, which on a multi-homed host is not necessarily the TV's network — on the
    house host it is the IoT VLAN — so the packet has to name the subnet."""
    try:
        target = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    best: ipaddress.IPv4Network | None = None
    for line in route_table.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8:
            continue
        try:
            dest, gateway, mask = _route_addr(cols[1]), _route_addr(cols[2]), _route_addr(cols[7])
        except ValueError:
            continue
        if int(gateway) or not int(mask):
            continue
        net = ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)
        if target in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best.broadcast_address) if best else None
