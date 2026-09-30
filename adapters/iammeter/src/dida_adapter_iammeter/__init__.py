"""DIDA IAMMETER grid energy-meter adapter.

Reads an IAMMETER WiFi meter (WEM3080 / WEM3080T) over its LOCAL HTTP API by
polling `http://<host>/monitorjson` — read-only, no cloud, no HA. The meter's
terse `Data` array is normalised to canonical capabilities here. It sits on the
grid feed and reports NET power (negative = exporting to the grid), plus import
and export lifetime energy — so it pairs with the solar adapter for a full
"producing / importing / exporting" picture.

Replaces the last device that came in through Home Assistant's MQTT discovery
(via the emqx broker) — this talks straight to the meter instead, and captures
all seven fields (voltage, current, power, import+export energy, frequency,
power factor) rather than the two the HA-discovery bridge exposed.
"""

from __future__ import annotations

from dida_adapter_iammeter.adapter import IammeterAdapter

__all__ = ["IammeterAdapter"]
