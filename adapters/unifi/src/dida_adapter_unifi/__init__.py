"""DIDA UniFi presence adapter.

Presence read from the UniFi controller's association table rather than from a
router's ARP table. A phone that dozes its Wi-Fi keeps its 802.11 association, so it
stays visible while it sits at home — the failure that made the retired OPNsense
lease adapter useless on departures — and a disassociation is a positive "left"
signal instead of an entry quietly ageing out.

Each person maps to their device MACs. The addresses are locally administered
(private Wi-Fi address) but stable — the household's phones have held the same one
for seventeen months — so the MAC is the reliable key, while the controller's client
name is editable and not unique. The name remains a fallback when no MAC is given.
`uplinkDeviceId` also says which access point they are on, so four APs give a coarse
in-house zone on top of home/away.

Feeds the same `presence:<user>` entities as the OwnTracks/web-app GPS sources; the
shared reconciler in `dida_core.presence` arbitrates between them.
"""

from __future__ import annotations

from dida_adapter_unifi.adapter import UnifiAdapter

__all__ = ["UnifiAdapter"]
