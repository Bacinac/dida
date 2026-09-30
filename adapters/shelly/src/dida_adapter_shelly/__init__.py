"""DIDA Shelly adapter.

Bridges Shelly Gen2+ devices onto the DIDA bus over their local RPC (HTTP for
reads/commands, an outbound WebSocket per device for NotifyStatus pushes).
Fully local — no cloud, no keys. This adapter knows Shelly; the engine never
does. It runs as its own container, so a device outage or a malformed frame
storm stays contained here.
"""

from __future__ import annotations

from dida_adapter_shelly.adapter import ShellyAdapter

__all__ = ["ShellyAdapter"]
