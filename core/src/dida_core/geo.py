"""Geo helpers — zone resolution shared by every presence source.

A zone is a circle: (name, latitude, longitude, radius_m). Resolving a position
to a zone is the same regardless of how the position arrived (the DIDA web app's
browser geolocation, an OwnTracks phone over MQTT, …), so the logic lives here
once and every source calls it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

# Value of the `location` capability when a position is outside every zone.
AWAY = "away"

# A zone for resolution: (name, latitude, longitude, radius_m).
Zone = tuple[str, float, float, float]

# Max zone-containment credit granted for a fix's reported inaccuracy. Capped so
# a kilometre-grade fix can't glue a person to a zone from across town.
ACC_CREDIT_MAX_M = 500.0

# A fix coarser than this can't say which ~100 m zone the phone is in — a cell
# tower "position" 2 km wide would flip someone to "away" (or into the wrong
# zone) while they sit at home. Every GPS source drops such reports; the phone's
# next proper fix lands seconds later.
MAX_ACCURACY_M = 500.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resolve_zone(lat: float, lon: float, zones: Iterable[Zone], acc_m: float = 0.0) -> str:
    """Resolve a position to the most specific containing zone.

    "Most specific" = smallest radius (a small zone inside a big one wins), then
    nearest centre as a tie-break. Returns the zone name, or AWAY when the point
    falls outside every zone.

    `acc_m` is the fix's reported accuracy radius: a coarse WiFi/cell fix can sit
    hundreds of metres off while the phone is physically inside the zone, so a
    point whose error circle still overlaps the zone must NOT assert "away". The
    credit is capped (ACC_CREDIT_MAX_M) — genuine departure fixes are GPS-grade
    (tens of metres) and resolve away exactly as before.
    """
    credit = min(max(acc_m, 0.0), ACC_CREDIT_MAX_M)
    best_key: tuple[float, float] | None = None
    best_name = AWAY
    for name, zlat, zlon, radius in zones:
        dist = haversine_m(lat, lon, zlat, zlon)
        if dist <= radius + credit:
            key = (radius, dist)
            if best_key is None or key < best_key:
                best_key = key
                best_name = name
    return best_name
