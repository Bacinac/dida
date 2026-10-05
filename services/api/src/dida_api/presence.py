"""Shared presence publishing — the ONE implementation every HTTP GPS source
goes through (the web app's `/presence/report`, the OwnTracks HTTP receiver).

Identity is the authenticated user, so the entity is `presence:<username>` —
the same entity the OwnTracks MQTT adapter writes (the network presence adapters
wrote it too, until the router moved to OpenWrt). Every source converges on one entity per person; the
zone is resolved with the shared core logic (`resolve_zone`), and all caps are
published non-diagnostic so the entity stays a curated presence card.
"""

from __future__ import annotations

import math
import re

from dida_core import MAX_ACCURACY_M, resolve_zone

from dida_api.presence_order import publish_ordered
from dida_api.presence_order import timestamp_ns as timestamp_ns

AWAY = "away"

_SLUG = re.compile(r"[^a-z0-9_-]+")


def _entity_for(username: str) -> tuple[str, str]:
    """(entity_id, display name) for a presence username — the one convention
    every source shares."""
    slug = _SLUG.sub("_", username.strip().lower()).strip("_") or "person"
    return f"presence:{slug}", username[:1].upper() + username[1:]


async def publish_transition(state, username: str, event: str, desc: str, *, observed_ns: int,
                             sequence: int | None = None, reporter: str | None = None) -> dict:
    """Honour an OwnTracks region transition — the phone-side geofence enter/leave
    that significant-change monitoring fires when it crosses a DIDA zone we pushed
    as a waypoint. This is the event-driven presence edge the network ARP signal
    can't give (a dozing phone reads offline while sitting at home).

    ENTER latches the zone as present; LEAVE clears it to "away" — but only if we
    still believe they're in the zone being left, so a stale leave that arrives
    after the enter of the next zone can't wrongly evict them."""
    entity_id, name = _entity_for(username)
    ev = (event or "").strip().lower()
    desc = (desc or "").strip()
    if ev == "enter" and desc:
        value: str = desc
    elif ev == "leave" and desc:
        value = AWAY
    else:
        return {"accepted": False, "reason": "unhandled_event"}
    return await publish_ordered(state, username, entity_id, name, observed_ns, [("location", value)],
                                 event=ev, desc=desc, sequence=sequence, reporter=reporter)


async def publish_report(
    state,
    username: str,
    latitude: float,
    longitude: float,
    accuracy: float | None = None,
    battery: float | None = None,
    *, observed_ns: int, sequence: int | None = None, reporter: str | None = None,
) -> dict:
    """Resolve the zone and publish location/lat/lon/battery for `username`.
    Returns `{accepted, zone}` (or `{accepted: False, reason}` for a fix too
    coarse to trust) so the reporting device can show what got registered."""
    if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
        return {"accepted": False, "reason": "invalid_accuracy"}
    if accuracy is not None and accuracy > MAX_ACCURACY_M:
        return {"accepted": False, "reason": "low_accuracy"}
    rows = await state.pool.fetch("SELECT name, latitude, longitude, radius_m FROM zones")
    zones = [(r["name"], r["latitude"], r["longitude"], r["radius_m"]) for r in rows]
    location = resolve_zone(latitude, longitude, zones, acc_m=accuracy or 0.0)

    entity_id, name = _entity_for(username)
    values = [("location", location), ("latitude", round(latitude, 6)), ("longitude", round(longitude, 6))]
    if battery is not None:
        values.append(("battery", float(battery)))
    return await publish_ordered(state, username, entity_id, name, observed_ns, values,
                                 sequence=sequence, reporter=reporter)
