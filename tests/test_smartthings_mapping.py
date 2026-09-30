"""Regression tests for the SmartThings cloud <-> canonical mapping.

Run inside the smartthings adapter image (dida_adapter_smartthings installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-smartthings:latest \
      -c "python -m pytest tests/test_smartthings_mapping.py"

Covers map_component (the raw {stcap:{attr:{value,unit}}} status shape -> readings
+ command routes), translate_command (canonical -> ST capability/command/arg,
including named commands and the color/position special cases), the hue/sat <->
hex colour conversions, and the °F -> °C normalisation.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest
from dida_adapter_smartthings.mapping import (
    Route,
    _rev,
    _seconds_until,
    _source_ids,
    _to_c,
    _unit,
    _val,
    hex_to_hs,
    hs_to_hex,
    map_component,
    translate_command,
)


def rmap(comp):
    readings, routes = map_component(comp)
    return {r.cap: r for r in readings}, routes


# --- map_component: ST status -> readings + routes --------------------------
def test_map_component():
    r, routes = rmap({"switch": {"switch": {"value": "on"}}})
    assert r["on_off"].value is True, "switch on -> on_off True"
    assert routes["on_off"].capability == "switch", "on_off route -> switch capability"

    r, _ = rmap({"switchLevel": {"level": {"value": 75}}})
    assert r["brightness"].value == 75 and r["brightness"].unit == "%", "switchLevel 75 -> brightness 75 %"

    r, _ = rmap({"colorTemperature": {"colorTemperature": {"value": 4000}}})
    assert r["color_temp"].value == 4000, "colorTemperature 4000 -> color_temp 4000"

    # °F is normalised to °C at the boundary
    r, _ = rmap({"temperatureMeasurement": {"temperature": {"value": 212, "unit": "F"}}})
    assert r["temperature"].value == 100.0, "212 °F -> 100.0 °C"

    r, _ = rmap({"contactSensor": {"contact": {"value": "open"}}})
    assert r["contact"].value is True, "contactSensor open -> contact True"
    r, _ = rmap({"motionSensor": {"motion": {"value": "active"}}})
    assert r["motion"].value is True, "motionSensor active -> motion True"
    r, _ = rmap({"lock": {"lock": {"value": "locked"}}})
    assert r["lock"].value is True, "lock locked -> lock True"

    # battery is a diagnostic reading, not a primary tile
    r, _ = rmap({"battery": {"battery": {"value": 55}}})
    assert r["battery"].diagnostic is True, "battery -> diagnostic"

    r, routes = rmap({"windowShadeLevel": {"shadeLevel": {"value": 60}}})
    assert r["open_close"].value == 60, "windowShadeLevel 60 -> open_close 60"

    # one reading per capability (dedup): a device exposing both doorControl and
    # windowShade must not emit two open_close facets.
    readings, _ = map_component({
        "windowShade": {"windowShade": {"value": "open"}},
        "doorControl": {"door": {"value": "closed"}},
    })
    assert sum(1 for x in readings if x.cap == "open_close") == 1, "duplicate open_close deduped to one"


# --- translate_command: canonical -> ST -------------------------------------
def test_translate_command():
    assert translate_command("on_off", "turn_on", {}, Route("switch")) == ("switch", "on", None), \
        "turn_on -> switch/on"
    assert translate_command("lock", "lock", {}, Route("lock")) == ("lock", "lock", None), \
        "lock -> lock/lock"
    assert translate_command("brightness", "set_brightness", {"value": 75}, Route("switchLevel", "setLevel")) \
        == ("switchLevel", "setLevel", [75]), "set_brightness -> setLevel [75]"
    assert translate_command("target_temperature", "set_temperature", {"value": 21.5},
                            Route("thermostatCoolingSetpoint", "setCoolingSetpoint")) \
        == ("thermostatCoolingSetpoint", "setCoolingSetpoint", [21.5]), "set_temperature -> setCoolingSetpoint"
    assert translate_command("color_rgb", "set_color", {"value": "#ff0000"}, Route("colorControl", "setColor")) \
        == ("colorControl", "setColor", [{"hue": 0.0, "saturation": 100.0}]), \
        "set_color -> setColor with hue/sat"
    assert translate_command("media_transport", "next", {}, Route("mediaPlayback")) \
        == ("mediaTrackControl", "nextTrack", None), "media next -> mediaTrackControl/nextTrack"
    assert translate_command("open_close", "set_position", {"value": 30}, Route("windowShade", has_level=True)) \
        == ("windowShadeLevel", "setShadeLevel", [30]), "set_position -> setShadeLevel [30]"

    # untranslatable command raises ValueError (drops loud, no bogus cloud call)
    with pytest.raises(ValueError):
        translate_command("nope", "bar", {}, Route("x"))


# --- colour + temperature helpers -------------------------------------------
def test_colour_and_temperature_helpers():
    assert hs_to_hex(0, 100) == "#ff0000", "hue0/sat100 -> red"
    assert hex_to_hs("#ff0000") == {"hue": 0.0, "saturation": 100.0}, "red -> hue0/sat100"
    assert _to_c(212, "F") == 100.0 and _to_c(20, "C") == 20.0, "°F converts, °C passes through"
    assert _to_c(None, "C") is None, "no temperature -> None"
    assert _to_c("hot", "F") is None, "unparseable temperature -> None"


# --- low-level helpers: _val / _rev / _source_ids / _seconds_until ----------
def test_val_missing_paths():
    # missing capability or attribute -> None, never a KeyError out of the boundary
    assert _val({}, "switch", "switch") is None, "missing capability -> None"
    assert _val({"switch": {}}, "switch", "switch") is None, "missing attribute -> None"
    assert _val({"switch": {"switch": "on"}}, "switch", "switch") is None, \
        "malformed (attr not a {value:..} dict) -> None"
    assert _val({"switch": {"switch": {"value": "on"}}}, "switch", "switch") == "on", "well-formed -> value"
    # _unit mirrors _val for the unit sub-key (e.g. a temperature reading's °C/°F)
    assert _unit({}, "temperatureMeasurement", "temperature") is None, "missing capability -> None"
    assert _unit({"temperatureMeasurement": {"temperature": "bad"}},
                 "temperatureMeasurement", "temperature") is None, "malformed attr -> None"
    assert _unit({"temperatureMeasurement": {"temperature": {"unit": "C"}}},
                 "temperatureMeasurement", "temperature") == "C", "well-formed -> unit"


def test_rev():
    m = {"cool": "cool", "wind": "fan_only", "fanOnly": "fan_only"}
    # DIDA value -> the FIRST supported native value mapping to it
    assert _rev(m, ["cool", "wind", "fanOnly"]) == {"cool": "cool", "fan_only": "wind"}, \
        "first native wins per DIDA value; later duplicate dropped"
    assert _rev(m, ["totallyUnknown"]) == {}, "native with no DIDA mapping is skipped"


def test_source_ids():
    assert _source_ids(["HDMI1", "HDMI2"]) == ["HDMI1", "HDMI2"], "plain id list"
    assert _source_ids([{"value": "AV"}, {"name": "TV"}]) == ["AV", "TV"], "id|value|name maps"
    assert _source_ids("notalist") == [], "non-list -> empty"
    assert _source_ids([None, "", {"foo": "bar"}]) == [], "empty / unrecognised entries skipped"


def test_seconds_until():
    future = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    assert 100 <= _seconds_until(future) <= 120, "future instant -> positive countdown"
    past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    assert _seconds_until(past) == 0, "past instant clamps to 0, never negative"
    assert _seconds_until(None) is None, "no timestamp -> None"
    assert _seconds_until("not-a-date") is None, "unparseable -> None"
    assert _seconds_until("2999-01-01T00:00:00Z") > 0, "trailing-Z instant parses"
    # a naive (no-tz) ISO string is assumed UTC, not rejected
    naive = (datetime.now(UTC) + timedelta(seconds=90)).replace(tzinfo=None).isoformat()
    assert _seconds_until(naive) is not None and _seconds_until(naive) > 0, "naive ISO treated as UTC"


# --- map_component: colour / media / input source --------------------------
def test_map_component_colour_and_media():
    r, routes = rmap({"colorControl": {"hue": {"value": 0}, "saturation": {"value": 100}}})
    assert r["color_rgb"].value == "#ff0000", "hue0/sat100 -> red hex"
    assert routes["color_rgb"].capability == "colorControl"

    r, routes = rmap({
        "audioVolume": {"volume": {"value": 30}},
        "audioMute": {"mute": {"value": "muted"}},
        "mediaPlayback": {"playbackStatus": {"value": "playing"}},
    })
    assert r["volume"].value == 30 and r["volume"].unit == "%"
    assert routes["volume"].setter == "setVolume"
    assert r["mute"].value is True, "muted -> mute True"
    assert r["media_transport"].value == "playing"

    # an unknown playback status is normalised to 'idle'
    r, _ = rmap({"mediaPlayback": {"playbackStatus": {"value": "weird"}}})
    assert r["media_transport"].value == "idle"


def test_map_component_input_source():
    r, routes = rmap({"samsungvd.mediaInputSource": {
        "inputSource": {"value": "HDMI1"},
        "supportedInputSources": {"value": [{"id": "HDMI1", "name": "Xbox"}, {"id": "HDMI2"}]},
    }})
    assert r["source"].value == "HDMI1"
    assert routes["source"].capability == "samsungvd.mediaInputSource"
    assert routes["source"].setter == "setInputSource"
    assert json.loads(r["source_options"].value) == ["HDMI1", "HDMI2"], "source ids extracted from maps"
    assert r["source_options"].category == "config"


# --- map_component: climate (AC + thermostat) ------------------------------
def test_map_component_ac_climate():
    r, routes = rmap({
        "temperatureMeasurement": {"temperature": {"value": 23.0, "unit": "C"}},
        "relativeHumidityMeasurement": {"humidity": {"value": 45}},
        "airConditionerMode": {
            "airConditionerMode": {"value": "cool"},
            "supportedAcModes": {"value": ["cool", "heat", "wind"]},
        },
        "thermostatCoolingSetpoint": {"coolingSetpoint": {"value": 21.0, "unit": "C"}},
        "airConditionerFanMode": {
            "fanMode": {"value": "high"},
            "supportedAcFanModes": {"value": ["low", "high", "turbo"]},
        },
    })
    assert r["temperature"].value == 23.0
    assert r["humidity"].value == 45.0 and r["humidity"].unit == "%"
    assert r["hvac_mode"].value == "cool"
    assert json.loads(r["hvac_mode_options"].value) == sorted(["cool", "heat", "fan_only"])
    assert routes["hvac_mode"].setter == "setAirConditionerMode"
    assert routes["hvac_mode"].modes["fan_only"] == "wind", "DIDA fan_only maps back to native 'wind'"
    assert r["target_temperature"].value == 21.0
    assert routes["target_temperature"].setter == "setCoolingSetpoint"
    assert r["fan_mode"].value == "high"
    assert json.loads(r["fan_mode_options"].value) == sorted(["low", "high", "max"])
    assert routes["fan_mode"].modes["max"] == "turbo", "DIDA max maps back to native 'turbo'"


def test_map_component_thermostat():
    # thermostatMode is the elif branch (only when airConditionerMode is absent);
    # likewise the heating setpoint (only when there's no cooling setpoint).
    r, routes = rmap({
        "thermostatMode": {
            "thermostatMode": {"value": "heat"},
            "supportedThermostatModes": {"value": ["off", "heat", "cool", "auto"]},
        },
        "thermostatHeatingSetpoint": {"heatingSetpoint": {"value": 68, "unit": "F"}},
    })
    assert r["hvac_mode"].value == "heat"
    assert routes["hvac_mode"].setter == "setThermostatMode"
    assert r["target_temperature"].value == 20.0, "68 °F heating setpoint -> 20.0 °C"
    assert routes["target_temperature"].setter == "setHeatingSetpoint"


# --- map_component: openings / presence / smoke ----------------------------
def test_map_component_shade_door_presence_smoke():
    # a windowShade with no level: the open/closed string drives 100 / 0 %
    r, routes = rmap({"windowShade": {"windowShade": {"value": "open"}}})
    assert r["open_close"].value == 100
    assert routes["open_close"].capability == "windowShade" and routes["open_close"].has_level is False

    r, routes = rmap({"doorControl": {"door": {"value": "closed"}}})
    assert r["open_close"].value == 0, "door closed -> 0 % open"
    assert routes["open_close"].capability == "doorControl"

    r, _ = rmap({"presenceSensor": {"presence": {"value": "present"}}})
    assert r["occupancy"].value is True, "presence present -> occupancy True"
    r, _ = rmap({"smokeDetector": {"smoke": {"value": "detected"}}})
    assert r["smoke"].value is True, "smoke detected -> smoke True"


# --- map_component: power reports (diagnostic) -----------------------------
def test_map_component_power_reports():
    r, _ = rmap({"powerConsumptionReport": {
        "powerConsumption": {"value": {"power": 150.0, "energy": 2500}}}})
    assert r["power"].value == 150.0 and r["power"].diagnostic is True
    assert r["energy"].value == 2.5, "2500 Wh -> 2.5 kWh"

    # the simpler powerMeter capability is the elif branch
    r, _ = rmap({"powerMeter": {"power": {"value": 42.0}}})
    assert r["power"].value == 42.0 and r["power"].unit == "W"


# --- map_component: appliance + robot operating states ---------------------
def test_map_component_appliance():
    done = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    r, routes = rmap({"washerOperatingState": {
        "machineState": {"value": "run"},
        "supportedMachineStates": {"value": ["run", "pause", "stop"]},
        "washerJobState": {"value": "wash"},
        "completionTime": {"value": done},
    }})
    assert r["enum"].value == "run"
    assert json.loads(r["enum_options"].value) == ["run", "pause", "stop"]
    assert routes["enum"].capability == "washerOperatingState"
    assert routes["enum"].setter == "setMachineState"
    assert r["text"].value == "wash", "job phase surfaced while running"
    assert r["remaining"].value and r["remaining"].value > 0, "remaining seconds computed"

    # a STOPPED appliance clears the latched job phase + countdown (so the last
    # value doesn't stay pinned in current_state forever)
    r, _ = rmap({"dryerOperatingState": {
        "machineState": {"value": "stop"},
        "dryerJobState": {"value": "cooling"},
        "completionTime": {"value": done},
    }})
    assert r["text"].value == "", "stopped -> job phase cleared"
    assert r["remaining"].value == 0, "stopped -> countdown cleared to 0"


def test_map_component_robot_vacuum():
    r, routes = rmap({
        "robotCleanerMovement": {"robotCleanerMovement": {"value": "cleaning"}},
        "robotCleanerCleaningMode": {
            "robotCleanerCleaningMode": {"value": "auto"},
            "supportedCleaningMode": {"value": ["auto", "spot", "map"]},
        },
    })
    assert r["text"].value == "cleaning", "movement is read-only text"
    assert r["enum"].value == "auto", "cleaning mode is the settable enum"
    assert json.loads(r["enum_options"].value) == ["auto", "spot", "map"]
    assert routes["enum"].capability == "robotCleanerCleaningMode"
    assert routes["enum"].setter == "setRobotCleanerCleaningMode"


def test_map_component_dedup_drops_second():
    # two capabilities that both emit the 'text' cap -> only the first survives
    # (one value per (entity, capability); a duplicate would break the keyed render)
    readings, _ = map_component({
        "washerOperatingState": {
            "machineState": {"value": "run"},
            "washerJobState": {"value": "wash"},
        },
        "robotCleanerMovement": {"robotCleanerMovement": {"value": "moving"}},
    })
    texts = [x for x in readings if x.cap == "text"]
    assert len(texts) == 1, "duplicate 'text' cap deduped to one"
    assert texts[0].value == "wash", "first occurrence (appliance job phase) kept"


# --- translate_command: value + named commands not covered above -----------
def test_translate_command_value_setters():
    assert translate_command("color_temp", "set_color_temp", {"value": 4000},
                             Route("colorTemperature", "setColorTemperature")) \
        == ("colorTemperature", "setColorTemperature", [4000])
    assert translate_command("volume", "set_volume", {"value": 25}, Route("audioVolume", "setVolume")) \
        == ("audioVolume", "setVolume", [25])
    assert translate_command("source", "set_source", {"value": "HDMI1"}, Route("mediaInputSource")) \
        == ("mediaInputSource", "setInputSource", ["HDMI1"])
    assert translate_command("enum", "set_option", {"value": "spot"},
                             Route("robotCleanerCleaningMode", "setRobotCleanerCleaningMode")) \
        == ("robotCleanerCleaningMode", "setRobotCleanerCleaningMode", ["spot"])

    # hvac / fan mode: the DIDA value is mapped back to the device's native value
    r = Route("airConditionerMode", "setAirConditionerMode")
    r.modes = {"fan_only": "wind"}
    assert translate_command("hvac_mode", "set_hvac_mode", {"value": "fan_only"}, r) \
        == ("airConditionerMode", "setAirConditionerMode", ["wind"]), "DIDA fan_only -> native wind"
    assert translate_command("hvac_mode", "set_hvac_mode", {"value": "cool"}, r) \
        == ("airConditionerMode", "setAirConditionerMode", ["cool"]), "no reverse map -> pass through"
    rf = Route("airConditionerFanMode", "setFanMode")
    rf.modes = {"max": "turbo"}
    assert translate_command("fan_mode", "set_fan_mode", {"value": "max"}, rf) \
        == ("airConditionerFanMode", "setFanMode", ["turbo"])


def test_translate_command_named_and_openclose():
    assert translate_command("on_off", "turn_off", {}, Route("switch")) == ("switch", "off", None)
    assert translate_command("mute", "mute", {}, Route("audioMute")) == ("audioMute", "mute", None)
    assert translate_command("mute", "unmute", {}, Route("audioMute")) == ("audioMute", "unmute", None)
    assert translate_command("volume", "volume_up", {}, Route("audioVolume")) \
        == ("audioVolume", "volumeUp", None)
    assert translate_command("media_transport", "play", {}, Route("mediaPlayback")) \
        == ("mediaPlayback", "play", None)
    assert translate_command("media_transport", "previous", {}, Route("mediaPlayback")) \
        == ("mediaTrackControl", "previousTrack", None)
    # open_close named directions + stop -> pause
    assert translate_command("open_close", "open", {}, Route("windowShade")) \
        == ("windowShade", "open", None)
    assert translate_command("open_close", "close", {}, Route("windowShade")) \
        == ("windowShade", "close", None)
    assert translate_command("open_close", "stop", {}, Route("windowShade")) \
        == ("windowShade", "pause", None)
    # an open_close command that's none of the above falls through to the loud raise
    with pytest.raises(ValueError):
        translate_command("open_close", "wiggle", {}, Route("windowShade"))
