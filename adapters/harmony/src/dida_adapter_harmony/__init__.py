"""DIDA Harmony adapter.

Bridges a Logitech Harmony Hub onto the DIDA bus over its LOCAL API
(aioharmony) — no cloud. A Harmony hub's whole model is "which activity is
running" (Watch TV, PowerOff, …), which maps cleanly onto the canonical `enum`
capability: the current activity is the value, the available activities are the
options, and selecting one starts that activity (PowerOff turns everything
off). So the hub needs no bespoke capability or UI — it reuses the enum control.
"""

from __future__ import annotations

from dida_adapter_harmony.adapter import HarmonyAdapter

__all__ = ["HarmonyAdapter"]
