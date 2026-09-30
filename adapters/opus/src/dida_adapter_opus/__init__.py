"""DIDA OPUS adapter.

OPUS · Player has two outputs the house plays to: its own app on the living-room
television, and the DAC that hangs off the player's host. Each has one master —
the player — so this adapter never talks to the box or the DAC: it reads what the
player says each is doing and hands the house's orders to the player, the same
road the player's own screens use.
"""

from __future__ import annotations

from dida_adapter_opus.adapter import OpusAdapter

__all__ = ["OpusAdapter"]
