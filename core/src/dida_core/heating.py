"""Heating model — the typed config the heating controller executes, plus the
PURE decisions it makes.

Same split as automations.py: the API validates on write, the automation service
decodes on load, so a config the API accepted always runs. Everything that decides
something (which profile is active now, does a room call for heat, may the boiler
switch yet) lives here as a pure function over explicit inputs — the controller
only supplies clock, state and bus.

The physical model this drives: TRVs are the room controllers (DIDA writes their
setpoint, the valve does its own proportional control) and the boiler relay is a
plain room-thermostat contact — it fires when at least one room is short of its
target and stops when none is, with anti-cycling on both edges.
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple

import msgspec

from dida_core.capabilities import CapabilityError

# The four setpoints a room carries. A schedule slot picks one; the house mode can
# force one for every room at once.
PROFILES: tuple[str, ...] = ("comfort", "eco", "night", "away")
# `off` is not a setpoint but a step the house can be in: heating stands down and
# every room holds frost protection. It is what a schedule means by "and from here
# until morning, nothing" — distinct from the master switch, which is hands-off
# entirely and stops the controller writing at all.
OFF = "off"
SCHEDULE_PROFILES: tuple[str, ...] = (*PROFILES, OFF)
# House mode: `auto` follows the schedule, the rest pin one step for every room.
MODES: tuple[str, ...] = ("auto", *SCHEDULE_PROFILES)

DEFAULT_TARGETS: dict[str, float] = {"comfort": 21.0, "eco": 19.0, "night": 18.0, "away": 15.0}

# A setpoint the user may ask for. Wider than any sane room target on purpose — the
# TRV clamps to its own min/max anyway; this only rejects nonsense.
TARGET_MIN = 5.0
TARGET_MAX = 30.0
# How far a room may trim the house setpoint. A room is a variation on the house,
# not a different house — past a few degrees it is a different profile, not an offset.
MAX_OFFSET = 5.0

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# Caps that make a device an actuator rather than a thermometer. A relay with an
# internal temperature (a Shelly's 55 °C) and a TRV pressed against a radiator both
# report a temperature that is NOT the room's — same rule the floor-plan room label
# already applies, so both surfaces agree on which thermometer speaks for a room.
_CONTROL_CAPS = frozenset({
    "on_off", "open_close", "lock", "hvac_mode", "fan_speed", "media_transport", "target_temperature",
})
# Sub-entities every thermostatic head publishes. They are how a TRV is recognised
# when its battery has died and it has stopped reporting a setpoint entirely.
_VALVE_MARKERS = ("comfort_temperature", "eco_temperature", "valve_detection")


class RoomEntities(NamedTuple):
    """What a room already contains — the heating config's default, not a copy of it."""

    valves: list[str]
    sensors: list[str]
    contacts: list[str]   # door/window sensors in the room (open = true)


def valve_ids(rows) -> set[str]:
    """Every thermostatic head in the registry. ONE definition of what a valve is,
    shared by the room derivation and by the API's write boundary — two rules would
    eventually disagree about the same device, and a relay would slip through as a
    radiator valve.

    A head either reports a setpoint (and is not a full climate unit, which is a
    device in its own right), or it publishes a valve's own settings — which is how
    a TRV with a flat battery, reporting nothing but its temperature, is still
    recognised as one."""
    caps = {r["entity_id"]: set(r["capabilities"] or ()) for r in rows if not _is_own(r["entity_id"])}
    heads = {eid.rsplit(":", 1)[0] for eid in caps if eid.rsplit(":", 1)[-1] in _VALVE_MARKERS}
    return {
        eid for eid, c in caps.items()
        if "hvac_mode" not in c and ("target_temperature" in c or (eid in heads and "temperature" in c))
    }


def _is_own(entity_id: str) -> bool:
    """The controller publishes each room's temperature and target as `heating:*`
    entities. They are its OUTPUT, not devices: left in scope they read as valves
    with no room of their own, and the house would end up chasing its own tail."""
    return entity_id.split(":", 1)[0] == "heating"


def classify_entities(rows, sensor_configs: dict[int, dict | None] | None = None,
                      ) -> tuple[dict[int, RoomEntities], list[str]]:
    """Sort every entity into the room it is assigned to: which are thermostatic
    heads, and which are the room's own thermometers. Returns that mapping plus the
    valves that belong to no room (they can't be heated until someone puts them in
    one, and saying so beats them silently missing).

    `rows` carry entity_id, area_id and capabilities. A room's `sensor_config` — the
    curation the room label already uses — is honoured here rather than duplicated:
    a reading the user excluded from the room's label is not the room's temperature.
    """
    caps = {r["entity_id"]: set(r["capabilities"] or ()) for r in rows}
    valves = valve_ids(rows)

    def is_sensor(c: set[str]) -> bool:
        return "temperature" in c and not (c & _CONTROL_CAPS)

    rooms: dict[int, RoomEntities] = {}
    orphans: list[str] = []
    for r in rows:
        if _is_own(r["entity_id"]):
            continue
        eid, area, c = r["entity_id"], r["area_id"], caps[r["entity_id"]]
        valve, sensor, contact = eid in valves, is_sensor(c), "contact" in c
        if not (valve or sensor or contact):
            continue
        if area is None:
            if valve:
                orphans.append(eid)
            continue
        entry = rooms.setdefault(area, RoomEntities([], [], []))
        if valve:
            entry.valves.append(eid)
        elif sensor and _label_includes(sensor_configs, area, eid):
            entry.sensors.append(eid)
        if contact:
            entry.contacts.append(eid)
    for entry in rooms.values():
        entry.valves.sort()
        entry.sensors.sort()
        entry.contacts.sort()
    return rooms, sorted(orphans)


def _label_includes(sensor_configs: dict[int, dict | None] | None, area: int, eid: str) -> bool:
    cfg = (sensor_configs or {}).get(area) or {}
    return f"{eid}:temperature" not in set(cfg.get("excluded") or ())


async def derive_rooms(pool) -> tuple[dict[int, RoomEntities], list[str]]:
    """`classify_entities` over the live registry."""
    rows = await pool.fetch("SELECT entity_id, area_id, capabilities FROM entities")
    areas = await pool.fetch("SELECT id, sensor_config FROM areas")
    return classify_entities(rows, {a["id"]: a["sensor_config"] for a in areas})


class Slot(msgspec.Struct, frozen=True):
    """One step of a room's weekly schedule: from `at` (local HH:MM) on each of
    `days` (0=Mon … 6=Sun), the room runs `profile` — until the next slot. A room
    with no slots runs `comfort` whenever the house mode is `auto`."""

    days: list[int]
    at: str
    profile: str


class RoomHeating(msgspec.Struct, frozen=True):
    """One room's heating config (areas.heating_config) — an OVERLAY, empty by default.

    Everything here is an exception to the house. `offset` is the whole of a normal
    room's config: how much warmer or cooler than the rest of the house it wants to
    be, in °C. A RELATIVE trim rather than four absolute setpoints, because that is
    what a room actually is — "the bedroom runs a degree cooler" stays true when the
    house's comfort setpoint moves, and it is one number instead of four to keep in
    step. An empty `schedule` follows the house schedule, and `sensor`/`valves` are
    the entities the room already contains unless it needs something else.

    `override_target` + `override_until` are the temporary boost ("22° until 18:00"):
    stored, not held in memory, so a restart doesn't drop it and every surface sees
    the same override.
    """

    enabled: bool = True
    sensor: str = ""
    valves: list[str] = []
    offset: float = 0.0
    schedule: list[Slot] = []
    window_pause: bool = True
    # May this room START the boiler? A small room (a toilet, a hallway) being half a
    # degree down is a poor reason to fire the whole house's burner; with this off the
    # room still opens its valve and takes heat whenever the boiler runs for someone
    # else, but never asks for it on its own.
    can_call_boiler: bool = True
    override_target: float | None = None
    override_until: float | None = None  # epoch seconds, UTC


class HeatingSettings(msgspec.Struct, frozen=True):
    """House-wide heating config (app_settings key `heating`).

    `enabled` is the commissioning gate: until it is on, the controller decides and
    publishes everything but never writes a setpoint or touches the boiler. That is
    what makes it safe to configure a live house before the first cold day.
    """

    enabled: bool = False
    mode: str = "auto"
    boiler: str = ""            # relay entity (on_off) wired to the boiler's thermostat contact
    # The house's own setpoints and weekly schedule — what every room runs unless it
    # says otherwise. Heating is a house-wide habit with exceptions, not eight
    # independent programmes that happen to agree.
    targets: dict[str, float] = {}
    schedule: list[Slot] = []
    # How many rooms must be calling before the burner starts. One cold small room is
    # a poor reason to fire the whole house; two is a heating season. Asymmetric on
    # purpose (see `boiler_demand`): it takes N to START, but only 0 to stop, or the
    # boiler would chatter around the threshold.
    min_calling: int = 1
    hysteresis: float = 0.3     # °C below target before a room calls for heat
    deadband: float = 0.3       # °C above target before its call ends
    min_on_s: int = 300         # once fired, keep the boiler on this long (anti-cycling)
    min_off_s: int = 600        # once stopped, keep it off this long
    outdoor: str = ""           # outdoor temperature entity
    summer_cutoff: float = 17.0 # outdoor °C above which no room may fire the boiler
    away_helper: str = ""       # boolean entity (helper:house_empty): true → away profile
    frost_target: float = 8.0   # setpoint written to a room that is off/paused (freeze guard)
    stale_after_s: int = 7200   # a room reading older than this is NOT heated on (fail loud)
    force_manual: bool = True   # hold TRVs in manual so their own weekly schedule can't fight us
    # DIDA's OWN open-window detection: a room cooling far faster than heating can
    # explain. Better evidence than the TRV's built-in version, which watches the air
    # at the radiator; this watches the room's own thermometer.
    window_detect: bool = True
    window_drop: float = 1.2      # °C lost inside the window below = someone opened something
    window_minutes: int = 10      # how far back the drop is measured
    window_recover: float = 0.3   # °C back up from the low = it's shut again
    window_max_pause_s: int = 1800  # give up and heat anyway after this (a cold snap is not a window)
    # Optimum start: begin early enough to ARRIVE at the scheduled temperature, rather
    # than to start heating at it. How much earlier depends on how far the room has to
    # climb and on how cold it is outside — a house loses heat to the outdoors while
    # it is warming up, so the same climb takes longer in January than in April.
    preheat: bool = True
    preheat_rate: float = 20.0        # minutes per °C of climb at the reference outdoor temp
    preheat_reference: float = 10.0   # outdoor °C the rate above was measured at
    preheat_cold_factor: float = 0.04 # each °C below the reference adds this fraction to the rate
    preheat_max_minutes: int = 180    # never start more than this far ahead
    # Frost protection follows the weather: 8 °C is enough at -2 outside and thin at
    # -15, and an unheated room is exactly the one nobody is watching.
    frost_cold_below: float = 0.0     # outdoor °C under which the floor starts rising
    frost_per_degree: float = 0.2     # …by this much per °C below it
    frost_max: float = 12.0
    require_open_valve: bool = True  # stop a running boiler once every valve is confirmed shut


_SETTING_RANGES: tuple[tuple[str, float, float, str], ...] = (
    ("min_calling", 1, 20, " rooms"),
    ("hysteresis", 0.1, 5, " °C"),
    ("deadband", 0.1, 5, " °C"),
    ("min_on_s", 0, 3600, " s"),
    ("min_off_s", 0, 3600, " s"),
    ("summer_cutoff", -20, 40, " °C"),
    ("frost_target", TARGET_MIN, 15, " °C"),
    ("window_drop", 0.2, 10, " °C"),
    ("window_recover", 0.1, 5, " °C"),
    ("window_minutes", 2, 60, ""),
    ("preheat_rate", 1, 120, " min/°C"),
    ("preheat_cold_factor", 0, 1, ""),
    ("preheat_max_minutes", 0, 12 * 60, ""),
    ("frost_per_degree", 0, 2, ""),
)


def validate_settings(raw: object) -> HeatingSettings:
    """Decode + validate the house-wide config. Fail loud — the API turns a
    CapabilityError into a 400 and never stores it."""
    try:
        cfg = msgspec.convert(raw, HeatingSettings)
    except msgspec.ValidationError as exc:
        raise CapabilityError(f"malformed heating settings: {exc}") from exc
    if cfg.mode not in MODES:
        raise CapabilityError(f"heating mode must be one of {list(MODES)}, got {cfg.mode!r}")
    _check_targets(cfg.targets)
    _check_schedule(cfg.schedule)
    for name, lo, hi, unit in _SETTING_RANGES:
        val = getattr(cfg, name)
        if not (math.isfinite(val) and lo <= val <= hi):
            raise CapabilityError(f"{name} must be between {lo} and {hi}{unit}, got {val}")
    if not (math.isfinite(cfg.frost_max) and cfg.frost_target <= cfg.frost_max <= 18):
        raise CapabilityError(
            f"frost_max must be between the frost target and 18 °C, got {cfg.frost_max}")
    if not 60 <= cfg.window_max_pause_s <= 6 * 3600:
        raise CapabilityError(
            f"window_max_pause_s must be between 60 and {6 * 3600} s, got {cfg.window_max_pause_s}")
    if not 60 <= cfg.stale_after_s <= 86400:
        raise CapabilityError(f"stale_after_s must be between 60 and 86400 s, got {cfg.stale_after_s}")
    # Actuation without a relay is a rule that can never do anything — reject it at
    # the boundary rather than run a controller that silently drives nothing.
    if cfg.enabled and not cfg.boiler.strip():
        raise CapabilityError("heating cannot be enabled without a boiler entity")
    return cfg


def validate_room(raw: object) -> RoomHeating:
    """Decode + validate one room's config (fail loud)."""
    try:
        room = msgspec.convert(raw, RoomHeating)
    except msgspec.ValidationError as exc:
        raise CapabilityError(f"malformed room heating config: {exc}") from exc
    if not (math.isfinite(room.offset) and -MAX_OFFSET <= room.offset <= MAX_OFFSET):
        raise CapabilityError(f"room offset must be between -{MAX_OFFSET} and {MAX_OFFSET} °C, got {room.offset}")
    _check_schedule(room.schedule)
    if len(set(room.valves)) != len(room.valves):
        raise CapabilityError("the same valve is listed twice")
    if any(not v.strip() for v in room.valves):
        raise CapabilityError("valve entity_id must not be empty")
    if room.override_target is not None:
        _check_target("override", room.override_target)
        if room.override_until is None:
            # An override with no end never expires — the user would silently lose the
            # schedule for good. The UI always sets both.
            raise CapabilityError("override_target requires override_until")
    if room.override_until is not None and not math.isfinite(room.override_until):
        raise CapabilityError("override_until must be an epoch timestamp")
    return room


def _check_target(what: str, val: float) -> None:
    if not (math.isfinite(val) and TARGET_MIN <= val <= TARGET_MAX):
        raise CapabilityError(f"{what} target must be between {TARGET_MIN} and {TARGET_MAX} °C, got {val}")


def _check_targets(targets: dict[str, float]) -> None:
    """Setpoints, house-wide or per room — same rules either way, so the house and a
    room that overrides it can never disagree about what a legal setpoint is."""
    for profile, val in targets.items():
        if profile not in PROFILES:
            raise CapabilityError(f"unknown heating profile {profile!r}; allowed: {list(PROFILES)}")
        _check_target(profile, val)


def _check_schedule(schedule: list[Slot]) -> None:
    for slot in schedule:
        if not _HHMM.match(slot.at):
            raise CapabilityError(f"schedule time must be HH:MM, got {slot.at!r}")
        if slot.profile not in SCHEDULE_PROFILES:
            raise CapabilityError(
                f"unknown heating profile {slot.profile!r}; allowed: {list(SCHEDULE_PROFILES)}")
        if not slot.days or any(d < 0 or d > 6 for d in slot.days):
            raise CapabilityError("schedule days must be a non-empty list of 0..6 (Mon..Sun)")


def profile_at(schedule: list[Slot], weekday: int, minutes: int) -> str:
    """The profile a schedule puts in force at `minutes` past midnight on `weekday`.

    The active slot is the LAST one that has already started — scanning backwards
    through the week, so 23:00 Sunday still rules at 05:00 Monday. An empty (or
    all-days-unused) schedule means comfort: a room the user never scheduled is a
    room they want warm, not one that silently runs cold.
    """
    for back in range(7):
        day = (weekday - back) % 7
        cutoff = minutes if back == 0 else 24 * 60
        best: tuple[int, str] | None = None
        for slot in schedule:
            if day not in slot.days:
                continue
            start = _minutes(slot.at)
            if start <= cutoff and (best is None or start > best[0]):
                best = (start, slot.profile)
        if best is not None:
            return best[1]
    return "comfort"


def _minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def target_for(room: RoomHeating, settings: HeatingSettings, weekday: int, minutes: int,
               *, away: bool, now: float) -> tuple[float, str]:
    """A room's setpoint right now, and the reason for it (the profile name, or
    `override` / `away`). Precedence: an unexpired manual override beats everything,
    then an empty house, then the house mode, then the schedule — the room's own if
    it has one, otherwise the house's."""
    if room.override_target is not None and room.override_until is not None and now < room.override_until:
        return room.override_target, "override"
    if away:
        profile = "away"
    elif settings.mode != "auto":
        profile = settings.mode
    else:
        profile = profile_at(room.schedule or settings.schedule, weekday, minutes)
    if profile == OFF:
        # Nothing to trim: a room that is off holds the freeze guard, not a setpoint.
        return settings.frost_target, OFF
    return setpoint_for(room, settings, profile), profile


def setpoint_for(room: RoomHeating, settings: HeatingSettings, profile: str) -> float:
    """This room's temperature for a profile: the house setpoint, trimmed by the
    room's offset and held inside the range any setpoint must satisfy."""
    house = settings.targets.get(profile, DEFAULT_TARGETS[profile])
    return min(TARGET_MAX, max(TARGET_MIN, house + room.offset))


def boiler_demand(calling: int, piggyback: int, is_on: bool, settings: HeatingSettings) -> bool:
    """Should the burner be running, given how many rooms are calling for heat?

    `calling` counts rooms allowed to start it; `piggyback` counts rooms that want
    heat but may only take it while it already runs. Starting needs `min_calling`
    real callers; STOPPING needs everyone satisfied. That asymmetry is deliberate —
    a symmetric threshold would stop the boiler the moment the second room reached
    its target and restart it a minute later, which is the cycling this exists to
    prevent."""
    if is_on:
        return calling + piggyback > 0
    return calling >= settings.min_calling


def demands_heat(was_demanding: bool, temperature: float, target: float,
                 settings: HeatingSettings) -> bool:
    """Does this room call for heat? A latch, not a comparison: it starts calling a
    hysteresis below target and keeps calling until a deadband above it, so a room
    sitting exactly on its setpoint can't chatter the boiler."""
    if was_demanding:
        return temperature < target + settings.deadband
    return temperature <= target - settings.hysteresis


def boiler_decision(want_on: bool, is_on: bool, held_for_s: float,
                    settings: HeatingSettings) -> bool | None:
    """The boiler state to write, or None to leave it alone.

    `held_for_s` is how long it has been in its current state. Anti-cycling is the
    only reason this isn't just `want_on`: a gas boiler that is started and stopped
    every couple of minutes wears itself out and heats nothing, so each edge has to
    wait out its minimum. A blocked transition is not lost — the next tick re-asks.
    """
    if want_on == is_on:
        return None
    if want_on and held_for_s < settings.min_off_s:
        return None
    if not want_on and held_for_s < settings.min_on_s:
        return None
    return want_on


def detect_window(samples: list[tuple[float, float]], open_since: float | None,
                  now: float, settings: HeatingSettings) -> bool:
    """Is a window open in this room, judged from its temperature alone?

    A heated room loses heat slowly — walls, furniture and water in the radiator all
    fight the loss. A fall of a degree or more inside a few minutes is not weather
    and not the heating being off; it is an opening. `samples` are (epoch, °C) of the
    room's own thermometer, newest last, already trimmed to the measuring window.

    Closing again is deliberately NOT the mirror of opening: the room does not jump
    back, it recovers slowly, so the pause ends on a modest rise off the low point —
    or on the time cap, because a room that never recovers is a cold house, not an
    open window, and refusing to heat it forever would be the worse failure.

    The cap only holds if the CALLER resets the trace when a pause ends: this
    function is stateless, so with the pre-drop samples still present the next call
    (open_since back to None) measures the very same drop and re-opens the pause
    forever. The controller pops the room's trace for exactly that reason — only a
    drop measured wholly after the pause counts as a new window.
    """
    if len(samples) < 2:
        return open_since is not None
    newest = samples[-1][1]
    if open_since is None:
        return max(t for _, t in samples) - newest >= settings.window_drop
    if now - open_since >= settings.window_max_pause_s:
        return False
    low = min(t for ts, t in samples if ts >= open_since) if any(ts >= open_since for ts, _ in samples) else newest
    return newest - low < settings.window_recover


def frost_for(settings: HeatingSettings, outdoor: float | None) -> float:
    """The freeze guard for the weather actually outside. Flat at the configured
    target until it gets properly cold, then rising — an unheated room is the one
    nobody is watching, and it is also the one with water in the radiator. Without a
    reading the base target stands: a guess in either direction would be worse."""
    if outdoor is None or outdoor >= settings.frost_cold_below:
        return settings.frost_target
    lifted = settings.frost_target + (settings.frost_cold_below - outdoor) * settings.frost_per_degree
    return min(settings.frost_max, lifted)


def next_step(schedule: list[Slot], weekday: int, minutes: int) -> tuple[str, int] | None:
    """The schedule's next step and how many minutes away it is, scanning forward
    through the week. None when the schedule is empty — nothing is coming."""
    best: tuple[str, int] | None = None
    for ahead in range(8):
        day = (weekday + ahead) % 7
        for slot in schedule:
            if day not in slot.days:
                continue
            start = _minutes(slot.at)
            delta = ahead * 24 * 60 + start - minutes
            if delta <= 0:
                continue
            if best is None or delta < best[1]:
                best = (slot.profile, delta)
        if best is not None:
            return best
    return None


def preheat_minutes(current: float, target: float, outdoor: float | None,
                    settings: HeatingSettings) -> int:
    """How long this room needs to climb from `current` to `target`.

    Rate × climb, with the rate stretched by how far below the reference the outdoor
    temperature sits: the house is losing heat to the outside the whole way up, so
    the same climb takes longer in January. Capped, because a room that cannot make
    it should start at its cap and arrive late rather than heat all night."""
    climb = target - current
    if climb <= 0:
        return 0
    rate = settings.preheat_rate
    if outdoor is not None and outdoor < settings.preheat_reference:
        rate *= 1 + settings.preheat_cold_factor * (settings.preheat_reference - outdoor)
    return min(settings.preheat_max_minutes, round(climb * rate))
