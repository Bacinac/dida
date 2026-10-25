"""The heating model's pure decisions: config validation, which profile a schedule
puts in force, which setpoint wins, the demand latch, and boiler anti-cycling.

These are the rules the house runs on, and every one of them is a pure function of
explicit inputs — so they are tested here rather than inferred from a live boiler in
January.
"""
import msgspec
import pytest
from dida_core.capabilities import CapabilityError
from dida_core.heating import (
    HeatingSettings,
    RoomHeating,
    Slot,
    boiler_decision,
    boiler_demand,
    classify_entities,
    compensated_setpoint,
    demands_heat,
    detect_window,
    frost_for,
    house_away,
    next_step,
    preheat_minutes,
    profile_at,
    profile_setpoint,
    room_vacant,
    setpoint_for,
    target_for,
    validate_room,
    validate_settings,
)


def _rows(*items):
    return [{"entity_id": e, "area_id": a, "capabilities": c} for e, a, c in items]


def test_a_room_knows_its_own_valves_and_thermometer():
    """The whole point of deriving: nobody should have to tell heating what is
    already sitting in the room."""
    rooms, orphans = classify_entities(_rows(
        ("mqtt:thbedroom", 13, ["battery", "humidity", "temperature"]),
        ("mqtt:trbedroom", 13, ["open_close", "target_temperature", "temperature"]),
    ))
    assert rooms[13].valves == ["mqtt:trbedroom"]
    assert rooms[13].sensors == ["mqtt:thbedroom"]
    assert orphans == []


def test_a_relay_and_an_ac_are_not_the_rooms_thermometer():
    """A Shelly reports its own 55 °C and an AC reports the air at its intake —
    neither is what the room is, and a TRV pressed to a radiator isn't either."""
    rooms, _ = classify_entities(_rows(
        ("shelly:relay:switch_0", 17, ["on_off", "temperature"]),
        ("midea:ac", 17, ["hvac_mode", "target_temperature", "temperature"]),
        ("mqtt:trliving", 17, ["target_temperature", "temperature"]),
        ("mqtt:aqtliving", 17, ["humidity", "pm25", "temperature"]),
    ))
    assert rooms[17].sensors == ["mqtt:aqtliving"], "only the air sensor speaks for the room"
    assert rooms[17].valves == ["mqtt:trliving"], "the AC is a device of its own, not a valve"


def test_a_trv_with_a_flat_battery_is_still_a_valve():
    """It stops reporting a setpoint, but it still publishes its own settings —
    and the room must not silently lose its radiator because of a battery."""
    rooms, _ = classify_entities(_rows(
        ("mqtt:trpaula", 7, ["temperature"]),
        ("mqtt:trpaula:comfort_temperature", 7, ["number", "number_options"]),
        ("mqtt:thpaula", 7, ["temperature", "humidity"]),
    ))
    assert rooms[7].valves == ["mqtt:trpaula"]
    assert rooms[7].sensors == ["mqtt:thpaula"]


def test_a_valve_in_no_room_is_named_not_dropped():
    _, orphans = classify_entities(_rows(("mqtt:trstudy", None, ["target_temperature", "temperature"])))
    assert orphans == ["mqtt:trstudy"]


def test_the_controllers_own_output_is_not_mistaken_for_a_device():
    """Each room's decisions are published as `heating:*` entities carrying a
    temperature and a target. Classified, they would read as valves in no room —
    the house looking at its own reflection and reporting it as hardware."""
    rooms, orphans = classify_entities(_rows(
        ("heating:room:4", None, ["temperature", "target_temperature", "binary", "text"]),
        ("heating:system", None, ["binary", "number", "text"]),
        ("mqtt:trbathroom", 4, ["target_temperature", "temperature"]),
    ))
    assert orphans == [], "nothing of ours is a homeless valve"
    assert rooms[4].valves == ["mqtt:trbathroom"]
    assert all(not v.startswith("heating:") for v in rooms[4].valves + rooms[4].sensors)


def test_a_reading_excluded_from_the_room_label_is_not_the_rooms_temperature():
    """The curation already done for the floor-plan label is honoured, not asked
    for a second time in different words."""
    rooms, _ = classify_entities(
        _rows(("mqtt:th1", 4, ["temperature"]), ("mqtt:th2", 4, ["temperature"]),
              ("mqtt:tr1", 4, ["target_temperature", "temperature"])),
        {4: {"excluded": ["mqtt:th2:temperature"]}},
    )
    assert rooms[4].sensors == ["mqtt:th1"]


def test_settings_defaults_are_inert():
    """A house with no heating config must not drive anything."""
    cfg = validate_settings({})
    assert cfg.enabled is False, "actuation is opt-in"
    assert cfg.mode == "auto"


@pytest.mark.parametrize("bad", [
    {"mode": "boost"},                 # not a profile
    {"hysteresis": 0},                 # zero would chatter the boiler
    {"deadband": 99},
    {"min_on_s": -1},
    {"min_off_s": 99999},
    {"summer_cutoff": 999},
    {"frost_target": 25},              # "frost protection" at 25 °C heats the house
    {"stale_after_s": 5},
    {"vacant_minutes": 1},             # a trip to the kitchen is not an empty room
    {"enabled": True},                 # enabled with no boiler drives nothing
])
def test_settings_rejected(bad):
    with pytest.raises(CapabilityError):
        validate_settings(bad)


def test_settings_enabled_needs_a_boiler():
    assert validate_settings({"enabled": True, "boiler": "shelly:x:switch_0"}).enabled is True


@pytest.mark.parametrize("bad", [
    {"offset": 9},                                          # a room is a trim, not a house
    {"offset": -9},
    {"valves": ["mqtt:a", "mqtt:a"]},                       # duplicate
    {"valves": [" "]},                                      # empty id
    {"schedule": [{"days": [0], "at": "6:30", "profile": "comfort"}]},   # not HH:MM
    {"schedule": [{"days": [0], "at": "25:00", "profile": "comfort"}]},
    {"schedule": [{"days": [], "at": "06:30", "profile": "comfort"}]},   # no days
    {"schedule": [{"days": [7], "at": "06:30", "profile": "comfort"}]},
    {"schedule": [{"days": [0], "at": "06:30", "profile": "warm"}]},
    {"override_target": 22},                                # no expiry
])
def test_room_rejected(bad):
    with pytest.raises(CapabilityError):
        validate_room(bad)


def test_room_round_trips():
    room = validate_room({
        "sensor": "mqtt:th", "valves": ["mqtt:tr"],
        "offset": -1.5,
        "schedule": [{"days": [0, 6], "at": "06:30", "profile": "comfort"}],
        "override_target": 22, "override_until": 1_700_000_000,
    })
    assert room.valves == ["mqtt:tr"]
    assert room.schedule[0].days == [0, 6]
    assert room.offset == -1.5


SCHEDULE = [
    Slot(days=[0, 1, 2, 3, 4], at="06:30", profile="comfort"),
    Slot(days=[0, 1, 2, 3, 4], at="09:00", profile="eco"),
    Slot(days=[0, 1, 2, 3, 4], at="16:00", profile="comfort"),
    Slot(days=[0, 1, 2, 3, 4, 5, 6], at="23:00", profile="night"),
    Slot(days=[5, 6], at="08:30", profile="comfort"),
]


@pytest.mark.parametrize("weekday,minutes,expected", [
    (0, 7 * 60, "comfort"),        # Monday morning, after 06:30
    (0, 10 * 60, "eco"),           # mid-morning setback
    (0, 20 * 60, "comfort"),       # evening
    (0, 23 * 60 + 30, "night"),
    (5, 9 * 60, "comfort"),        # Saturday starts later
    (5, 7 * 60, "night"),          # …and before that, Friday's 23:00 night still rules
])
def test_profile_at(weekday, minutes, expected):
    assert profile_at(SCHEDULE, weekday, minutes) == expected


def test_profile_wraps_across_the_week():
    """05:00 Monday is governed by Sunday's last step, not by Monday's first."""
    assert profile_at(SCHEDULE, 0, 5 * 60) == "night"


def test_profile_without_a_schedule_is_comfort():
    """An unscheduled room is one the user wants warm — never one that silently
    runs cold because nobody filled the table in."""
    assert profile_at([], 3, 12 * 60) == "comfort"


def test_target_precedence():
    settings = HeatingSettings(mode="auto", targets={"comfort": 21, "eco": 19, "night": 18, "away": 15})
    room = RoomHeating(schedule=SCHEDULE, override_target=23, override_until=1_000.0)
    # an unexpired override beats everything
    assert target_for(room, settings, 0, 10 * 60, away=False, vacant=False, now=900.0) == (23, "override")
    # once it expires, the schedule is back in charge
    assert target_for(room, settings, 0, 10 * 60, away=False, vacant=False, now=1_001.0) == (19, "eco")
    # an empty house overrides the schedule
    assert target_for(room, settings, 0, 10 * 60, away=True, vacant=False, now=1_001.0) == (15, "away")
    # a pinned house mode overrides the schedule too
    pinned = msgspec.structs.replace(settings, mode="comfort")
    assert target_for(room, pinned, 0, 10 * 60, away=False, vacant=False, now=1_001.0) == (21, "comfort")


def test_a_room_knows_who_could_be_in_it():
    """Any occupancy or motion sensor assigned to the room is a witness — the same
    pair the floor plan lights its presence dot on."""
    rooms, _ = classify_entities(_rows(
        ("mqtt:trstudy", 11, ["open_close", "target_temperature", "temperature"]),
        ("esphome:study:occupancy", 11, ["occupancy"]),
        ("esphome:study:pir_sensor", 11, ["motion"]),
        ("mqtt:mittoilet", 10, ["battery", "occupancy", "temperature"]),
    ))
    assert rooms[11].presence == ["esphome:study:occupancy", "esphome:study:pir_sensor"]
    assert rooms[10].presence == rooms[10].sensors == ["mqtt:mittoilet"], \
        "a thermometer that also sees people is both"


def test_an_empty_room_trims_comfort_to_eco():
    house = HeatingSettings(mode="auto", targets={"comfort": 22, "eco": 19, "night": 18})
    room = RoomHeating(eco_when_empty=True, offset=0.5)
    assert target_for(room, house, 0, 10 * 60, away=False, vacant=True, now=0) == (19.5, "vacant")
    assert target_for(room, house, 0, 10 * 60, away=False, vacant=False, now=0) == (22.5, "comfort")
    assert target_for(RoomHeating(), house, 0, 10 * 60, away=False, vacant=True, now=0) == (22, "comfort"), \
        "only a room that asked for it"


def test_an_empty_room_never_warms_up():
    """Night, away and off are already set for nobody; and an eco setpoint above
    comfort is a typo, not a reason to heat an empty room harder."""
    room = RoomHeating(eco_when_empty=True)
    night = HeatingSettings(mode="night", targets={"comfort": 22, "eco": 19, "night": 18})
    assert target_for(room, night, 0, 0, away=False, vacant=True, now=0) == (18, "night")
    inverted = HeatingSettings(mode="comfort", targets={"comfort": 19, "eco": 21})
    assert target_for(room, inverted, 0, 0, away=False, vacant=True, now=0) == (19, "comfort")
    boosted = RoomHeating(eco_when_empty=True, override_target=23, override_until=100.0)
    assert target_for(boosted, night, 0, 0, away=False, vacant=True, now=50) == (23, "override"), \
        "a hand on the dial outranks an empty room"


@pytest.mark.parametrize("presence,vacant", [
    ([], False),                                   # no sensor, no evidence
    ([(False, 1800.0)], True),                     # quiet for the whole hold
    ([(False, 1799.0)], False),                    # not quite
    ([(False, 3600.0), (True, 5000.0)], False),    # one witness still sees someone
    ([(False, 3600.0), (None, 5000.0)], False),    # one gone silent is not one saying nobody
])
def test_room_vacant(presence, vacant):
    stamped = [(v, 10_000.0 - ago) for v, ago in presence]
    assert room_vacant(stamped, 10_000.0, HeatingSettings(vacant_minutes=30)) is vacant


@pytest.mark.parametrize("flag,away", [
    ([], False),                 # the flag never reported
    ([(True, 1800.0)], True),    # empty for the whole hold
    ([(True, 120.0)], False),    # a phone's GPS blipped outside the zone
    ([(False, 7200.0)], False),  # someone is home
    ([(None, 7200.0)], False),   # the flag's device dropped off
])
def test_an_empty_house_counts_only_once_it_lasts(flag, away):
    stamped = [(v, 10_000.0 - ago) for v, ago in flag]
    assert house_away(stamped, 10_000.0, HeatingSettings(vacant_minutes=30)) is away


def test_the_morning_climb_of_an_empty_room_aims_at_eco():
    house = HeatingSettings(targets={"comfort": 22.0, "eco": 19.0})
    room = RoomHeating(eco_when_empty=True)
    assert profile_setpoint(room, house, "comfort", vacant=True) == (19.0, "vacant")
    assert profile_setpoint(room, house, "comfort", vacant=False) == (22.0, "comfort")
    assert profile_setpoint(room, house, "night", vacant=True) == (18.0, "night")


@pytest.mark.parametrize("valve,room,expected", [
    (15.0, 19.0, 17.0),   # cold radiator reads low: aim lower, or it opens for a warm room
    (25.0, 20.0, 26.0),   # hot radiator reads high: aim higher, or it shuts on a cold room
    (None, 19.0, 21.0),   # no valve reading: write the target as it is
    (15.0, None, 21.0),   # no room reading: nothing to compare against
    (0.0, 20.0, 13.0),    # a valve twenty degrees off is broken: the shift stops at 8
    (40.0, 20.0, 29.0),
])
def test_a_valve_is_aimed_at_the_room_not_at_its_radiator(valve, room, expected):
    assert compensated_setpoint(21.0, valve, room) == expected


def test_target_falls_back_to_the_default_setpoint():
    """A house that never had its setpoints filled in still resolves to something
    sane rather than to no target at all."""
    target, profile = target_for(RoomHeating(), HeatingSettings(), 0, 12 * 60, away=False, vacant=False, now=0)
    assert (target, profile) == (21.0, "comfort")


def test_a_room_is_a_trim_on_the_house_not_its_own_programme():
    """The offset is the whole of a normal room's config: one number that stays
    true when the house setpoints move."""
    house = HeatingSettings(mode="auto", targets={"comfort": 21, "night": 18})
    cool = RoomHeating(offset=-1.5)
    assert setpoint_for(cool, house, "comfort") == 19.5
    assert setpoint_for(cool, house, "night") == 16.5, "the same trim applies to every profile"
    warmer = msgspec.structs.replace(house, targets={"comfort": 23, "night": 18})
    assert setpoint_for(cool, warmer, "comfort") == 21.5, "raise the house, the room follows"


def test_an_offset_cannot_push_a_room_outside_the_legal_range():
    house = HeatingSettings(targets={"comfort": 28})
    assert setpoint_for(RoomHeating(offset=5), house, "comfort") == 30.0


def test_a_room_without_a_schedule_follows_the_house():
    house = HeatingSettings(mode="auto", schedule=SCHEDULE, targets={"comfort": 21, "eco": 19})
    assert target_for(RoomHeating(), house, 0, 10 * 60, away=False, vacant=False, now=0) == (19, "eco")
    own = RoomHeating(schedule=[Slot(days=[0, 1, 2, 3, 4, 5, 6], at="00:00", profile="comfort")])
    assert target_for(own, house, 0, 10 * 60, away=False, vacant=False, now=0) == (21, "comfort"), \
        "a room that disagrees keeps its own"


def test_the_boiler_needs_a_quorum_to_start_but_not_to_keep_going():
    """One cold small room shouldn't fire the burner; two rooms is a heating season.
    Stopping is not symmetric, or it would cycle around the threshold."""
    s = HeatingSettings(min_calling=2)
    assert boiler_demand(1, is_on=False, settings=s) is False
    assert boiler_demand(2, is_on=False, settings=s) is True
    assert boiler_demand(1, is_on=True, settings=s) is True, "one caller keeps it running"
    assert boiler_demand(0, is_on=True, settings=s) is False


def test_demand_latches_across_the_setpoint():
    s = HeatingSettings(hysteresis=0.3, deadband=0.3)
    assert demands_heat(False, 20.7, 21.0, s) is True, "0.3 below target starts the call"
    assert demands_heat(False, 20.9, 21.0, s) is False, "just under target does not"
    assert demands_heat(True, 21.2, 21.0, s) is True, "a call continues past the setpoint"
    assert demands_heat(True, 21.4, 21.0, s) is False, "…and ends a deadband above it"


def test_boiler_anti_cycling():
    s = HeatingSettings(min_on_s=300, min_off_s=600)
    assert boiler_decision(True, False, 700, s) is True, "rested long enough → fire"
    assert boiler_decision(True, False, 100, s) is None, "still resting → wait"
    assert boiler_decision(False, True, 400, s) is False, "burnt long enough → stop"
    assert boiler_decision(False, True, 100, s) is None, "too short a burn → keep going"
    assert boiler_decision(True, True, 10, s) is None, "already where it should be"
    assert boiler_decision(False, False, 10, s) is None


def test_a_schedule_can_say_off():
    """"When do we shut the heating down" is a step like any other — and unlike the
    master switch it still holds the freeze guard rather than letting go."""
    house = HeatingSettings(mode="auto", frost_target=8.0, targets={"comfort": 21},
                            schedule=[Slot(days=[0], at="00:00", profile="comfort"),
                                      Slot(days=[0], at="09:00", profile="off")])
    assert target_for(RoomHeating(), house, 0, 8 * 60, away=False, vacant=False, now=0) == (21, "comfort")
    assert target_for(RoomHeating(), house, 0, 10 * 60, away=False, vacant=False, now=0) == (8.0, "off")


def test_an_off_step_ignores_the_room_offset():
    """A room that runs 1.5° warmer does not get a warmer freeze guard."""
    house = HeatingSettings(mode="off", frost_target=8.0)
    assert target_for(RoomHeating(offset=1.5), house, 0, 12 * 60, away=False, vacant=False, now=0) == (8.0, "off")


def test_detect_window_reads_a_fall_no_heating_can_explain():
    s = HeatingSettings(window_drop=1.2, window_recover=0.3, window_max_pause_s=1800)
    slow = [(0.0, 21.0), (300.0, 20.7), (600.0, 20.4)]     # a room simply cooling
    assert detect_window(slow, None, 600.0, s) is False
    fast = [(0.0, 21.0), (300.0, 20.2), (600.0, 19.4)]     # something is open
    assert detect_window(fast, None, 600.0, s) is True


def test_detect_window_needs_more_than_one_reading():
    s = HeatingSettings()
    assert detect_window([(0.0, 21.0)], None, 0.0, s) is False


def test_the_pause_ends_on_recovery_not_on_symmetry():
    """The room doesn't jump back to where it was; a modest rise off the low is the
    honest signal that the window shut."""
    s = HeatingSettings(window_drop=1.2, window_recover=0.3)
    trace = [(0.0, 21.0), (300.0, 19.4), (600.0, 19.5)]
    assert detect_window(trace, 300.0, 600.0, s) is True, "0.1 back up is noise"
    assert detect_window([*trace, (900.0, 19.8)], 300.0, 900.0, s) is False


def test_the_pause_has_a_time_cap():
    """A room that never recovers is a cold house, and refusing to heat it forever
    would be the worse failure."""
    s = HeatingSettings(window_max_pause_s=600)
    trace = [(0.0, 21.0), (300.0, 19.0), (900.0, 19.0)]
    assert detect_window(trace, 0.0, 900.0, s) is False


def test_frost_protection_follows_the_weather():
    """8 °C is enough at -2 and thin at -15, and the unheated room is the one nobody
    is watching."""
    s = HeatingSettings(frost_target=8.0, frost_cold_below=0.0, frost_per_degree=0.2, frost_max=12.0)
    assert frost_for(s, 5.0) == 8.0, "mild: the base target stands"
    assert frost_for(s, -10.0) == 10.0
    assert frost_for(s, -40.0) == 12.0, "capped"
    assert frost_for(s, None) == 8.0, "no reading is not a reason to guess"


def test_next_step_looks_forward_across_the_week():
    schedule = [Slot(days=[0], at="06:30", profile="comfort"), Slot(days=[0], at="23:00", profile="night")]
    assert next_step(schedule, 0, 5 * 60) == ("comfort", 90)
    assert next_step(schedule, 0, 7 * 60) == ("night", 16 * 60)
    assert next_step(schedule, 1, 12 * 60) == ("comfort", 6 * 24 * 60 + 390 - 720), "wraps to next Monday"
    assert next_step([], 0, 0) is None


def test_preheat_takes_longer_when_it_is_colder_outside():
    s = HeatingSettings(preheat_rate=20.0, preheat_reference=10.0, preheat_cold_factor=0.04,
                        preheat_max_minutes=180)
    assert preheat_minutes(20.0, 21.0, 10.0, s) == 20, "one degree at the reference"
    assert preheat_minutes(20.0, 21.0, -5.0, s) == 32, "the same climb, fifteen degrees colder"
    assert preheat_minutes(21.0, 21.0, -5.0, s) == 0, "already there"
    assert preheat_minutes(10.0, 21.0, -10.0, s) == 180, "capped rather than heating all night"
    assert preheat_minutes(20.0, 21.0, None, s) == 20, "no reading falls back to the plain rate"
