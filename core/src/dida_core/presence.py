"""Shared network-presence reconciliation — used by the network-presence adapters
that derive `presence:<user>` from network evidence (router DHCP leases, ARP).

The hard part isn't the scan; it's NOT fighting the GPS sources (OwnTracks / the
web app) that write the SAME `presence:<user>` entities. The hierarchy follows
the physics: a phone the router actively sees is ON the home network — being
home is the only way that evidence exists — so **while present, the network
verdict always stands** and repairs any GPS claim (a coarse WiFi/cell fix can
place a person hundreds of metres out while they sit on the couch). Only when
the network does NOT see the phone does GPS own the value: a fresh disagreeing
claim is respected (the phone may just be sleeping its radio at home — or truly
gone), while a claim older than the grace window is drift the live verdict may
repair. The cost is a later "away" after a real departure (the ARP entry has to
age out first) — far cheaper than a false "away" while physically home. On top
of that, an unchanged verdict is re-published periodically so the UI's staleness
window never flags an actively-confirmed phone.

The retired opnsense and wifi adapters had byte-identical copies of this logic (a
location fetch + the arbitration + a publish-dedupe); this is the single source of
truth. Both are gone since the router moved to OpenWrt, so nothing calls this today
— kept forward-compatible for the OpenWrt presence adapter, whose router exposes the
same DHCP-lease evidence over ubus. The eid computation stays in each adapter (its
slug convention), and the bus publish stays there too — this module owns only the
reconciliation decision.
"""

from __future__ import annotations

import time


async def fetch_presence_locations(broker) -> dict[str, tuple[str, float]]:
    """Current `location` (+ age in seconds) per presence entity, from the engine's
    current_state through the adapter's broker — what OTHER sources last said."""
    rows = await broker.call("state", prefix="presence:", capabilities=["location"])
    return {r["entity_id"]: (r["value"], r["age"]) for r in rows if isinstance(r["value"], str)}


class PresenceReconciler:
    """Per-adapter reconciliation state: GPS-grace arbitration + publish dedupe.

    One instance per adapter. Call `resolve()` for each person each scan; it
    returns the location value to publish, or None to stay quiet (a fresh foreign
    claim owns the value while we don't see the phone, or the verdict is
    unchanged and still fresh)."""

    def __init__(self, *, grace_seconds: float) -> None:
        self._grace = grace_seconds
        self._last_pub: dict[str, tuple[str, float]] = {}   # eid -> (value, ts) we last published

    def resolve(
        self,
        eid: str,
        present: bool,
        site: str,
        current: dict[str, tuple[str, float]],
        *,
        refresh_seconds: float,
        now: float | None = None,
    ) -> str | None:
        """Decide the value to publish for `eid`, or None to skip.

        `present` is this source's live verdict; `site` is the zone to report when
        present ("away" otherwise); `current` is the fetched cross-source snapshot
        (eid -> (value, age_s))."""
        now = time.time() if now is None else now
        loc, age = current.get(eid, (None, 0.0))
        value = site if present else "away"
        prev = self._last_pub.get(eid)
        disagree = loc is not None and loc != value
        ours = prev is not None and prev[0] == loc  # the stored value is our own claim
        if disagree and not ours and age <= self._grace and not present:
            # We don't see the phone and a fresh foreign (GPS) claim owns the
            # value — drop our dedupe memory so a future verdict publishes
            # cleanly. (While PRESENT we never yield: an actively-seen phone IS
            # home — GPS drift can't argue with the router.)
            self._last_pub.pop(eid, None)
            return None
        # A disagreeing stored value bypasses the dedupe: "unchanged since we last
        # published" is the wrong reason to stay quiet when another source
        # overwrote the state.
        if not disagree and prev is not None and prev[0] == value and (now - prev[1]) < refresh_seconds:
            return None  # unchanged and still fresh — don't spam the bus
        self._last_pub[eid] = (value, now)
        return value
