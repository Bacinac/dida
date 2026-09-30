"""DIDA ESPHome adapter.

Connects directly to ESPHome nodes over their native API (port 6053) — no MQTT,
no Home Assistant in the loop — and maps each node's entities to canonical DIDA
capabilities. One persistent, auto-reconnecting connection per node, isolated in
its own container. The path to adopting DIY ESPHome devices straight into DIDA.
"""

from __future__ import annotations

from dida_adapter_esphome.adapter import EsphomeAdapter

__all__ = ["EsphomeAdapter"]
