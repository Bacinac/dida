"""HomeKit characteristics → canonical capabilities, and the perms that gate them.

262 statements at 0%. HAP is a protocol where the wrong answer is always a plausible
one: a contact sensor whose 0/1 means the opposite of what you assume, a
characteristic you may write but must not read, a transient error that looks like
"no motion". None of those raise.

The decoders carry the sharpest edge. `_bool` returns None — not False — for
anything it does not recognise, because False is a STATEMENT: motion=clear fires
the automations that wait for a room to empty. A read error must say "I don't know",
and the engine drops an unknown at the boundary rather than acting on it.
"""

from __future__ import annotations

import pytest
from aiohomekit.model.characteristics import CharacteristicsTypes as C
from dida_adapter_homekit.adapter import HomekitAdapter, _normalize_pin
from dida_adapter_homekit.mapping import READ_MAP, WRITE_MAP, normalize_type


def _dec(uuid: str):
    return READ_MAP[normalize_type(uuid)][1]


def _cap(uuid: str) -> str:
    return READ_MAP[normalize_type(uuid)][0]


# --- decoders -----------------------------------------------------------------


def test_HAP_sends_0_and_1_for_booleans():
    on = _dec(C.ON)
    assert on(1) is True and on(0) is False
    assert on(True) is True and on(False) is False


def test_an_UNRECOGNISED_boolean_reads_as_unknown_not_as_false():
    """The edge this exists for: False means "the room is empty", which fires every
    automation waiting for that. A read error must not be able to say it."""
    motion = _dec(C.MOTION_DETECTED)
    assert motion(None) is None
    assert motion("nonsense") is None
    assert motion(2.5) is None


def test_brightness_is_clamped_into_percent():
    b = _dec(C.BRIGHTNESS)
    assert b(50) == 50
    assert b(140) == 100
    assert b(-3) == 0


def test_brightness_from_a_non_number_is_unknown():
    assert _dec(C.BRIGHTNESS)("bright") is None


def test_a_lock_is_secured_ONLY_on_the_secured_code():
    """HAP LockCurrentState: 1 = secured, 0/2/3 = unsecured/jammed/unknown. Reading
    "jammed" as locked is the one answer that matters and the easiest to get wrong."""
    lock = _dec(C.LOCK_MECHANISM_CURRENT_STATE)
    assert lock(1) is True
    assert lock(0) is False
    assert lock(2) is False, "jammed is not secured"
    assert lock(3) is False, "unknown is not secured"


def test_a_lock_state_that_is_not_an_int_is_unknown():
    assert _dec(C.LOCK_MECHANISM_CURRENT_STATE)(None) is None


def test_a_position_is_clamped_to_0_100():
    p = _dec(C.POSITION_CURRENT)
    assert p(0) == 0 and p(100) == 100
    assert p(120) == 100


def test_temperature_and_humidity_pass_through_as_numbers():
    assert _dec(C.TEMPERATURE_CURRENT)(21.5) == 21.5
    assert _dec(C.RELATIVE_HUMIDITY_CURRENT)(48) == 48


def test_battery_is_mapped_but_marked_DIAGNOSTIC():
    """Otherwise every HomeKit sensor grows a battery tile in the house view."""
    assert READ_MAP[normalize_type(C.BATTERY_LEVEL)][2] is True
    assert READ_MAP[normalize_type(C.MOTION_DETECTED)][2] is False


def test_every_writable_capability_is_also_readable():
    """A control you can set but never read back shows a state the house is only
    guessing at."""
    readable = {cap for cap, _dec, _diag in READ_MAP.values()}
    assert set(WRITE_MAP) <= readable, f"write-only: {sorted(set(WRITE_MAP) - readable)}"


def test_short_and_full_uuids_canonicalise_to_the_same_thing():
    """HAP sends either form depending on the accessory; two spellings of one
    characteristic would be two entities, one of which never updates."""
    assert normalize_type("25") == normalize_type(C.ON)


# --- building entities from an accessory tree ---------------------------------


def _svc(*chars, iid=1):
    return {"iid": iid, "characteristics": list(chars)}


def _char(uuid, iid, perms=("pr",), value=None):
    return {"type": uuid, "iid": iid, "perms": list(perms), "value": value}


def _build(*services, alias="hub"):
    a = HomekitAdapter()
    a._pairings = {alias: {"name": "Hub"}}
    a._build_entities(alias, [{"aid": 1, "services": list(services)}])
    return a


def test_a_readable_characteristic_becomes_a_reading():
    a = _build(_svc(_char(C.NAME, 1, value="Kitchen Motion"),
                    _char(C.MOTION_DETECTED, 2)))
    assert [r.capability for r in a._readings.values()] == [_cap(C.MOTION_DETECTED)]
    assert next(iter(a._readings.values())).name == "Kitchen Motion"


def test_a_characteristic_without_READ_permission_is_not_a_reading():
    """Publishing a value for something never read means inventing it."""
    a = _build(_svc(_char(C.NAME, 1, value="Lamp"),
                    _char(C.ON, 2, perms=("pw",))))
    assert a._readings == {}


def test_a_writable_target_becomes_a_command_target():
    a = _build(_svc(_char(C.NAME, 1, value="Lamp"),
                    _char(C.ON, 2, perms=("pr", "pw"))))
    entity = next(iter(a._readings.values())).entity_id
    assert (entity, _cap(C.ON)) in a._writable


def test_a_READ_ONLY_characteristic_is_not_offered_as_a_control():
    """A sensor with a control on its card is a button that does nothing.

    Uses `ON` deliberately: it IS a write target, so read-only perms are the only
    thing keeping it out of `_writable`. A first draft used MOTION_DETECTED, which
    is in no WRITE_MAP entry at all — so the assertion held no matter what the
    permission check did, and deleting that check left the test green."""
    a = _build(_svc(_char(C.NAME, 1, value="Reporting-only switch"),
                    _char(C.ON, 2, perms=("pr",))))
    assert a._readings, "the fixture must produce a reading, or this proves nothing"
    assert a._writable == {}


def test_a_service_with_nothing_mappable_is_skipped_entirely():
    a = _build(_svc(_char("public.hap.characteristic.firmware.revision", 2)))
    assert a._readings == {} and a._writable == {}


def test_a_service_with_no_NAME_still_gets_a_stable_label():
    """Falling back to the empty string would collapse several services of one
    accessory into a single entity id."""
    a = _build(_svc(_char(C.MOTION_DETECTED, 2), iid=7))
    (reading,) = a._readings.values()
    assert reading.entity_id.startswith("homekit:hub:")
    assert reading.entity_id != "homekit:hub:"


def test_a_device_with_ONLY_diagnostics_is_not_curated():
    """A bare battery probe should not become a tile in the house view."""
    a = _build(_svc(_char(C.NAME, 1, value="Probe"), _char(C.BATTERY_LEVEL, 2)))
    assert a._curated == set()


def test_one_primary_characteristic_curates_the_whole_entity():
    a = _build(_svc(_char(C.NAME, 1, value="Sensor"),
                    _char(C.MOTION_DETECTED, 2), _char(C.BATTERY_LEVEL, 3)))
    assert len(a._curated) == 1


# --- the pairing pin ----------------------------------------------------------


@pytest.mark.parametrize("typed", ["12345678", "123-45-678", "123 45 678"])
def test_a_pin_is_accepted_however_it_was_typed(typed):
    """The code is printed on the accessory with dashes; people type it both ways,
    and a rejected pin reads as "pairing failed"."""
    assert _normalize_pin(typed) == "123-45-678"


def test_a_pin_that_is_not_eight_digits_is_left_alone():
    """Not silently reshaped into something that will fail differently."""
    assert _normalize_pin("1234") == "1234"
