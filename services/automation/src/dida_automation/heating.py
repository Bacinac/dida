"""The heating controller: rooms → TRV setpoints, rooms → boiler.

It runs inside the automation service because it is the same kind of thing — a
derivation over state that acts — and because a control loop needs what that
service already has: the live snapshot, the bus, and a tick. It is deliberately
NOT expressible as a rule: hysteresis latches and anti-cycling timers are state
over time, and a Starlark script (no I/O, no timers, no memory) structurally
cannot hold them.

What it does every tick:

  1. read the state of everything the config references (one query, with the
     timestamps — freshness is part of the decision, not a detail),
  2. per room: resolve the target, decide whether it calls for heat,
  3. write each valve's setpoint (only on a real change, then VERIFY the valve
     took it — a TRV that never echoes its setpoint is broken, and saying so is
     the whole point of an integration you can trust),
  4. one boiler decision for the house, with anti-cycling on both edges,
  5. publish what it decided as `heating:*` state, which is what the UI reads.

Publishing is how the decisions leave this process: no surface re-derives them,
and the whole thing lands in history for free — a season of target-vs-actual per
room is exactly what you need to tune a house.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from dida_core import AdapterConfig, Bus, StateUpdate, app_setting, prepare_command
from dida_core.heating import (
    OFF,
    HeatingSettings,
    RoomEntities,
    RoomHeating,
    boiler_decision,
    boiler_demand,
    demands_heat,
    derive_rooms,
    detect_window,
    frost_for,
    next_step,
    preheat_minutes,
    setpoint_for,
    target_for,
    validate_room,
    validate_settings,
)

log = logging.getLogger("dida.automation.heating")

SETTINGS_KEY = "heating"
# How long a valve gets to echo a setpoint we wrote before we call it broken. Long
# on purpose: a battery TRV reports on its own schedule, not ours.
VERIFY_S = 900.0
# TRVs step in halves; anything finer would write forever without converging.
STEP = 0.5

# Room status, published as the room entity's `text`. One vocabulary, English like
# every other adapter descriptor — the UI translates it.
ST_OFF = "off"            # heating disabled, house-wide or for this room
ST_SUMMER = "summer"      # outdoor temperature above the cutoff
ST_WINDOW = "window"      # a window is open here
ST_STALE = "stale"        # no fresh room reading — refusing to heat blind
ST_NO_SENSOR = "no_sensor"
ST_VALVE_ERROR = "valve_error"  # a valve won't take (or won't report) its setpoint
ST_HEATING = "heating"
ST_PREHEAT = "preheat"    # climbing early so it ARRIVES at the scheduled temperature
ST_IDLE = "idle"


class Room:
    """One room's live view: its config, what it resolved to, and why.

    `sensors`/`valves` are what the room actually contains (its assigned entities),
    overridden by the config only where the config says so."""

    __slots__ = ("area_id", "cfg", "contacts", "demand", "name", "profile", "sensors",
                 "status", "target", "temperature", "valves")

    def __init__(self, area_id: int, name: str, cfg: RoomHeating, contains: RoomEntities) -> None:
        self.area_id = area_id
        self.name = name
        self.cfg = cfg
        self.valves = cfg.valves or list(contains.valves)
        self.sensors = [cfg.sensor] if cfg.sensor else list(contains.sensors)
        self.contacts = list(contains.contacts)
        self.temperature: float | None = None
        self.target: float | None = None
        self.profile = ""
        self.demand = False
        self.status = ST_IDLE


class HeatingController:
    def __init__(self, bus: Bus, pool: asyncpg.Pool, unreachable: Callable[[], frozenset[str]]) -> None:
        self._bus = bus
        self._pool = pool
        # The automation engine's view of which entities sit on an unreachable device.
        # A window contact that dropped off while "open" would otherwise hold its room
        # at frost for as long as it stays gone, and a boiler relay would be cycled on
        # a state nobody is reporting.
        self._unreachable = unreachable
        self._boiler_unknown_logged = False
        self._settings = HeatingSettings()
        self._rooms: list[Room] = []
        self._tz = ZoneInfo("Europe/Zagreb")
        self._astro_cfg = AdapterConfig("astro", pool)
        # Per-valve: the setpoint we last wrote and when (monotonic). Drives both
        # the re-write interval and the did-it-take verification.
        self._written: dict[str, tuple[float, float]] = {}
        # Valves already reported as not echoing their setpoint — logged once, not
        # every tick, so a dead TRV doesn't drown the log.
        self._valve_errors: set[str] = set()
        self._boiler_on: bool | None = None
        self._boiler_since = 0.0  # epoch seconds of the last boiler transition
        self._master_off_logged = False  # logged the "off while running" warning once
        self._published: dict[tuple[str, str], object] = {}
        # Per room: the recent temperature trace our own window detection reads, and
        # when it decided a window was open. In memory only — after a restart the
        # trace refills within the measuring window, and until it has, no detection
        # is better than one made up from two samples.
        self._trace: dict[int, list[tuple[float, float]]] = {}
        self._window_since: dict[int, float] = {}
        # Per room: whether it was calling for heat last tick. The demand hysteresis
        # is a LATCH (keep calling until a deadband above target), so it must persist
        # across ticks — and `Room` objects are rebuilt on every reload(), which runs
        # before every tick, so it cannot live on the Room. Keyed by area_id, like
        # the trace above.
        self._demand: dict[int, bool] = {}
        # Last config error logged per room (and "" for the house), so a config that
        # stays broken says so ONCE instead of every reload — the reason the same
        # guard exists for automation rules.
        self._invalid_logged: dict[int | str, str] = {}

    async def reload(self) -> None:
        """Re-read the config. Invalid rows are skipped LOUDLY: a house whose config
        stopped decoding must not silently fall back to heating nothing."""
        raw = await app_setting(self._pool, SETTINGS_KEY)
        try:
            self._settings = validate_settings(_json(raw)) if raw else HeatingSettings()
            self._invalid_logged.pop("", None)
        except Exception as exc:
            log.debug("heating settings invalid", exc_info=True)
            self._log_invalid("", f"heating settings invalid ({exc}) — controller idle until fixed")
            self._settings = HeatingSettings()
        # A room with a thermostatic valve in it IS a heated room — no separate act of
        # adding it. Its config is an overlay on that, and a room that never got one
        # runs the defaults rather than being left out in the cold.
        contains, _orphans = await derive_rooms(self._pool)
        rows = await self._pool.fetch("SELECT id, name, kind, heating_config FROM areas ORDER BY id")
        rooms: list[Room] = []
        for r in rows:
            here = contains.get(r["id"], RoomEntities([], [], []))
            raw = r["heating_config"]
            if raw is None and not here.valves:
                continue
            try:
                cfg = validate_room(raw) if raw is not None else RoomHeating()
            except Exception as exc:
                log.debug("heating config of room %s invalid", r['id'], exc_info=True)
                self._log_invalid(r["id"], f"room {r['id']} heating config invalid: {exc}")
                continue
            self._invalid_logged.pop(r["id"], None)
            room = Room(r["id"], r["name"] or r["kind"] or f"area {r['id']}", cfg, here)
            if room.valves:
                rooms.append(room)
        self._rooms = rooms
        try:
            await self._astro_cfg.load()
            name = self._astro_cfg.get("tz", "") or "Europe/Zagreb"
            self._tz = ZoneInfo(name)
        except Exception as exc:
            log.warning("heating: could not resolve timezone (%s); keeping %s", exc, self._tz, exc_info=True)

    def _log_invalid(self, key: int | str, message: str) -> None:
        if self._invalid_logged.get(key) != message:
            self._invalid_logged[key] = message
            log.error("%s", message)

    async def tick(self, now: float | None = None) -> None:
        """One control pass. Raises nothing the caller must handle — a failure here
        is logged and the boiler simply keeps its last state (the safe default: a
        thermostat contact that stops changing is a house that keeps its heat).

        `now` exists so a test can put the house at half five in the morning; the
        service never passes it."""
        state = await self._read_state()
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, self._tz)
        away = self._settings.away_helper != "" and state.value(self._settings.away_helper) is True
        outdoor = self._outdoor(state)
        summer = outdoor is not None and outdoor > self._settings.summer_cutoff
        frost = frost_for(self._settings, outdoor)

        for room in self._rooms:
            self._resolve(room, state, local, away=away, summer=summer, frost=frost,
                          outdoor=outdoor, now=now)
        for room in self._rooms:
            await self._drive_valves(room, state)

        is_on = state.value(self._settings.boiler, "on_off") is True if self._settings.boiler else False
        want = boiler_demand(
            sum(1 for r in self._rooms if r.demand and r.cfg.can_call_boiler),
            sum(1 for r in self._rooms if r.demand and not r.cfg.can_call_boiler),
            is_on, self._settings,
        )
        await self._drive_boiler(want, state, now)
        await self._publish(state)

    # --- decisions -------------------------------------------------------------

    def _outdoor(self, state: _State) -> float | None:
        """The weather, or None when no sensor is configured or it has gone quiet.
        Every use of it treats None as "carry on heating": a missing summer cutoff
        makes the house too warm in June, a false one leaves it cold in January."""
        if not self._settings.outdoor:
            return None
        # Freshness matters here as much as for a room: a sensor wedged at 20 °C in
        # October would make every tick think it's summer and park the whole house
        # at frost all winter, with the status giving no hint the input is dead. A
        # stale reading is treated as no reading — which falls back to "carry on
        # heating", the safe winter default.
        val = state.value(self._settings.outdoor, "temperature", max_age=self._settings.stale_after_s)
        return float(val) if isinstance(val, int | float) else None

    def _track(self, room: Room, now: float) -> None:
        """Keep each room's recent temperature trace, trimmed to the measuring window.
        Sampled at the tick, so the trace is as dense as the control loop itself."""
        if room.temperature is None:
            return
        trace = self._trace.setdefault(room.area_id, [])
        trace.append((now, room.temperature))
        cutoff = now - self._settings.window_minutes * 60
        self._trace[room.area_id] = [s for s in trace if s[0] >= cutoff]

    def _resolve(self, room: Room, state: _State, local: datetime, *,
                 away: bool, summer: bool, frost: float, outdoor: float | None, now: float) -> None:
        """Resolve one room, then carry its demand latch forward. The latch read/write
        is wrapped here (not inside `_decide`) so it survives every early-return branch
        AND the per-tick reload() that rebuilds the Room."""
        self._decide(room, state, local, away=away, summer=summer, frost=frost,
                     outdoor=outdoor, now=now)
        self._demand[room.area_id] = room.demand

    def _decide(self, room: Room, state: _State, local: datetime, *,
                away: bool, summer: bool, frost: float, outdoor: float | None, now: float) -> None:
        cfg = room.cfg
        was_demanding = self._demand.get(room.area_id, False)
        room.temperature = self._room_temperature(room, state)
        self._track(room, now)  # the trace our own window detection reads, kept current
        room.target, room.profile = target_for(
            cfg, self._settings, local.weekday(), local.hour * 60 + local.minute,
            away=away, now=now,
        )
        if not self._settings.enabled:
            # Master off: hands off. The resolved target is still published (it is
            # what the room WOULD run at), but nothing is written anywhere.
            room.status, room.demand = ST_OFF, False
            return
        if not cfg.enabled:
            # A room switched off still gets the frost setpoint — an unheated room
            # with water in the radiator is a burst pipe waiting for January.
            room.status, room.demand = ST_OFF, False
            room.target = frost
            return
        if cfg.window_pause and self._window_open(room, state, now):
            room.status, room.demand = ST_WINDOW, False
            room.target = frost
            return
        if summer:
            # Out of season the valves are parked at frost too, so a TRV isn't left
            # holding a summer setpoint it will chase (and flatten its battery on).
            room.status, room.demand = ST_SUMMER, False
            room.target = frost
            return
        if room.profile == OFF:
            # The schedule (or the house mode) says heating stands down now. Unlike
            # the master switch this still WRITES — the freeze guard is the point.
            room.status, room.demand = ST_OFF, False
            return
        if room.temperature is None:
            # No reading at all vs a reading that stopped coming: different faults,
            # different fixes, so they get different statuses.
            room.status = ST_STALE if self._has_stale_source(room, state) else ST_NO_SENSOR
            room.demand = False
            return
        early = self._preheat(room, local, outdoor, now, away=away)
        if early is not None and early > room.target:
            room.target = early
            room.demand = demands_heat(was_demanding, room.temperature, room.target, self._settings)
            room.status = ST_PREHEAT if room.demand else ST_IDLE
            return
        room.demand = demands_heat(was_demanding, room.temperature, room.target, self._settings)
        room.status = ST_HEATING if room.demand else ST_IDLE

    def _preheat(self, room: Room, local: datetime, outdoor: float | None,
                 now: float, *, away: bool) -> float | None:
        """The setpoint of the NEXT scheduled step, if the room has to start climbing
        now to reach it on time. A schedule that says 21 at 06:30 means the room is
        warm at half six, not that the boiler lights then — which is the whole
        difference between a schedule and a promise.

        Only for a house running its schedule: a pinned mode, an empty house or a
        manual override are all statements about NOW, and starting early would
        override the person who just made them."""
        settings = self._settings
        if away or not settings.preheat or settings.mode != "auto" or room.temperature is None:
            # An away house is heated to its away setpoint on purpose — don't climb
            # toward comfort for a room no one is in.
            return None
        if room.cfg.override_target is not None and (room.cfg.override_until or 0) > now:
            return None
        step = next_step(room.cfg.schedule or settings.schedule, local.weekday(),
                         local.hour * 60 + local.minute)
        if step is None or step[0] == OFF:
            return None
        upcoming = setpoint_for(room.cfg, settings, step[0])
        if upcoming <= room.temperature:
            return None
        return upcoming if step[1] <= preheat_minutes(room.temperature, upcoming, outdoor, settings) else None

    def _room_temperature(self, room: Room, state: _State) -> float | None:
        """The room's temperature: the mean of its thermometers — the ones assigned to
        it, exactly the set its floor-plan label averages. A room with no thermometer
        of its own falls back to its valves' internal readings, which are biased by the
        radiator but better than nothing. Stale readings are not used at all; heating
        on a number from two days ago is worse than not heating."""
        fresh = self._settings.stale_after_s
        for sources in (room.sensors, room.valves):
            vals = [state.value(s, "temperature", max_age=fresh) for s in sources]
            nums = [float(v) for v in vals if isinstance(v, int | float)]
            if nums:
                return sum(nums) / len(nums)
        return None

    def _has_stale_source(self, room: Room, state: _State) -> bool:
        """True when the room HAS a temperature source that has simply gone quiet
        (as opposed to never having had one at all)."""
        return any(state.value(s, "temperature") is not None or state.unreachable(s)
                   for s in room.sensors + room.valves)

    def _window_open(self, room: Room, state: _State, now: float) -> bool:
        """Open on any of three witnesses, best first: a contact sensor in the room,
        our own reading of its temperature trace, or the valve's built-in flag.

        A contact knows an open window that isn't cold yet. Our own detection watches
        the room's real thermometer, which is better placed than the one inside a TRV
        pressed against a radiator — which is why we do it ourselves rather than take
        the valve's word for it."""
        if any(state.value(c, "contact") is True for c in room.contacts):
            return True
        if self._settings.window_detect and room.sensors:
            since = self._window_since.get(room.area_id)
            is_open = detect_window(self._trace.get(room.area_id, []), since, now, self._settings)
            if is_open and since is None:
                self._window_since[room.area_id] = now
                log.info("heating: %s — window detected (temperature falling)", room.name)
            elif not is_open and since is not None:
                self._window_since.pop(room.area_id, None)
                # Start the trace over. The samples that triggered THIS pause are
                # still in it — the room's pre-drop high is the newest thing above
                # a still-cold reading — so leaving them means the very next tick
                # sees that same drop again and opens a fresh pause. That defeats
                # the max_pause cap entirely: a room losing heat faster than it
                # recovers (a cold snap, exactly what the cap exists for) would
                # never actually be heated. Only a drop measured wholly AFTER this
                # pause is a new window.
                self._trace.pop(room.area_id, None)
                log.info("heating: %s — window pause ended", room.name)
            return is_open
        return any(state.value(f"{v}:window_open", "binary") is True for v in room.valves)

    # --- actuation -------------------------------------------------------------

    async def _drive_valves(self, room: Room, state: _State) -> None:
        if not self._settings.enabled or room.target is None:
            return
        target = round(room.target / STEP) * STEP
        for valve in room.valves:
            clamped = self._clamp(valve, target, state)
            reported = state.value(valve, "target_temperature")
            written, at = self._written.get(valve, (None, 0.0))
            age = time.monotonic() - at
            converged = isinstance(reported, int | float) and abs(float(reported) - clamped) < STEP / 2
            if converged:
                # The valve agrees with us — clear any earlier complaint about it.
                self._valve_errors.discard(valve)
            elif written == clamped and age < VERIFY_S:
                pass  # written, still within the window a battery TRV may take to echo
            elif written == clamped and age >= VERIFY_S:
                # The valve had its VERIFY_S window and still hasn't echoed the
                # setpoint: flag it (once) and re-send. The re-send resets the write
                # clock, so a genuinely-deaf valve is retried once every VERIFY_S.
                room.status = ST_VALVE_ERROR
                if valve not in self._valve_errors:
                    self._valve_errors.add(valve)
                    log.error("heating: %s did not take setpoint %.1f °C (reports %r) — check battery/pairing",
                              valve, clamped, reported)
                await self._write_setpoint(valve, clamped, room)
            else:
                await self._write_setpoint(valve, clamped, room)
            await self._hold_manual(valve, state, room)
            await self._sync_window_detection(valve, state, room)

    def _clamp(self, valve: str, target: float, state: _State) -> float:
        """Respect the valve's own limits — writing 21 °C to a TRV capped at 20
        would leave us waiting forever for an echo that can never come."""
        lo = state.value(f"{valve}:min_temperature", "number")
        hi = state.value(f"{valve}:max_temperature", "number")
        if isinstance(lo, int | float):
            target = max(target, float(lo))
        if isinstance(hi, int | float):
            target = min(target, float(hi))
        return target

    async def _write_setpoint(self, valve: str, target: float, room: Room) -> None:
        self._written[valve] = (target, time.monotonic())
        await self._command(valve, "target_temperature", "set_temperature", {"value": target}, room)
        log.info("heating: %s → %.1f °C (%s, %s)", valve, target, room.name, room.profile)

    async def _hold_manual(self, valve: str, state: _State, room: Room) -> None:
        """A TRV carries its own weekly programme. Left in a scheduled preset it will
        overwrite our setpoint at its next internal step — two controllers, one valve,
        and the house drifts. Manual is what makes DIDA the only writer."""
        if not self._settings.force_manual:
            return
        preset = state.value(f"{valve}:preset", "enum")
        if isinstance(preset, str) and preset and preset != "manual":
            options = state.value(f"{valve}:preset", "enum_options")
            if isinstance(options, str) and "manual" not in options:
                return
            log.info("heating: %s preset %r → manual", valve, preset)
            await self._command(f"{valve}:preset", "enum", "set_option", {"value": "manual"}, room)

    async def _sync_window_detection(self, valve: str, state: _State, room: Room) -> None:
        """A TRV's own window detection is what makes the room's window-pause mean
        anything when the room has no contact sensor — and it ships switched OFF, so
        the setting would otherwise be a switch wired to nothing. Held in step with
        the room's choice, in both directions."""
        # Only when nothing better is watching: with a contact sensor, or with our own
        # detection running off a real room thermometer, the valve deciding for itself
        # is a second controller closing the radiator behind our back.
        own_detection = self._settings.window_detect and bool(room.sensors)
        want = room.cfg.window_pause and not room.contacts and not own_detection
        current = state.value(f"{valve}:window_detection", "boolean")
        if isinstance(current, bool) and current is not want:
            log.info("heating: %s window detection → %s (%s)", valve, "on" if want else "off", room.name)
            await self._command(f"{valve}:window_detection", "boolean",
                                "turn_on" if want else "turn_off", {}, room)

    async def _drive_boiler(self, want: bool, state: _State, now: float) -> None:
        settings = self._settings
        if not settings.boiler:
            return
        reported = state.value(settings.boiler, "on_off")
        if reported is None:
            # Neither a start nor a stop can be judged against a relay that is not
            # reporting, and the anti-cycling clock would restart on a guess.
            if not self._boiler_unknown_logged:
                self._boiler_unknown_logged = True
                log.error("heating: boiler %s state unknown — no boiler decision until it reports",
                          settings.boiler)
            self._boiler_on = None
            return
        self._boiler_unknown_logged = False
        is_on = reported is True
        if not settings.enabled or not self._rooms:
            # Master off is "hands off" for the setpoints, but the boiler is the one
            # thing that must be actively driven off: returning early here leaves a
            # firing burner closed indefinitely while the UI shows every room off.
            # Command it off (and keep doing so until it reports off — a single
            # best-effort attempt that the boiler ignores would be silent), logging
            # the warning once.
            if is_on:
                if not self._master_off_logged:
                    self._master_off_logged = True
                    log.warning("heating: boiler running without enabled rooms — turning it off")
                await self._command(settings.boiler, "on_off", "turn_off", {}, None)
            else:
                self._master_off_logged = False
            self._boiler_on = is_on
            self._boiler_since = now
            return
        self._master_off_logged = False
        if self._boiler_on != is_on:
            # Transitions are tracked HERE, not from current_state.updated_at: that
            # column bumps on every report, so an unchanged re-announce would keep
            # resetting the anti-cycling clock and defeat it entirely. The first
            # observation after a restart is dated far enough back to be free to act
            # — a deploy should not hold the boiler hostage for ten minutes.
            back = max(settings.min_on_s, settings.min_off_s) if self._boiler_on is None else 0
            self._boiler_on = is_on
            self._boiler_since = now - back
        if want and is_on and settings.require_open_valve and self._all_valves_shut(state):
            # Every valve confirmed shut while the boiler runs = pumping against a
            # closed circuit. Only ever a reason to STOP: an unknown position counts
            # as open, so this can never block a legitimate start.
            log.warning("heating: every valve is shut — stopping the boiler despite demand")
            want = False
        decision = boiler_decision(want, is_on, now - self._boiler_since, settings)
        if decision is None:
            return
        log.info("heating: boiler %s (%d room(s) calling)", "on" if decision else "off",
                 sum(1 for r in self._rooms if r.demand))
        await self._command(settings.boiler, "on_off", "turn_on" if decision else "turn_off", {}, None)

    def _all_valves_shut(self, state: _State) -> bool:
        positions = [
            state.value(v, "open_close")
            for room in self._rooms if room.cfg.enabled
            for v in room.valves
        ]
        known = [p for p in positions if isinstance(p, int | float)]
        return bool(known) and len(known) == len(positions) and all(p <= 0 for p in known)

    async def _command(self, entity_id: str, capability: str, command: str,
                       args: dict, room: Room | None) -> None:
        source = f"heating:{room.name}" if room is not None else "heating"
        await self._bus.publish_command(await prepare_command(
            self._pool, entity_id, capability, command, args, source=source))

    # --- publishing ------------------------------------------------------------

    async def _publish(self, state: _State) -> None:
        for room in self._rooms:
            eid = f"heating:room:{room.area_id}"
            await self._emit(eid, "text", room.status, room.name)
            await self._emit(eid, "binary", room.demand, room.name)
            if room.temperature is not None:
                await self._emit(eid, "temperature", round(room.temperature, 1), room.name)
            if room.target is not None:
                await self._emit(eid, "target_temperature", room.target, room.name)
        calling = sum(1 for r in self._rooms if r.demand)
        await self._emit("heating:system", "number", float(calling), "Heating")
        await self._emit("heating:system", "text",
                         "off" if not self._settings.enabled else self._settings.mode, "Heating")
        boiler = state.value(self._settings.boiler, "on_off") if self._settings.boiler else None
        if boiler is not None:
            await self._emit("heating:system", "binary", boiler is True, "Heating")

    async def _emit(self, entity_id: str, capability: str, value: object, name: str) -> None:
        """Publish a decision, deduped: unchanged values would otherwise re-enter the
        history firehose every tick and bury the transitions that matter."""
        key = (entity_id, capability)
        if self._published.get(key) == value:
            return
        self._published[key] = value
        await self._bus.publish_state(StateUpdate(
            entity_id=entity_id, capability=capability, value=value,  # type: ignore[arg-type]
            adapter="heating", ts_ns=time.time_ns(),
            name=f"Heating {name}" if entity_id != "heating:system" else "Heating",
            device="heating",
        ))

    # --- state -----------------------------------------------------------------

    async def _read_state(self) -> _State:
        """Every value the config references, with its age. One query — the set is
        tens of rows, and reading it fresh (rather than from the engine's cache) is
        what makes freshness a first-class input."""
        ids: set[str] = set()
        for room in self._rooms:
            ids.update(room.sensors)
            ids.update(room.contacts)
            for valve in room.valves:
                ids.update((valve, f"{valve}:window_open", f"{valve}:preset",
                            f"{valve}:window_detection",
                            f"{valve}:min_temperature", f"{valve}:max_temperature"))
        for eid in (self._settings.boiler, self._settings.outdoor, self._settings.away_helper):
            if eid:
                ids.add(eid)
        rows = await self._pool.fetch(
            "SELECT entity_id, capability, value, updated_at FROM current_state WHERE entity_id = ANY($1)",
            list(ids),
        )
        return _State(rows, self._unreachable())


class _State:
    """A read-only snapshot with timestamps, keyed (entity_id, capability). An entity
    on an unreachable device reads as absent, however recent its last report."""

    __slots__ = ("_at", "_down", "_val")

    def __init__(self, rows, unreachable: frozenset[str]) -> None:
        self._down = unreachable
        self._val: dict[tuple[str, str], object] = {}
        self._at: dict[tuple[str, str], float] = {}
        for r in rows:
            key = (r["entity_id"], r["capability"])
            self._val[key] = r["value"]
            self._at[key] = r["updated_at"].timestamp()

    def value(self, entity_id: str, capability: str = "", *, max_age: float | None = None) -> object:
        """A value, or None when absent — or, with `max_age`, when it is too old to
        act on. A blank capability means "whatever single capability this entity has",
        which is how a helper (`boolean`) or a plain sensor is read without the caller
        having to know its capability name."""
        if entity_id in self._down:
            return None
        if not capability:
            hit = [(k, v) for k, v in self._val.items() if k[0] == entity_id]
            if len(hit) != 1:
                return None
            (key, val) = hit[0]
        else:
            key = (entity_id, capability)
            val = self._val.get(key)
            if val is None:
                return None
        if max_age is not None and time.time() - self._at.get(key, 0.0) > max_age:
            return None
        return val


    def unreachable(self, entity_id: str) -> bool:
        return entity_id in self._down


def _json(raw: str) -> object:
    return json.loads(raw)
