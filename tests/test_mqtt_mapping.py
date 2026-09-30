"""Regression tests for the zigbee2mqtt <-> canonical mapping.

Run inside the mqtt adapter image (dida_adapter_mqtt installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-mqtt:latest \
      -c "python -m pytest tests/test_mqtt_mapping.py"

Covers the unit conversions (brightness 0..254<->%, color_temp mireds<->Kelvin),
the semantic quirks z2m gets wrong-way-round (contact true = CLOSED, LOCK on the
`state` field), and a full command round-trip (canonical -> z2m -> canonical).
"""
import json

import pytest
from dida_adapter_mqtt.mapping import (
    Expose,
    _action,
    _brightness,
    _color_temp,
    _contact_open,
    _facet_name,
    _humanize,
    _lock_state,
    _number,
    _on_off,
    _position,
    build_generic_updates,
    build_state_updates,
    command_to_mqtt,
    device_type_from_exposes,
    facet_category,
    facet_meta,
    generic_command,
    generic_field_spec,
    parse_exposes,
)


# --- unit converters --------------------------------------------------------
def test_unit_converters():
    assert _brightness(254) == 100, "brightness 254 -> 100%"
    assert _brightness(127) == 50, "brightness 127 -> 50%"
    assert _brightness(0) == 0, "brightness 0 -> 0%"
    assert _brightness(300) == 100, "brightness clamps above 254"
    assert _brightness(True) is None, "brightness ignores bool"

    assert _color_temp(250) == 4000, "color_temp 250 mireds -> 4000 K"
    assert _color_temp(500) == 2000, "color_temp 500 mireds -> 2000 K"
    assert _color_temp(100) == 6535, "color_temp clamps to 6535 K max"
    assert _color_temp(0) is None, "color_temp rejects 0 (no divide-by-zero)"

    assert _on_off("ON") is True and _on_off("OFF") is False, "on_off ON/OFF"
    assert _on_off(True) is True, "on_off passes a bool through"

    # z2m gets these semantically inverted / overloaded — the mapping must fix them:
    assert _contact_open(True) is False, "z2m contact:true means CLOSED -> canonical open=False"
    assert _contact_open(False) is True, "z2m contact:false means OPEN -> canonical open=True"
    assert _lock_state("LOCK") is True and _lock_state("UNLOCK") is False, "lock LOCK/UNLOCK"
    assert _position(0) == 0, "position 0 stays 0 (not None — the falsy-0 bug)"


# --- build_state_updates: whole-payload translation -------------------------
def test_build_state_updates():
    u = dict((cap, (val, unit)) for cap, val, unit in build_state_updates({"state": "ON", "brightness": 254}))
    assert u.get("on_off") == (True, None), "payload state:ON -> on_off True"
    assert u.get("brightness") == (100, "%"), "payload brightness:254 -> 100 %"

    u = dict((cap, val) for cap, val, _ in build_state_updates({"contact": True}))
    assert u.get("contact") is False, "payload contact:true -> canonical open=False"

    # a lock speaks LOCK/UNLOCK on the SAME `state` field a switch uses for ON/OFF —
    # it must become a lock capability, never a false on_off=False.
    caps = [cap for cap, _v, _u in build_state_updates({"state": "LOCK"})]
    assert caps == ["lock"], "state:LOCK routes to lock, not on_off"

    u = dict((cap, val) for cap, val, _ in build_state_updates({"local_temperature": 21.5, "presence": True}))
    assert u.get("temperature") == 21.5, "local_temperature -> temperature"
    assert u.get("occupancy") is True, "presence -> occupancy"


# --- command_to_mqtt: canonical -> z2m set payload --------------------------
def test_command_to_mqtt():
    assert command_to_mqtt("on_off", "turn_on", {}) == {"state": "ON"}, "turn_on -> state ON"
    assert command_to_mqtt("brightness", "set_brightness", {"value": 100}) == {"brightness": 254}, \
        "brightness 100% -> 254"
    assert command_to_mqtt("brightness", "set_brightness", {"value": 50}) == {"brightness": 127}, \
        "brightness 50% -> 127"
    assert command_to_mqtt("color_temp", "set_color_temp", {"value": 4000}) == {"color_temp": 250}, \
        "color_temp 4000 K -> 250 mireds"
    assert command_to_mqtt("open_close", "set_position", {"value": 50}) == {"position": 50}, \
        "set_position 50 -> position 50"
    assert command_to_mqtt("lock", "lock", {}) == {"state": "LOCK"}, "lock -> state LOCK"

    # round-trip: a brightness set command, sent to z2m and read back, returns the value.
    sent = command_to_mqtt("brightness", "set_brightness", {"value": 80})
    assert _brightness(sent["brightness"]) == 80, "brightness command round-trips (80% -> 203 -> 80%)"


# --- generic_command: writable-facet /set payload + toggle resolution --------
def test_generic_command():
    assert generic_command("child_lock", "boolean", "turn_on", {}, None) == {"child_lock": True}, \
        "boolean facet turn_on"
    assert generic_command("child_lock", "boolean", "turn_off", {}, None) == {"child_lock": False}, \
        "boolean facet turn_off"
    # toggle: z2m has no generic toggle, so it resolves against the last known value
    # (this is the fix — BOOLEAN advertises toggle, it must not silently no-op).
    assert generic_command("child_lock", "boolean", "toggle", {}, None, current=True) == {"child_lock": False}, \
        "toggle from ON -> off"
    assert generic_command("child_lock", "boolean", "toggle", {}, None, current=False) == {"child_lock": True}, \
        "toggle from OFF -> on"
    # toggle with unknown state should raise, not no-op (fail-loud, not a silent no-op)
    with pytest.raises(ValueError):
        generic_command("child_lock", "boolean", "toggle", {}, None, current=None)
    assert generic_command("state", "number", "set_value", {"value": 3}, None) == {"state": 3}, \
        "number facet set_value"


# --- low-level converter None paths -----------------------------------------
def test_converter_none_paths():
    assert _on_off(123) is None, "on_off ignores a non bool/str value"
    assert _number("x") is None and _number(True) is None, "number rejects non-number and bool"
    assert _action("single") == "single", "non-empty action string passes"
    assert _action("") is None and _action(5) is None, "empty / non-string action -> None"


# --- build_state_updates: skips, units, extra fields ------------------------
def test_build_state_updates_skips_and_extras():
    # a field whose converter returns None is skipped (not emitted as None)
    assert build_state_updates({"occupancy": "notabool", "brightness": True}) == [], \
        "unconvertible values dropped, not surfaced as None"

    # a button 'action' string surfaces as the BUTTON capability
    out = dict((cap, val) for cap, val, _ in build_state_updates({"action": "single"}))
    assert out.get("button") == "single", "action -> button"

    # units come from the canonical capability spec
    u = {cap: (val, unit) for cap, val, unit in build_state_updates({"power": 42.0, "humidity": 55})}
    assert u["power"] == (42.0, "W")
    assert u["humidity"] == (55.0, "%")

    # color_temp mireds -> Kelvin through the whole-payload path
    u = dict((cap, val) for cap, val, _ in build_state_updates({"color_temp": 250}))
    assert u["color_temp"] == 4000


# --- command_to_mqtt: cover/lock/temperature + the loud raise ----------------
def test_command_to_mqtt_cover_temp_and_raise():
    assert command_to_mqtt("open_close", "open", {}) == {"state": "OPEN"}
    assert command_to_mqtt("open_close", "close", {}) == {"state": "CLOSE"}
    assert command_to_mqtt("open_close", "stop", {}) == {"state": "STOP"}
    assert command_to_mqtt("target_temperature", "set_temperature", {"value": 21.5}) \
        == {"current_heating_setpoint": 21.5}, "setpoint °C -> current_heating_setpoint"
    # a capability with no mqtt mapping raises (dropped loud, never a bogus publish)
    with pytest.raises(ValueError):
        command_to_mqtt("temperature", "set", {})


# --- Expose.writable + parse_exposes ----------------------------------------
def test_expose_writable():
    assert Expose(access=1).writable is False, "published-only -> read-only"
    assert Expose(access=3).writable is True, "access bit2 (set) -> writable"


def test_parse_exposes():
    exposes = [
        {"type": "light", "features": [
            {"property": "state", "type": "binary", "access": 7},
            {"property": "brightness", "type": "numeric", "access": 7,
             "value_min": 0, "value_max": 254},
        ]},
        {"property": "child_lock", "type": "binary", "access": 3,
         "value_on": "LOCK", "value_off": "UNLOCK", "label": "Child lock"},
        {"property": "sensitivity", "type": "enum", "access": 3, "values": ["low", "high"]},
        {"not_a_dict": True},          # no property -> skipped
        "junk",                        # not a dict -> skipped
        {"type": "numeric"},           # no property -> skipped
    ]
    out = parse_exposes(exposes)
    assert set(out) == {"state", "brightness", "child_lock", "sensitivity"}, \
        "composite features flattened; malformed entries skipped"
    assert out["child_lock"].value_on == "LOCK" and out["child_lock"].label == "Child lock"
    assert out["child_lock"].writable is True
    assert out["sensitivity"].values == ("low", "high")
    assert out["brightness"].vmin == 0 and out["brightness"].vmax == 254
    assert parse_exposes("notalist") == {}, "non-list exposes -> empty"


def test_device_type_from_exposes():
    assert device_type_from_exposes([{"type": "light", "features": []}]) == "light"
    assert device_type_from_exposes([{"type": "cover", "features": []}]) == "cover"
    assert device_type_from_exposes([{"type": "climate", "features": []}]) is None, \
        "only light/switch/cover/lock are device-type hints"
    assert device_type_from_exposes("notalist") is None
    assert device_type_from_exposes([{"property": "temperature"}]) is None, "sensors-only -> None"


# --- naming + categorisation helpers ----------------------------------------
def test_humanize_and_facet_name():
    assert _humanize("power_outage_count") == "Power outage count"
    assert _humanize("") == "", "empty field stays empty"
    assert _facet_name("child_lock", Expose(label="Child lock")) == "Child lock", "real label wins"
    assert _facet_name("occupancy_timeout", Expose(label="State")) == "Occupancy timeout", \
        "generic 'State' label falls back to the humanized property"
    assert _facet_name("sensitivity", None) == "Sensitivity", "no expose -> humanized field"


def test_facet_category():
    assert facet_category("system_mode", None) == "control", "curated primary control"
    assert facet_category("linkquality", None) == "diagnostic", "curated diagnostic field"
    assert facet_category("x", Expose(category="config")) == "config", "z2m's own category honoured"
    assert facet_category("y", Expose(category="diagnostic")) == "diagnostic"
    assert facet_category("z", Expose(access=1)) == "diagnostic", "read-only leftover -> diagnostic"
    assert facet_category("w", Expose(access=3)) == "config", "writable knob -> config setting"


# --- generic_field_spec: value + expose -> Facet ----------------------------
def test_generic_field_spec_binary():
    f = generic_field_spec("child_lock", "LOCK",
                           Expose(type="binary", access=3, value_on="LOCK", value_off="UNLOCK"))
    assert f.cap == "boolean" and f.value is True, "value_on matched + writable -> boolean True"
    f = generic_field_spec("tamper", True, Expose(type="binary", access=1))
    assert f.cap == "binary" and f.value is True, "read-only bool -> binary sensor"
    f = generic_field_spec("y", "UNLOCK",
                           Expose(type="binary", access=3, value_on="LOCK", value_off="UNLOCK"))
    assert f.cap == "boolean" and f.value is False, "value_off matched -> False"
    assert generic_field_spec("x", "MAYBE",
                              Expose(type="binary", access=3, value_on="LOCK", value_off="UNLOCK")) is None, \
        "value matching neither on nor off -> unrepresentable -> None"


def test_generic_field_spec_numeric():
    f = generic_field_spec("sensitivity", 5,
                           Expose(type="numeric", access=3, unit="x", vmin=0, vmax=10, vstep=1))
    assert f.cap == "number" and f.value == 5.0 and f.unit == "x"
    assert f.options_cap == "number_options"
    assert json.loads(f.options_val) == {"min": 0, "max": 10, "step": 1, "unit": "x"}
    f = generic_field_spec("rssi", -60, Expose(type="numeric", access=1))
    assert f.cap == "measurement" and f.value == -60.0 and f.options_cap is None, "read-only -> measurement"
    assert generic_field_spec("bad", "notnum", Expose(type="numeric", access=3)) is None, \
        "numeric-typed expose but non-numeric value -> None (defensive)"


def test_generic_field_spec_enum_text_and_skips():
    f = generic_field_spec("mode", "auto", Expose(type="enum", access=3, values=("auto", "eco")))
    assert f.cap == "enum" and f.value == "auto"
    assert json.loads(f.options_val) == ["auto", "eco"]
    f = generic_field_spec("state_text", "idle", Expose(type="enum", access=1, values=("idle",)))
    assert f.cap == "text", "read-only enum (no set access) -> plain text"
    assert generic_field_spec("linkquality", 120, None) is None, "ignored transport field skipped"
    assert generic_field_spec("anything", None, None) is None, "null value skipped"
    f = generic_field_spec("flag", True, None)
    assert f.cap == "binary" and f.value is True, "untyped bool inferred as read-only binary"
    assert generic_field_spec("blob", [1, 2], Expose(type="composite", access=3)) is None, \
        "composite / list value -> skipped"


def test_build_generic_updates():
    exposes = parse_exposes([
        {"property": "child_lock", "type": "binary", "access": 3},
        {"property": "sensitivity", "type": "numeric", "access": 3, "value_min": 0, "value_max": 9},
    ])
    facets = build_generic_updates(
        {"state": "ON", "child_lock": True, "sensitivity": 5, "linkquality": 99}, exposes)
    fields = {f.field for f in facets}
    assert "state" not in fields, "semantic fields handled by build_state_updates, not here"
    assert "linkquality" not in fields, "ignored transport field dropped"
    assert fields == {"child_lock", "sensitivity"}


# --- facet_meta: shape-from-exposes (no value) ------------------------------
def test_facet_meta():
    cap, ocap, oval, name, unit, _cat = facet_meta(
        "sensitivity",
        Expose(type="numeric", access=3, unit="lx", vmin=0, vmax=100, vstep=5, label="Sensitivity"))
    assert cap == "number" and ocap == "number_options" and unit == "lx" and name == "Sensitivity"
    assert json.loads(oval) == {"min": 0, "max": 100, "step": 5, "unit": "lx"}
    assert facet_meta("rssi", Expose(type="numeric", access=1))[0] == "measurement", "read-only numeric"
    cap, ocap, oval, *_ = facet_meta("mode", Expose(type="enum", access=3, values=("a", "b")))
    assert cap == "enum" and ocap == "enum_options" and json.loads(oval) == ["a", "b"]
    assert facet_meta("st", Expose(type="enum", access=1))[0] == "text", "read-only enum -> text"
    assert facet_meta("sw", Expose(type="binary", access=3))[0] == "boolean", "writable binary -> boolean"
    assert facet_meta("bl", Expose(type="binary", access=1))[0] == "binary", "read-only binary"
    assert facet_meta("note", Expose(type="text", access=1))[0] == "text"
    assert facet_meta("state", Expose(type="binary")) is None, "semantic field owned by _FIELD_MAP"
    assert facet_meta("linkquality", Expose(type="numeric")) is None, "ignored field -> None"
    assert facet_meta("x", None) is None, "no expose -> None"
    assert facet_meta("y", Expose(type="composite")) is None, "untypeable composite -> None"


# --- generic_command: enum + the loud raises --------------------------------
def test_generic_command_enum_and_raises():
    assert generic_command("mode", "enum", "set_option", {"value": "eco"}, None) == {"mode": "eco"}
    with pytest.raises(ValueError):
        generic_command("child_lock", "boolean", "flip", {}, None)
    with pytest.raises(ValueError):
        generic_command("mode", "enum", "bogus", {}, None)
    # a whole-number float collapses to int; a fractional value stays float
    assert generic_command("x", "number", "set_value", {"value": 3.0}, None) == {"x": 3}
    assert generic_command("x", "number", "set_value", {"value": 2.5}, None) == {"x": 2.5}
