"""DIDA astro adapter.

A pure-computation *source* adapter: it owns no hardware, it publishes the sun's
elevation for the home's location onto the bus as `astro:sun` / `sun_elevation`,
plus the discrete phase `sun_state` (night/dawn/day/dusk). That turns daylight into
ordinary capabilities the engine already understands: gate on "night" with a native
condition (``sun_elevation <= 0``), and TRIGGER on "at dawn" with ``sun_state`` —
which, unlike the continuous elevation, changes exactly once per transition and
tells dawn apart from dusk (see mapping.py).

Like every adapter it runs as its own container — but it talks to no device,
so it can never block on I/O; the worst case is a missing/bad coordinate, which
``start()`` rejects with an explicit RuntimeError (fail loud, visible restart).
"""

from __future__ import annotations

from dida_adapter_astro.adapter import AstroAdapter

__all__ = ["AstroAdapter"]
