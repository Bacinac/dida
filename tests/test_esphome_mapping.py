"""Regression tests for the ESPHome native-API <-> canonical mapping.

Run inside the esphome adapter image (dida_adapter_esphome installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-esphome:latest \
      -c "python -m pytest tests/test_esphome_mapping.py"

map_entity dispatches on the aioesphomeapi Info class NAME and reads plain
attributes, and decode_state reads plain attributes off the state object — so both
are exercised with duck-typed stand-ins (no live device / API handshake needed).
"""
import asyncio
import json
from types import SimpleNamespace

import aioesphomeapi as api
import pytest
from dida_adapter_esphome.mapping import (
    EntityMap,
    _enum_name,
    _parse_hex,
    decode_state,
    is_diagnostic,
    map_entity,
    send_command,
)
from dida_core import CapabilityKind


def info(cls, **attrs):
    """An object whose class NAME is `cls` (map_entity dispatches on that)."""
    return type(cls, (), attrs)()


def approx(a, b, eps=1e-6):
    return abs(a - b) < eps


# --- _parse_hex -------------------------------------------------------------
def test_parse_hex():
    r, g, b = _parse_hex("#FF8000")
    assert approx(r, 1.0) and approx(g, 128 / 255) and approx(b, 0.0), "#FF8000 -> (1, .502, 0)"
    assert _parse_hex("00FF00") == (0.0, 1.0, 0.0), "hex without # -> (0,1,0)"
    with pytest.raises(ValueError):  # bad hex raises, doesn't return garbage
        _parse_hex("xyz")


# --- map_entity: Info class -> EntityMap ------------------------------------
def test_map_entity():
    em = map_entity(info("SwitchInfo", key=1, object_id="relay"))
    assert em.etype == "switch" and em.caps == ["on_off"], "SwitchInfo -> switch/on_off"

    em = map_entity(info("SensorInfo", key=2, object_id="t", device_class="temperature",
                         unit_of_measurement="°C"))
    assert em.caps == ["temperature"] and em.unit == "°C", "SensorInfo device_class temperature"

    # no device_class → unit fallback ("W" -> power)
    em = map_entity(info("SensorInfo", key=3, object_id="p", device_class=None, unit_of_measurement="W"))
    assert em.caps == ["power"], "SensorInfo unit W -> power (device_class fallback)"

    em = map_entity(info("BinarySensorInfo", key=4, object_id="pir", device_class="motion"))
    assert em.caps == ["motion"], "BinarySensorInfo motion -> motion"

    # a light with brightness + RGB (legacy flags path)
    em = map_entity(info("LightInfo", key=5, object_id="lamp", supported_color_modes=[],
                         legacy_supports_brightness=True, legacy_supports_color_temperature=False,
                         legacy_supports_rgb=True, effects=[]))
    assert "on_off" in em.caps and "brightness" in em.caps and "color_rgb" in em.caps, \
        "LightInfo brightness+rgb -> caps"
    assert em.has_brightness is True and em.has_rgb is True, "light EntityMap flags set"

    # color-mode BITS path (brightness=2, color_temp=8): a light reporting mode 10
    em = map_entity(info("LightInfo", key=6, object_id="l2", supported_color_modes=[10],
                         legacy_supports_brightness=False, legacy_supports_color_temperature=False,
                         effects=[]))
    assert "brightness" in em.caps and "color_temp" in em.caps, "color-mode bits 2|8 -> brightness+color_temp"

    em = map_entity(info("CoverInfo", key=7, object_id="blind", supports_position=True))
    assert em.etype == "cover" and em.supports_position is True, "CoverInfo supports_position"

    assert map_entity(info("CameraInfo", key=9, object_id="cam")) is None, "unsupported type -> None"


# --- decode_state: state object -> readings ---------------------------------
def dmap(readings):
    return {c: (v, u) for c, v, u in readings}


def test_decode_state():
    sw = map_entity(info("SwitchInfo", key=1, object_id="relay"))
    assert decode_state(sw, SimpleNamespace(state=True)) == [("on_off", True, None)], "switch state True"

    sensor = map_entity(info("SensorInfo", key=2, object_id="t", device_class="temperature",
                             unit_of_measurement="°C"))
    assert decode_state(sensor, SimpleNamespace(state="21.5")) == [("temperature", 21.5, "°C")], \
        "sensor '21.5' -> 21.5"
    assert decode_state(sensor, SimpleNamespace(state=float("nan"))) == [], "sensor NaN -> skipped"
    assert decode_state(sensor, SimpleNamespace(state="notnum")) == [], "sensor non-numeric -> skipped"
    assert decode_state(sensor, SimpleNamespace(state=1.0, missing_state=True)) == [], \
        "missing_state -> skipped"

    light = map_entity(info("LightInfo", key=5, object_id="lamp", supported_color_modes=[],
                            legacy_supports_brightness=True, legacy_supports_color_temperature=True,
                            legacy_supports_rgb=True, effects=[]))
    d = dmap(decode_state(light, SimpleNamespace(
        state=True, brightness=0.5, color_temperature=250.0, red=1.0, green=0.0, blue=0.0)))
    assert d["on_off"] == (True, None), "light on_off"
    assert d["brightness"] == (50, "%"), "light brightness 0.5 -> 50 %"
    assert d["color_temp"] == (4000, "K"), "light color_temp 250 mireds -> 4000 K"
    assert d["color_rgb"] == ("#FF0000", None), "light rgb (1,0,0) -> #FF0000"

    cover = map_entity(info("CoverInfo", key=7, object_id="blind", supports_position=True))
    assert decode_state(cover, SimpleNamespace(position=0.5)) == [("open_close", 50, "%")], \
        "cover position 0.5 -> 50 %"


# --- map_entity: the remaining ESPHome Info classes -------------------------
def test_map_entity_text_and_measurement_fallback():
    em = map_entity(info("TextSensorInfo", key=10, object_id="ver"))
    assert em.etype == "text" and em.caps == [CapabilityKind.TEXT.value], "TextSensorInfo -> text"

    # a plain sensor with no device_class AND no recognised unit -> generic
    # MEASUREMENT fallback (a readable sensor is never dropped)
    em = map_entity(info("SensorInfo", key=11, object_id="x", device_class=None,
                         unit_of_measurement=None))
    assert em.caps == [CapabilityKind.MEASUREMENT.value] and em.unit is None, \
        "unknown sensor -> measurement fallback"


def test_map_entity_light_effects():
    # supported_color_modes bit 35 = ON_OFF|BRIGHTNESS|RGB (1|2|32)
    em = map_entity(info("LightInfo", key=12, object_id="strip", supported_color_modes=[35],
                         legacy_supports_brightness=False, legacy_supports_color_temperature=False,
                         legacy_supports_rgb=False, effects=["None", "Rainbow", "Pulse"]))
    assert "brightness" in em.caps and "color_rgb" in em.caps, "mode 35 -> brightness+rgb bits"
    assert "color_temp" not in em.caps, "mode 35 has no color-temp bit"
    assert CapabilityKind.EFFECT.value in em.caps, "effects present -> effect cap"
    assert em.effects == ["Rainbow", "Pulse"], "the sentinel 'None' effect is filtered out"


def test_map_entity_number():
    em = map_entity(info("NumberInfo", key=13, object_id="setpoint", min_value=5.0,
                         max_value=30.0, step=0.5, unit_of_measurement="°C", mode=None))
    assert em.etype == "number"
    assert em.caps == [CapabilityKind.NUMBER.value, CapabilityKind.NUMBER_OPTIONS.value]
    assert em.unit == "°C"
    assert em.number_meta == {"min": 5.0, "max": 30.0, "step": 0.5, "unit": "°C", "mode": ""}, \
        "number_meta carries bounds; mode None -> ''"


def test_map_entity_select():
    em = map_entity(info("SelectInfo", key=14, object_id="preset", options=["Eco", "Boost"]))
    assert em.etype == "select"
    assert em.caps == [CapabilityKind.ENUM.value, CapabilityKind.ENUM_OPTIONS.value]
    assert em.options == ["Eco", "Boost"]


def test_map_entity_button():
    em = map_entity(info("ButtonInfo", key=15, object_id="restart"))
    assert em.etype == "button" and em.caps == [CapabilityKind.PRESS.value]


def test_map_entity_fan():
    em = map_entity(info("FanInfo", key=16, object_id="fan", supports_speed=True,
                         supported_speed_count=3))
    assert em.etype == "fan" and CapabilityKind.FAN_SPEED.value in em.caps
    assert em.fan_speed_count == 3

    # a fan with no speed control -> just on_off, no fan_speed cap
    em = map_entity(info("FanInfo", key=17, object_id="fan2", supports_speed=False,
                         supported_speed_count=0))
    assert em.caps == ["on_off"] and em.fan_speed_count == 0


def test_map_entity_climate():
    em = map_entity(info("ClimateInfo", key=18, object_id="ac",
                         supported_modes=["cool", "heat", "off"],
                         supported_fan_modes=["auto", "low"],
                         supports_current_temperature=True))
    assert em.etype == "climate"
    for c in ("hvac_mode", "hvac_mode_options", "target_temperature", "temperature",
              "fan_mode", "fan_mode_options"):
        assert c in em.caps, f"climate exposes {c}"
    assert em.climate_modes == ["cool", "heat", "off"]
    assert em.climate_fan_modes == ["auto", "low"]
    assert em.has_current_temp is True

    # no fan modes and no current-temp sensor -> leaner cap set
    em = map_entity(info("ClimateInfo", key=19, object_id="ac2",
                         supported_modes=["auto"], supported_fan_modes=[],
                         supports_current_temperature=False))
    assert "fan_mode" not in em.caps and "temperature" not in em.caps
    assert em.has_current_temp is False


# --- helpers: _enum_name / is_diagnostic ------------------------------------
def test_enum_name():
    assert _enum_name(api.ClimateMode.COOL) == "cool", "enum -> lower snake name"
    assert _enum_name(None) == "", "None -> empty string"
    assert _enum_name("FAN_ONLY") == "fan_only", "string input lowered"
    assert _enum_name(2) == "2", "bare int -> its str"
    assert _enum_name(SimpleNamespace(name="HEAT_COOL")) == "heat_cool", "duck-typed enum via .name"


def test_is_diagnostic():
    assert is_diagnostic(SimpleNamespace(entity_category=2)) is True, "category 2 = DIAGNOSTIC"
    assert is_diagnostic(SimpleNamespace(entity_category="ENTITY_CATEGORY_DIAGNOSTIC")) is True, \
        "string category containing DIAGNOSTIC"
    assert is_diagnostic(SimpleNamespace(entity_category=0, device_class="energy",
                                         unit_of_measurement="kWh")) is True, "energy counter hidden"
    assert is_diagnostic(SimpleNamespace(entity_category=0, device_class=None,
                                         unit_of_measurement="kWh")) is True, "kWh unit alone hidden"
    assert is_diagnostic(SimpleNamespace(entity_category=0, device_class="power",
                                         unit_of_measurement="W")) is False, "live power is not diagnostic"


# --- decode_state: the remaining entity types -------------------------------
def test_decode_state_binary_and_text():
    b = map_entity(info("BinarySensorInfo", key=1, object_id="door", device_class="door"))
    assert decode_state(b, SimpleNamespace(state=True)) == [("contact", True, None)], \
        "binary_sensor decodes to its class capability"

    text = map_entity(info("TextSensorInfo", key=2, object_id="ver"))
    assert decode_state(text, SimpleNamespace(state="1.2.3")) == [("text", "1.2.3", None)]
    assert decode_state(text, SimpleNamespace(state=None)) == [], "text None -> skipped"


def test_decode_state_light_effects_and_ct_guard():
    light = map_entity(info("LightInfo", key=3, object_id="strip", supported_color_modes=[],
                            legacy_supports_brightness=True, legacy_supports_color_temperature=True,
                            legacy_supports_rgb=False, effects=["Rainbow"]))
    d = dmap(decode_state(light, SimpleNamespace(
        state=True, brightness=1.0, color_temperature=0.0, effect="Rainbow")))
    assert d["brightness"] == (100, "%"), "brightness 1.0 -> 100 %"
    assert "color_temp" not in d, "color_temperature 0 -> no reading (no divide-by-zero)"
    assert d["effect"] == ("Rainbow", None), "current effect surfaced"
    assert json.loads(d["effect_options"][0]) == ["Rainbow"], "effect_options is a JSON list"


def test_decode_state_cover_without_position():
    cover = map_entity(info("CoverInfo", key=4, object_id="blind", supports_position=False))
    assert decode_state(cover, SimpleNamespace(position=0.5)) == [], \
        "cover with no position support -> no open_close reading"


def test_decode_state_lock():
    lock = map_entity(info("LockInfo", key=5, object_id="door"))
    assert decode_state(lock, SimpleNamespace(state=int(api.LockState.LOCKED))) \
        == [("lock", True, None)], "LockState LOCKED -> lock True"
    assert decode_state(lock, SimpleNamespace(state=int(api.LockState.UNLOCKED))) \
        == [("lock", False, None)], "LockState UNLOCKED -> lock False"


def test_decode_state_number_and_select():
    num = map_entity(info("NumberInfo", key=6, object_id="sp", min_value=0.0, max_value=100.0,
                          step=1.0, unit_of_measurement="%", mode=None))
    out = dmap(decode_state(num, SimpleNamespace(state="42")))
    assert out["number"] == (42.0, "%"), "number '42' -> 42.0"
    assert json.loads(out["number_options"][0])["max"] == 100.0, "number_options carries bounds"
    assert decode_state(num, SimpleNamespace(state="bad")) == [], "non-numeric number -> skipped"
    assert decode_state(num, SimpleNamespace(state=float("inf"))) == [], "inf number -> skipped"

    sel = map_entity(info("SelectInfo", key=7, object_id="preset", options=["Eco", "Boost"]))
    out = dmap(decode_state(sel, SimpleNamespace(state="Eco")))
    assert out["enum"] == ("Eco", None)
    assert json.loads(out["enum_options"][0]) == ["Eco", "Boost"]
    assert decode_state(sel, SimpleNamespace(state=None)) == [], "select None -> skipped"


def test_decode_state_fan():
    fan = map_entity(info("FanInfo", key=8, object_id="fan", supports_speed=True,
                          supported_speed_count=4))
    out = dmap(decode_state(fan, SimpleNamespace(state=True, speed_level=2)))
    assert out["on_off"] == (True, None)
    assert out["fan_speed"] == (50, "%"), "speed_level 2 of 4 -> 50 %"


def test_decode_state_climate():
    clim = map_entity(info("ClimateInfo", key=9, object_id="ac",
                           supported_modes=["cool", "heat"], supported_fan_modes=["auto", "low"],
                           supports_current_temperature=True))
    out = dmap(decode_state(clim, SimpleNamespace(
        mode=int(api.ClimateMode.COOL), target_temperature=22.0, current_temperature=24.5,
        fan_mode=int(api.ClimateFanMode.LOW))))
    assert out["hvac_mode"] == ("cool", None)
    assert json.loads(out["hvac_mode_options"][0]) == ["cool", "heat"]
    assert out["target_temperature"] == (22.0, "°C")
    assert out["temperature"] == (24.5, "°C"), "current temperature surfaced when supported"
    assert out["fan_mode"] == ("low", None)
    assert json.loads(out["fan_mode_options"][0]) == ["auto", "low"]

    # NaN setpoint / current-temp -> those fields are skipped, not emitted as NaN
    out = dmap(decode_state(clim, SimpleNamespace(
        mode=int(api.ClimateMode.HEAT), target_temperature=float("nan"),
        current_temperature=float("nan"), fan_mode=int(api.ClimateFanMode.AUTO))))
    assert "target_temperature" not in out and "temperature" not in out, "NaN temps skipped"
    assert out["hvac_mode"] == ("heat", None)


def test_decode_state_button_is_write_only():
    btn = map_entity(info("ButtonInfo", key=10, object_id="restart"))
    assert decode_state(btn, SimpleNamespace(state=True)) == [], "button has no readable state"


def test_an_event_is_a_momentary_button_carrying_its_type():
    ev = map_entity(info("EventInfo", key=11, object_id="downstairs_button"))
    assert ev.etype == "event" and ev.caps == [CapabilityKind.BUTTON.value]
    assert decode_state(ev, SimpleNamespace(event_type="press")) == [("button", "press", None)]
    assert decode_state(ev, SimpleNamespace(event_type="")) == []


# --- send_command: canonical command -> ESPHome native-API call -------------
class FakeClient:
    """Records the native-API call send_command makes. Every method returns None
    (synchronous form) so _maybe_await does nothing — we only assert the call."""

    def __init__(self):
        self.calls = []

    def switch_command(self, key, state):
        self.calls.append(("switch_command", key, {"state": state}))

    def light_command(self, key, **kw):
        self.calls.append(("light_command", key, kw))

    def cover_command(self, key, **kw):
        self.calls.append(("cover_command", key, kw))

    def lock_command(self, key, cmd):
        self.calls.append(("lock_command", key, {"cmd": cmd}))

    def number_command(self, key, value):
        self.calls.append(("number_command", key, {"value": value}))

    def select_command(self, key, value):
        self.calls.append(("select_command", key, {"value": value}))

    def button_command(self, key):
        self.calls.append(("button_command", key, {}))

    def fan_command(self, key, **kw):
        self.calls.append(("fan_command", key, kw))

    def climate_command(self, key, **kw):
        self.calls.append(("climate_command", key, kw))


def run_cmd(em, command, args=None, *, current_on=None):
    client = FakeClient()
    ok = asyncio.run(send_command(client, em, command, args or {}, current_on=current_on))
    return ok, client.calls


def test_send_command_switch():
    em = EntityMap(1, "relay", "switch")
    ok, calls = run_cmd(em, "turn_on")
    assert ok is True and calls == [("switch_command", 1, {"state": True})]
    assert run_cmd(em, "turn_off")[1][0][2] == {"state": False}
    assert run_cmd(em, "toggle", current_on=True)[1][0][2] == {"state": False}, "toggle when on -> off"
    assert run_cmd(em, "toggle", current_on=False)[1][0][2] == {"state": True}, "toggle when off -> on"
    ok, calls = run_cmd(em, "set_brightness", {"value": 50})
    assert ok is False and calls == [], "switch rejects a non-switch command (loud drop)"


def test_send_command_light():
    em = EntityMap(2, "lamp", "light")
    assert run_cmd(em, "turn_on")[1] == [("light_command", 2, {"state": True})]
    assert run_cmd(em, "turn_off")[1] == [("light_command", 2, {"state": False})]
    assert run_cmd(em, "toggle", current_on=True)[1] == [("light_command", 2, {"state": False})]
    assert run_cmd(em, "set_brightness", {"value": 50})[1] \
        == [("light_command", 2, {"state": True, "brightness": 0.5})], "50 % -> 0.5 fraction"
    kw = run_cmd(em, "set_color_temp", {"value": 4000})[1][0][2]
    assert kw["color_temperature"] == 250.0 and kw["state"] is True, "4000 K -> 250 mireds"
    assert run_cmd(em, "set_color_temp", {"value": 0})[1][0][2]["color_temperature"] == 0, \
        "0 K -> 0 mireds (guard against divide-by-zero)"
    kw = run_cmd(em, "set_color", {"value": "#FF0000"})[1][0][2]
    assert kw["rgb"] == (1.0, 0.0, 0.0) and kw["color_brightness"] == 1.0, "red + full colour brightness"
    assert run_cmd(em, "set_effect", {"value": "Rainbow"})[1] \
        == [("light_command", 2, {"state": True, "effect": "Rainbow"})]
    ok, calls = run_cmd(em, "bogus")
    assert ok is False and calls == []


def test_send_command_cover():
    em = EntityMap(3, "blind", "cover")
    assert run_cmd(em, "open")[1] == [("cover_command", 3, {"position": 1.0})]
    assert run_cmd(em, "close")[1] == [("cover_command", 3, {"position": 0.0})]
    assert run_cmd(em, "stop")[1] == [("cover_command", 3, {"stop": True})]
    assert run_cmd(em, "set_position", {"value": 30})[1] == [("cover_command", 3, {"position": 0.3})]
    assert run_cmd(em, "toggle")[0] is False, "cover has no toggle"


def test_send_command_lock():
    em = EntityMap(4, "door", "lock")
    assert run_cmd(em, "lock")[1] == [("lock_command", 4, {"cmd": api.LockCommand.LOCK})]
    assert run_cmd(em, "unlock")[1] == [("lock_command", 4, {"cmd": api.LockCommand.UNLOCK})]
    assert run_cmd(em, "open")[0] is False, "lock has no open command"


def test_send_command_number_select_button():
    num = EntityMap(5, "sp", "number")
    assert run_cmd(num, "set_value", {"value": 21.5})[1] == [("number_command", 5, {"value": 21.5})]
    assert run_cmd(num, "nope")[0] is False

    sel = EntityMap(6, "preset", "select")
    assert run_cmd(sel, "set_option", {"value": "Eco"})[1] == [("select_command", 6, {"value": "Eco"})]
    assert run_cmd(sel, "nope")[0] is False

    btn = EntityMap(7, "restart", "button")
    assert run_cmd(btn, "press")[1] == [("button_command", 7, {})]
    assert run_cmd(btn, "nope")[0] is False


def test_send_command_fan():
    em = EntityMap(8, "fan", "fan", caps=["on_off", "fan_speed"], fan_speed_count=4)
    assert run_cmd(em, "turn_on")[1] == [("fan_command", 8, {"state": True})]
    assert run_cmd(em, "turn_off")[1] == [("fan_command", 8, {"state": False})]
    assert run_cmd(em, "toggle", current_on=True)[1] == [("fan_command", 8, {"state": False})]
    assert run_cmd(em, "set_fan_speed", {"value": 50})[1] \
        == [("fan_command", 8, {"state": True, "speed_level": 2})], "50 % of 4 speeds -> level 2"
    assert run_cmd(em, "set_fan_speed", {"value": 0})[1] == [("fan_command", 8, {"state": False})], \
        "0 % -> turn the fan off"
    assert run_cmd(em, "nope")[0] is False


def test_send_command_climate():
    em = EntityMap(9, "ac", "climate")
    assert run_cmd(em, "set_hvac_mode", {"value": "cool"})[1] \
        == [("climate_command", 9, {"mode": api.ClimateMode.COOL})]
    ok, calls = run_cmd(em, "set_hvac_mode", {"value": "bogus"})
    assert ok is False and calls == [], "unknown mode -> rejected, no cloud call"
    assert run_cmd(em, "set_temperature", {"value": 22.5})[1] \
        == [("climate_command", 9, {"target_temperature": 22.5})]
    assert run_cmd(em, "set_fan_mode", {"value": "low"})[1] \
        == [("climate_command", 9, {"fan_mode": api.ClimateFanMode.LOW})]
    assert run_cmd(em, "set_fan_mode", {"value": "bogus"})[0] is False
    assert run_cmd(em, "nope")[0] is False


def test_send_command_unknown_type():
    # read-only types (sensor / binary_sensor / text) have no command path
    assert run_cmd(EntityMap(10, "x", "sensor"), "turn_on")[0] is False


def test_send_command_awaitable_client():
    # aioesphomeapi's async client form: command methods return coroutines, which
    # _maybe_await runs supervised via home_core.tasks.spawn (not a bare create_task).
    calls = []

    class AsyncClient:
        async def switch_command(self, key, state):
            calls.append((key, state))

    async def drive():
        ok = await send_command(AsyncClient(), EntityMap(1, "relay", "switch"),
                                "turn_on", {}, current_on=None)
        await asyncio.sleep(0)  # let the supervised spawn() task run to completion
        return ok

    assert asyncio.run(drive()) is True and calls == [(1, True)], "awaitable command dispatched"
