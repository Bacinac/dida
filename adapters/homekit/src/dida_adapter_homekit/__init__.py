"""DIDA HomeKit adapter.

Bridges HomeKit (HAP-IP) accessories onto the DIDA bus via aiohomekit. DIDA is
its own HAP controller; pairings live in Settings → Adapters (DB) and are minted
once with `python -m dida_adapter_homekit.pair`. This adapter knows HAP; the engine never
does. It runs as its own container, so an accessory outage stays contained here.
"""

from __future__ import annotations

from dida_adapter_homekit.adapter import HomekitAdapter

__all__ = ["HomekitAdapter"]
