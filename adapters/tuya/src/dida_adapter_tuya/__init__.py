"""DIDA Tuya adapter.

Bridges Tuya Wi-Fi devices onto the DIDA bus over the LOCAL protocol (via
tinytuya) — no cloud in the runtime path. Each device's id/local_key/ip and an
explicit DPS->capability map are configured in Settings → Adapters (DB). This adapter
knows Tuya; the engine never does. It runs as its own container, so a device
outage stays contained here.
"""

from __future__ import annotations

from dida_adapter_tuya.adapter import TuyaAdapter

__all__ = ["TuyaAdapter"]
