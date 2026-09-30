"""Solar elevation → the discrete `sun_state` phase.

Pure and side-effect free so the boundaries can be tested without astral, a bus or
a clock. The adapter supplies the two numbers; this decides the phase.

Why a phase at all, when sun_elevation is already published: elevation is a level,
and "at dawn" is an edge. Written against the level, the rule has to guess a
threshold value to equal — and a once-a-minute sample rounded to 0.1° lands on
exactly 0.0 on roughly half the mornings, then fires a second time at sunset when
the sun crosses back through it. The phase moves through each of its four values
once a day, in order, so `to: "dawn"` is unambiguous and cannot be dusk.
"""

from __future__ import annotations

# Civil twilight. Above the horizon is day; below -6° there is no usable light and
# it is night; the band between them is dawn or dusk depending on which way the sun
# is going.
DAY_ABOVE = 0.0
NIGHT_BELOW = -6.0


def sun_phase(elevation: float, rising: bool) -> str:
    """One of night / dawn / day / dusk.

    `rising` disambiguates the twilight band — it is the whole reason dawn and dusk
    are separable at all, since the two are the same elevations traversed in
    opposite directions.
    """
    if elevation > DAY_ABOVE:
        return "day"
    if elevation < NIGHT_BELOW:
        return "night"
    return "dawn" if rising else "dusk"
