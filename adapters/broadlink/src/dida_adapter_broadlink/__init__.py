"""DIDA Broadlink adapter.

Bridges a Broadlink RM IR/RF blaster onto the DIDA bus over its LOCAL protocol
(python-broadlink) — no cloud. A blaster is a stateless *transmitter*: there is
nothing to read, you just fire learned codes at dumb devices (a projector, an
AC, …). So it maps onto a single `remote` capability whose value lists the
available command labels (the buttons) and whose `send` command transmits the
named code. Learned codes (exported from Home Assistant) live in
state/adapter-broadlink/codes.json.
"""

from __future__ import annotations

from dida_adapter_broadlink.adapter import BroadlinkAdapter

__all__ = ["BroadlinkAdapter"]
