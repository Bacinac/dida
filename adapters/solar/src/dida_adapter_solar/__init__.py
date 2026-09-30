"""DIDA solar-inverter adapter.

Reads a grid-tie PV inverter over its datalogger's LOCAL JSON API by polling —
read-only, so it coexists with whatever else already reads the logger (the
logger keeps its cloud/HA uploads; we never touch its config). The stick's
terse fixed-point fields (`pac`, `eto`, `vpv[]`, …) are normalised to canonical
capabilities here (the one place the logger's quirks live); the engine stays
protocol-blind.

Confirmed against a Solarman/iGEN stick (`/getdevdata.cgi?device=2&sn=…`,
port 8484, no auth) but the mapping is generic to that JSON shape.
"""

from __future__ import annotations

from dida_adapter_solar.adapter import SolarAdapter

__all__ = ["SolarAdapter"]
