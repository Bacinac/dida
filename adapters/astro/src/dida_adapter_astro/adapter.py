from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dida_core import AdapterConfig, Bus, Command, FailureGate, StateUpdate

from dida_adapter_astro.mapping import sun_phase

log = logging.getLogger("dida.adapter.astro")

NAMESPACE = "astro"
SUN_ENTITY = f"{NAMESPACE}:sun"
CLOCK_ENTITY = f"{NAMESPACE}:clock"


class AstroAdapter:
    """Publishes the sun's elevation + time-of-day for the home's location.

    No device, no network — it computes the solar altitude (astral) on a timer and
    publishes `astro:sun`/`sun_elevation` + `sun_state` and `astro:clock`/`time_of_day`.
    `sun_elevation` is the level (a good condition); `sun_state` is the edge
    (night/dawn/day/dusk), which is what a rule should TRIGGER on — see mapping.py.
    The location (lat/lon) is NOT configured here: it is read from the home zone
    (Zones → the one flagged `is_home`), the single source of truth for where
    "here" is — only altitude/tz/cadence come from the DB config. A pure READ
    source. Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._last_elev: float | None = None  # dedupe
        self._last_tod: int | None = None      # dedupe
        self._last_phase: str | None = None    # dedupe — one publish per transition

    async def _home(self) -> tuple[float, float] | None:
        home = next((z for z in await self.broker.call("zones") if z["is_home"]), None)
        return (float(home["latitude"]), float(home["longitude"])) if home else None

    async def start(self, bus: Bus) -> None:
        from astral import Observer
        from astral.sun import elevation

        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        gate = FailureGate(self.status, threshold=3)  # a transient DB blip must not flap the badge
        # Supervise: re-read config + home zone each tick so a moved home zone or
        # edited altitude/tz applies live, without a restart.
        while True:
            poll = 60
            try:
                await self._cfg.load()
                poll = self._cfg.int("poll_seconds", 60)
                home = await self._home()
                if home is None:
                    self.status.idle("no home zone — flag one in Zones")
                    gate.reset()
                else:
                    lat, lon = home
                    elev_m = self._cfg.int("elevation", 0)
                    tz_name = self._cfg.get("tz", "Europe/Zagreb")
                    try:
                        tz = ZoneInfo(tz_name)
                    except Exception:
                        log.warning("astro: unknown tz %r — falling back to Europe/Zagreb", tz_name, exc_info=True)
                        tz = ZoneInfo("Europe/Zagreb")
                    observer = Observer(latitude=lat, longitude=lon, elevation=elev_m)
                    now_utc = datetime.now(UTC)
                    # Round to 0.1° — fine for a night gate, and lets us dedupe so a
                    # slowly-moving sun doesn't spam the bus / history.
                    elev = round(elevation(observer, now_utc), 1)
                    await self._publish_elevation(elev)
                    # Direction from a look-ahead, not from the previous sample: this
                    # is then correct on the FIRST tick after a restart, where a
                    # history-based answer would have to guess or publish "unknown".
                    ahead = elevation(observer, now_utc + timedelta(minutes=10))
                    await self._publish_sun_state(sun_phase(elev, rising=ahead > elev))
                    # Local wall-clock minutes since midnight (time-window gating +
                    # a 1-min heartbeat). Local tz, not UTC.
                    local = now_utc.astimezone(tz)
                    await self._publish_time_of_day(local.hour * 60 + local.minute)
                    gate.ok(f"sun @ {lat:.3f},{lon:.3f}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("astro: compute/publish failed: %s", exc, exc_info=True)
                gate.fail(str(exc) or "compute failed")
            # Align to the next wall-clock minute boundary (fixed sleep drifted a
            # little past 60 s per cycle, occasionally SKIPPING a minute value —
            # automations gating on exact time_of_day would miss that minute).
            if poll == 60:
                await asyncio.sleep(60.5 - (time.time() % 60))
            else:
                await asyncio.sleep(poll)

    async def _publish_elevation(self, elev: float) -> None:
        if self._bus is None or elev == self._last_elev:
            return
        self._last_elev = elev
        await self._bus.publish_state(
            StateUpdate(
                entity_id=SUN_ENTITY,
                capability="sun_elevation",
                value=float(elev),
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit="°",
                name="Sun",
            )
        )

    async def _publish_sun_state(self, phase: str) -> None:
        if self._bus is None or phase == self._last_phase:
            return
        self._last_phase = phase
        await self._bus.publish_state(
            StateUpdate(
                entity_id=SUN_ENTITY,
                capability="sun_state",
                value=phase,
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                name="Sun",
            )
        )

    async def _publish_time_of_day(self, minutes: int) -> None:
        if self._bus is None or minutes == self._last_tod:
            return
        self._last_tod = minutes
        await self._bus.publish_state(
            StateUpdate(
                entity_id=CLOCK_ENTITY,
                capability="time_of_day",
                value=int(minutes),
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit="min",
                name="Clock",
            )
        )

    async def handle_command(self, command: Command) -> None:
        return  # read-only source — nothing to command

    async def stop(self) -> None:
        self._bus = None
