"""DIDA Midea adapter.

Bridges a Midea air-conditioner onto the DIDA bus over Midea's LOCAL LAN
protocol (msmart-ng) — no cloud in the runtime path. The AC decomposes into
ordinary climate capabilities on one entity (hvac_mode / target_temperature /
fan_mode, plus the current `temperature`), which the UI recomposes into a
climate card. Per-device host/id/token/key are configured in Settings →
Adapters (DB, carries the local key). This adapter knows Midea;
the engine only ever sees capabilities.
"""

from __future__ import annotations

from dida_adapter_midea.adapter import MideaAdapter

__all__ = ["MideaAdapter"]
