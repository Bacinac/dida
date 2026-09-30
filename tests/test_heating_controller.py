"""The heating controller against a fake house: what it writes to the valves, when
it fires the boiler, and — the part that matters most — every case where it must
refuse to heat and say why.

The controller is driven with a stub pool (the four queries it makes) and a stub
bus that records commands, so a whole heating season's worth of situations runs in
milliseconds. Times are supplied, never slept through.
"""
import json
import time

from dida_automation import heating as H
from dida_automation.heating import HeatingController


class Row(dict):
    """asyncpg rows are mappings; dict is close enough for the columns we read."""


class FakeBus:
    def __init__(self):
        self.commands = []
        self.states = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def publish_state(self, update):
        self.states.append(update)


class FakePool:
    """Dispatches the controller's queries by shape. `entities` is what each room
    CONTAINS (entity_id, area_id, capabilities) — the same registry the derivation
    reads; `state` is {(entity_id, capability): (value, age_seconds)}."""

    def __init__(self, settings: dict, rooms: list[tuple[int, str, dict]], state: dict,
                 entities: list[tuple[str, int | None, list[str]]]):
        self.settings = settings
        self.rooms = rooms
        self.state = state
        self.entities = entities

    async def fetchval(self, sql, *args):
        if "current_state" in sql:  # a command's device limits
            value = self.state.get((args[0], args[1]))
            return value[0] if value else None
        assert "app_settings" in sql
        return json.dumps(self.settings)

    async def fetch(self, sql, *args):
        if "adapter_config" in sql:
            return []
        if "sensor_config" in sql:
            return [Row(id=i, sensor_config=None) for i, _, _ in self.rooms]
        if "FROM entities" in sql:
            return [Row(entity_id=e, area_id=a, capabilities=c) for e, a, c in self.entities]
        if "FROM areas" in sql:
            return [Row(id=i, name=n, kind=None, heating_config=c) for i, n, c in self.rooms]
        assert "current_state" in sql
        wanted = set(args[0])
        now = time.time()
        return [
            Row(entity_id=eid, capability=cap, value=val,
                updated_at=_Stamp(now - age))
            for (eid, cap), (val, age) in self.state.items()
            if eid in wanted
        ]


class _Stamp:
    def __init__(self, epoch: float):
        self._epoch = epoch

    def timestamp(self) -> float:
        return self._epoch


BOILER = "shelly:relay:switch_0"
SENSOR = "mqtt:thbedroom"
VALVE = "mqtt:trbedroom"


def settings(**over) -> dict:
    base = {"enabled": True, "boiler": BOILER, "mode": "comfort", "outdoor": "eco:outdoor",
            "targets": {"comfort": 21.0}}
    base.update(over)
    return base


def room(**over) -> dict:
    # No sensor, no valves: the room's own entities supply both. A config that had
    # to name them would be the same fact written down twice.
    base = {}
    base.update(over)
    return base


# The bedroom as the registry knows it: a wall thermometer and a TRV, both assigned
# to the room; the boiler relay lives elsewhere in the house.
ENTITIES = [
    (SENSOR, 7, ["battery", "humidity", "temperature"]),
    (VALVE, 7, ["open_close", "target_temperature", "temperature"]),
    (BOILER, 17, ["on_off", "temperature"]),
]


def house(state_over=None, *, settings_over=None, room_over=None, boiler_on=False, outdoor=5.0,
          entities=None):
    state = {
        (SENSOR, "temperature"): (19.0, 60),
        (VALVE, "target_temperature"): (10.0, 60),
        (BOILER, "on_off"): (boiler_on, 60),
        ("eco:outdoor", "temperature"): (outdoor, 60),
    }
    state.update(state_over or {})
    return FakePool(settings(**(settings_over or {})),
                    [(7, "Bedroom", room(**(room_over or {})))], state,
                    list(ENTITIES if entities is None else entities))


async def run(pool, unreachable=()) -> tuple[HeatingController, FakeBus]:
    bus = FakeBus()
    ctrl = HeatingController(bus, pool, lambda: frozenset(unreachable))
    await ctrl.reload()
    await ctrl.tick()
    return ctrl, bus


def commands(bus, entity=None):
    return [c for c in bus.commands if entity is None or c.entity_id == entity]


def published(bus, entity, capability):
    hits = [s.value for s in bus.states if s.entity_id == entity and s.capability == capability]
    return hits[-1] if hits else None


async def test_a_room_is_heated_because_it_holds_a_valve():
    """No config at all: the room is on the page and under control purely because a
    TRV is assigned to it, and its thermometer comes from the same place."""
    pool = house()
    pool.rooms = [(7, "Bedroom", None)]
    ctrl, bus = await run(pool)
    assert [r.area_id for r in ctrl._rooms] == [7]
    assert ctrl._rooms[0].valves == [VALVE] and ctrl._rooms[0].sensors == [SENSOR]
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]


async def test_a_room_without_a_valve_is_not_a_heated_room():
    pool = house(entities=[(SENSOR, 7, ["temperature"]), (BOILER, 17, ["on_off"])])
    pool.rooms = [(7, "Bedroom", None)]
    ctrl, bus = await run(pool)
    assert ctrl._rooms == []
    assert bus.commands == []


async def test_a_config_may_still_override_what_the_room_contains():
    """The escape hatch stays: name a sensor and it wins over the room's own."""
    pool = house({("mqtt:other", "temperature"): (15.0, 60)}, room_over={"sensor": "mqtt:other"})
    ctrl, _ = await run(pool)
    assert ctrl._rooms[0].sensors == ["mqtt:other"]
    assert ctrl._rooms[0].temperature == 15.0


async def test_cold_room_gets_its_setpoint_and_fires_the_boiler():
    ctrl, bus = await run(house())
    setpoint = commands(bus, VALVE)
    assert [c.command for c in setpoint] == ["set_temperature"]
    assert setpoint[0].args["value"] == 21.0
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING
    assert published(bus, "heating:room:7", "binary") is True
    assert ctrl._rooms[0].demand is True


async def test_commands_carry_the_heating_source():
    """Every write is attributable in the command audit trail."""
    _, bus = await run(house())
    assert all(c.source.startswith("heating") for c in bus.commands)


async def test_warm_room_does_not_fire_the_boiler():
    _, bus = await run(house({(SENSOR, "temperature"): (22.0, 60)}))
    assert commands(bus, BOILER) == []
    assert published(bus, "heating:room:7", "text") == H.ST_IDLE


async def test_master_switch_off_drives_nothing():
    _, bus = await run(house(settings_over={"enabled": False, "boiler": ""}))
    assert bus.commands == [], "a house with heating off must not write anywhere"
    assert published(bus, "heating:room:7", "text") == H.ST_OFF


async def test_disabled_room_gets_the_frost_setpoint():
    """Switching a room off is not the same as abandoning it — the radiator still
    has water in it."""
    _, bus = await run(house(room_over={"enabled": False}))
    assert commands(bus, VALVE)[0].args["value"] == 8.0
    assert commands(bus, BOILER) == []


async def test_summer_cutoff_blocks_heating():
    _, bus = await run(house(outdoor=25.0))
    assert commands(bus, BOILER) == []
    assert commands(bus, VALVE)[0].args["value"] == 8.0, "valves park at frost out of season"
    assert published(bus, "heating:room:7", "text") == H.ST_SUMMER


async def test_the_valves_own_flag_is_used_only_where_nothing_better_exists():
    """A room with no thermometer of its own has nothing to reason from, so the
    valve's built-in guess is all there is."""
    pool = house({(f"{VALVE}:window_open", "binary"): (True, 30), (VALVE, "temperature"): (19.0, 60)},
                 entities=[(VALVE, 7, ["target_temperature", "temperature"]), (BOILER, 17, ["on_off"])])
    _, bus = await run(pool)
    assert commands(bus, VALVE)[0].args["value"] == 8.0
    assert commands(bus, BOILER) == []
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW


async def test_a_room_with_a_thermometer_ignores_the_valves_guess():
    """The valve reads the air at the radiator; the room has a better witness, and
    two answers to one question is how a house ends up arguing with itself."""
    _, bus = await run(house({(f"{VALVE}:window_open", "binary"): (True, 30)}))
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING


async def test_our_own_detection_pauses_a_room_that_cools_too_fast():
    """No contact sensor, no reliance on the valve: a degree and a half gone in a
    tick is not weather."""
    pool = house()
    ctrl, bus = await run(pool)
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING
    pool.state[(SENSOR, "temperature")] = (17.0, 30)   # someone opened the window
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW
    assert commands(bus, VALVE)[-1].args["value"] == 8.0, "the valve is parked at frost"
    assert ctrl._rooms[0].demand is False, "and it stops asking for the boiler"

    pool.state[(SENSOR, "temperature")] = (17.4, 30)   # …and shut it again
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING, "recovery ends the pause"


async def test_our_own_detection_gives_up_rather_than_freeze_the_house():
    """A room that never recovers is a cold house, not an open window."""
    pool = house(settings_over={"window_max_pause_s": 60})
    ctrl, bus = await run(pool)
    pool.state[(SENSOR, "temperature")] = (17.0, 30)
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW
    ctrl._window_since[7] = time.time() - 120   # the cap has passed
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING

    # …and it must STAY heating. The samples that opened the pause are still the
    # room's recent history, so leaving them in the trace makes the next tick see
    # the same drop and start a fresh pause — the cap would cap nothing, and a room
    # in a cold snap (exactly what it exists for) would never be heated at all.
    for _ in range(3):
        await ctrl.tick()
        assert published(bus, "heating:room:7", "text") == H.ST_HEATING, \
            "the cap must not be defeated by re-detecting the same drop"

    # A genuinely NEW drop, measured after the pause, still pauses the room again.
    pool.state[(SENSOR, "temperature")] = (15.0, 30)
    await ctrl.tick()
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW, \
        "a fresh fall is still a window — the cap suppresses the OLD drop, not detection"


async def test_stale_reading_does_not_heat():
    """A sensor that stopped reporting is a fault, not a cold room: heating on a
    two-day-old number is how a house ends up at 30 °C."""
    _, bus = await run(house({(SENSOR, "temperature"): (19.0, 3 * 3600)}))
    assert commands(bus, BOILER) == []
    assert published(bus, "heating:room:7", "text") == H.ST_STALE


async def test_missing_sensor_is_reported_as_such():
    pool = house()
    del pool.state[(SENSOR, "temperature")]
    _, bus = await run(pool)
    assert commands(bus, BOILER) == []
    assert published(bus, "heating:room:7", "text") == H.ST_NO_SENSOR


async def test_a_room_with_no_thermometer_falls_back_to_the_valve():
    """Biased by the radiator, but a room with only a TRV in it is still heated."""
    pool = house({(VALVE, "temperature"): (18.0, 60)},
                 entities=[(VALVE, 7, ["target_temperature", "temperature"]), (BOILER, 17, ["on_off"])])
    ctrl, bus = await run(pool)
    assert ctrl._rooms[0].sensors == []
    assert ctrl._rooms[0].temperature == 18.0
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]


async def test_setpoint_is_not_rewritten_once_the_valve_agrees():
    pool = house({(VALVE, "target_temperature"): (21.0, 60)})
    _, bus = await run(pool)
    assert commands(bus, VALVE) == [], "the valve already sits at the target"


async def test_setpoint_is_clamped_to_the_valve_limits():
    pool = house({(f"{VALVE}:max_temperature", "number"): (20.0, 60)})
    _, bus = await run(pool)
    assert commands(bus, VALVE)[0].args["value"] == 20.0


async def test_a_valve_that_never_echoes_is_reported_faulty():
    """The failure this whole verification exists for: a TRV with a flat battery
    takes the command and does nothing. It must show as broken, not as heating."""
    pool = house()
    ctrl, bus = await run(pool)
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING
    # …the write is now older than the window a slow battery TRV is given
    ctrl._written[VALVE] = (21.0, time.monotonic() - H.VERIFY_S - 1)
    bus.commands.clear()
    await ctrl.tick()
    assert published(bus, "heating:room:7", "text") == H.ST_VALVE_ERROR
    assert [c.command for c in commands(bus, VALVE)] == ["set_temperature"], "and it is retried"


async def test_a_valve_on_its_own_programme_is_put_back_to_manual():
    pool = house({(f"{VALVE}:preset", "enum"): ("schedule", 60),
                  (f"{VALVE}:preset", "enum_options"): ('["schedule", "manual"]', 60)})
    _, bus = await run(pool)
    presets = commands(bus, f"{VALVE}:preset")
    assert [c.args["value"] for c in presets] == ["manual"]


async def test_min_burn_keeps_a_satisfied_house_running():
    """A boiler that just fired is not stopped two minutes later."""
    pool = house({(SENSOR, "temperature"): (22.0, 60)}, boiler_on=True)
    bus = FakeBus()
    ctrl = HeatingController(bus, pool, frozenset)
    await ctrl.reload()
    ctrl._boiler_on, ctrl._boiler_since = True, time.time() - 60  # just fired
    await ctrl.tick()
    assert commands(bus, BOILER) == [], "the minimum burn has not elapsed"
    ctrl._boiler_since = time.time() - 400
    await ctrl.tick()
    assert [c.command for c in commands(bus, BOILER)] == ["turn_off"]


async def test_a_restart_does_not_hold_the_boiler_hostage():
    """The anti-cycling clock lives in this process, so the first pass after a
    deploy must be free to act rather than sit out a full minimum-rest."""
    _, bus = await run(house())
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]


async def test_a_running_boiler_stops_when_every_valve_is_shut():
    """Demand with every valve confirmed closed means the pump is working against a
    dead circuit — the one case that overrides the room's own call for heat."""
    pool = house({(VALVE, "open_close"): (0, 30)}, boiler_on=True)
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, BOILER)] == ["turn_off"]


async def test_an_unknown_valve_position_never_blocks_a_start():
    """Only positive evidence stops the boiler: the four TRVs that report no
    position at all must not be read as 'shut'."""
    pool = house(boiler_on=False)
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]


async def test_empty_house_uses_the_away_setpoint():
    pool = house({("helper:house_empty", "boolean"): (True, 60)},
                 settings_over={"away_helper": "helper:house_empty",
                                "targets": {"comfort": 21.0, "away": 16.0}})
    _, bus = await run(pool)
    assert commands(bus, VALVE)[0].args["value"] == 16.0


async def test_unchanged_decisions_are_not_republished():
    """Re-emitting every value each tick would bury the transitions in history."""
    ctrl, bus = await run(house())
    bus.states.clear()
    await ctrl.tick()
    assert bus.states == []


async def test_invalid_room_config_is_skipped_not_fatal(caplog):
    pool = house()
    pool.rooms.append((9, "Broken", {"offset": 99}))
    ctrl, bus = await run(pool)
    assert [r.area_id for r in ctrl._rooms] == [7], "the bad room is dropped, the good one runs"
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]
    # …and it says so ONCE: a config that stays broken must not fill the log with
    # the same line every half-minute for the rest of the winter. Proven against a
    # FRESH controller, so the first reload is seen to log before the repeats are
    # seen not to — a silent logger would fail this, not pass it.
    caplog.set_level("ERROR", logger="dida.automation.heating")
    caplog.clear()
    fresh = HeatingController(FakeBus(), pool, frozenset)
    await fresh.reload()
    assert len([r for r in caplog.records if "invalid" in r.message]) == 1, "the fault is reported"
    await fresh.reload()
    await fresh.reload()
    assert len([r for r in caplog.records if "invalid" in r.message]) == 1, "…and not repeated"


async def test_a_room_that_may_not_start_the_boiler_does_not():
    """The toilet being half a degree down is a poor reason to fire the burner."""
    pool = house(room_over={"can_call_boiler": False})
    _, bus = await run(pool)
    assert commands(bus, BOILER) == [], "it wants heat, but it may not ask for it"
    assert commands(bus, VALVE)[0].args["value"] == 21.0, "…and its valve is still set"


async def test_the_boiler_waits_for_a_quorum():
    pool = house(settings_over={"min_calling": 2})
    _, bus = await run(pool)
    assert commands(bus, BOILER) == [], "one room calling is not enough to start"


async def test_a_room_offset_shifts_the_house_setpoint():
    _, bus = await run(house(room_over={"offset": -1.5}))
    assert commands(bus, VALVE)[0].args["value"] == 19.5


async def test_a_contact_sensor_in_the_room_pauses_it():
    """A window sensor is a better witness than a valve inferring a draught."""
    pool = house({("mqtt:windowbedroom", "contact"): (True, 30)},
                 entities=[*ENTITIES, ("mqtt:windowbedroom", 7, ["contact", "battery"])])
    _, bus = await run(pool)
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW
    assert commands(bus, VALVE)[0].args["value"] == 8.0
    assert commands(bus, BOILER) == []


async def test_the_valves_own_detection_is_switched_on_only_where_it_is_needed():
    """A room with no thermometer has nothing else to detect with, so the valve's
    built-in version — which ships OFF — is switched on for it."""
    pool = house({(f"{VALVE}:window_detection", "boolean"): (False, 60),
                  (VALVE, "temperature"): (19.0, 60)},
                 entities=[(VALVE, 7, ["target_temperature", "temperature"]), (BOILER, 17, ["on_off"])])
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, f"{VALVE}:window_detection")] == ["turn_on"]


async def test_the_valve_does_not_second_guess_our_own_detection():
    pool = house({(f"{VALVE}:window_detection", "boolean"): (True, 60)})
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, f"{VALVE}:window_detection")] == ["turn_off"]


async def test_a_room_with_a_contact_sensor_leaves_the_valve_detection_alone():
    """With a real sensor the valve's guesswork is redundant — and it closing the
    radiator on its own is exactly the second controller we avoid everywhere else."""
    pool = house({(f"{VALVE}:window_detection", "boolean"): (True, 60),
                  ("mqtt:windowbedroom", "contact"): (False, 30)},
                 entities=[*ENTITIES, ("mqtt:windowbedroom", 7, ["contact"])])
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, f"{VALVE}:window_detection")] == ["turn_off"]


async def test_window_pause_off_switches_the_valve_detection_off_too():
    pool = house({(f"{VALVE}:window_detection", "boolean"): (True, 60)},
                 room_over={"window_pause": False})
    _, bus = await run(pool)
    assert [c.command for c in commands(bus, f"{VALVE}:window_detection")] == ["turn_off"]


# A Monday in a Zagreb winter, so "half five in the morning" means that here too.
def monday_at(hour: int, minute: int = 0) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 1, 5, hour, minute, tzinfo=ZoneInfo("Europe/Zagreb")).timestamp()


PREHEAT_HOUSE = {
    "mode": "auto",
    "schedule": [{"days": [0, 1, 2, 3, 4, 5, 6], "at": "06:30", "profile": "comfort"},
                 {"days": [0, 1, 2, 3, 4, 5, 6], "at": "23:00", "profile": "night"}],
    "targets": {"comfort": 21.0, "night": 18.0},
    "preheat_rate": 20.0, "preheat_reference": 10.0, "preheat_cold_factor": 0.04,
}


async def test_the_room_is_warm_at_half_six_not_starting_then():
    """Optimum start. 18 → 21 with 5 °C outside is 3 × 20 × 1.2 = 72 minutes, so the
    climb begins around 05:18 and the room ARRIVES at the scheduled temperature."""
    pool = house({(SENSOR, "temperature"): (18.0, 60)}, settings_over=PREHEAT_HOUSE, outdoor=5.0)
    bus = FakeBus()
    ctrl = HeatingController(bus, pool, frozenset)
    await ctrl.reload()

    await ctrl.tick(monday_at(4, 30))
    assert ctrl._rooms[0].target == 18.0, "still the night step, two hours out"
    assert ctrl._rooms[0].status == H.ST_IDLE

    await ctrl.tick(monday_at(5, 30))
    assert ctrl._rooms[0].target == 21.0, "inside the climb, so the morning target already"
    assert ctrl._rooms[0].status == H.ST_PREHEAT


async def test_a_colder_morning_starts_earlier():
    pool = house({(SENSOR, "temperature"): (18.0, 60)}, settings_over=PREHEAT_HOUSE, outdoor=-10.0)
    bus = FakeBus()
    ctrl = HeatingController(bus, pool, frozenset)
    await ctrl.reload()
    # -10 outside stretches the same climb to 3 × 20 × 1.8 = 108 min → from ~04:42.
    await ctrl.tick(monday_at(4, 30))
    assert ctrl._rooms[0].target == 18.0
    await ctrl.tick(monday_at(5, 0))
    assert ctrl._rooms[0].status == H.ST_PREHEAT, "colder outside, so it sets off sooner"


async def test_a_pinned_mode_is_not_second_guessed_by_preheat():
    """`comfort` pinned is a statement about now; starting early would override the
    person who just made it."""
    sched = [{"days": [0, 1, 2, 3, 4, 5, 6], "at": "06:30", "profile": "comfort"}]
    pool = house(settings_over={"mode": "comfort", "schedule": sched})
    ctrl, _ = await run(pool)
    assert ctrl._rooms[0].status == H.ST_HEATING


async def test_the_frost_floor_rises_with_the_weather():
    """A room switched off in a cold snap gets a higher freeze guard, not the mild
    one it would hold in April."""
    pool = house(outdoor=-10.0, room_over={"enabled": False},
                 settings_over={"frost_target": 8.0, "frost_per_degree": 0.3})
    _, bus = await run(pool)
    assert commands(bus, VALVE)[0].args["value"] == 11.0


# --- a device that dropped off the network is not a witness ----------------------


async def test_a_window_contact_that_dropped_off_while_open_does_not_hold_the_room_at_frost():
    open_window = {("mqtt:windowbedroom", "contact"): (True, 30)}
    with_contact = [*ENTITIES, ("mqtt:windowbedroom", 7, ["contact", "battery"])]
    _, bus = await run(house(open_window, entities=with_contact), unreachable=["mqtt:windowbedroom"])
    assert published(bus, "heating:room:7", "text") == H.ST_HEATING
    _, bus = await run(house(open_window, entities=with_contact))
    assert published(bus, "heating:room:7", "text") == H.ST_WINDOW


async def test_an_unreachable_thermometer_is_stale_at_once_not_after_two_hours():
    _, bus = await run(house(), unreachable=[SENSOR])
    assert published(bus, "heating:room:7", "text") == H.ST_STALE
    assert commands(bus, BOILER) == []


async def test_no_boiler_decision_on_a_relay_that_is_not_reporting(caplog):
    caplog.set_level("ERROR", logger="dida.automation.heating")
    ctrl, bus = await run(house(), unreachable=[BOILER])
    assert commands(bus, BOILER) == []
    assert published(bus, "heating:system", "binary") is None
    await ctrl.tick()
    assert len([r for r in caplog.records if "state unknown" in r.message]) == 1
    _, bus = await run(house())
    assert [c.command for c in commands(bus, BOILER)] == ["turn_on"]
