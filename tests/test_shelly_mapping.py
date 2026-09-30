"""Regression tests for the Shelly Gen2+ RPC <-> canonical mapping.

Run inside the shelly adapter image (dida_adapter_shelly installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-shelly:latest \
      -c "python -m pytest tests/test_shelly_mapping.py"

Covers component status -> readings (output/brightness/cover + power metering, the
Wh->kWh energy conversion, the diagnostic flags), partial NotifyStatus deltas, and
command -> RPC (Switch/Light/Cover methods).
"""
from dida_adapter_shelly.mapping import command_to_rpc, component_updates


def as_map(readings):
    # (cap, value, unit, diagnostic) -> {cap: (value, unit, diagnostic)}
    return {c: (v, u, d) for c, v, u, d in readings}


# --- component_updates: switch/light/cover + metering ----------------------
def test_component_updates():
    m = as_map(component_updates("switch", {"output": True, "apower": 42.5}))
    assert m.get("on_off") == (True, None, False), "switch output:true -> on_off True"
    assert m.get("power") == (42.5, "W", False), "switch apower -> power (W, not diagnostic)"

    m = as_map(component_updates("light", {"output": True, "brightness": 75.4}))
    assert m.get("brightness") == (75, "%", False), "light brightness 75.4 -> 75 % (rounded)"

    m = as_map(component_updates("cover", {"current_pos": 50.0}))
    assert m.get("open_close") == (50, "%", False), "cover current_pos 50 -> open_close 50 %"

    # Wh -> kWh energy conversion
    m = as_map(component_updates("switch", {"output": False, "aenergy": {"total": 1500.0}}))
    assert m.get("energy") == (1.5, "kWh", False), "aenergy 1500 Wh -> 1.5 kWh"
    assert m.get("on_off") == (False, None, False), "switch output:false -> on_off False"

    # voltage/current/freq/pf are all diagnostic (name-agnostic: count the diag flags)
    readings = component_updates("switch", {"voltage": 230.0, "current": 0.18, "freq": 50.0, "pf": 0.98})
    diag = [c for c, _v, _u, d in readings if d]
    assert len(diag) == 4, "voltage/current/freq/power-factor all flagged diagnostic"
    assert all(d for _c, _v, _u, d in readings), "no primary tile among the metering diagnostics"

    # internal device temperature (a dict {tC}) is diagnostic, not a room reading
    m = as_map(component_updates("switch", {"temperature": {"tC": 48.0}}))
    assert m.get("temperature") == (48.0, "°C", True), "internal temp -> diagnostic"


# --- sensor components ------------------------------------------------------
def test_sensor_components():
    assert as_map(component_updates("temperature", {"tC": 21.5}))["temperature"][0] == 21.5, \
        "temperature component tC -> temperature"
    assert as_map(component_updates("humidity", {"rh": 55.0}))["humidity"][0] == 55.0, \
        "humidity component rh -> humidity"
    assert as_map(component_updates("illuminance", {"lux": 300.0}))["illuminance"][0] == 300.0, \
        "illuminance component lux -> illuminance"
    assert as_map(component_updates("devicepower", {"battery": {"percent": 80.0}}))["battery"][0] == 80.0, \
        "devicepower battery.percent -> battery"
    assert as_map(component_updates("wifi", {"rssi": -60.0}))["signal"][2] is True, \
        "wifi rssi -> signal (diagnostic)"


# --- partial payloads (NotifyStatus deltas) — only present fields emit ------
def test_partial_payloads():
    m = as_map(component_updates("switch", {"output": True}))
    assert list(m.keys()) == ["on_off"], "a partial delta emits ONLY the present field"
    assert component_updates("switch", {"unknownfield": 1}) == [], "unknown fields are ignored"


# --- command_to_rpc: canonical -> Shelly RPC --------------------------------
def test_command_to_rpc():
    assert command_to_rpc("switch", 0, "on_off", "turn_on", {}) == ("Switch.Set", {"id": 0, "on": True}), \
        "turn_on -> Switch.Set on=true"
    assert command_to_rpc("switch", 1, "on_off", "turn_off", {}) == ("Switch.Set", {"id": 1, "on": False}), \
        "turn_off -> Switch.Set on=false"
    assert command_to_rpc("switch", 2, "on_off", "toggle", {}) == ("Switch.Toggle", {"id": 2}), \
        "toggle -> Switch.Toggle"
    assert command_to_rpc("light", 0, "brightness", "set_brightness", {"value": 50}) == \
        ("Light.Set", {"id": 0, "brightness": 50}), "brightness -> Light.Set"
    assert command_to_rpc("cover", 0, "open_close", "set_position", {"value": 30}) == \
        ("Cover.GoToPosition", {"id": 0, "pos": 30}), "set_position -> Cover.GoToPosition"
    assert command_to_rpc("cover", 0, "open_close", "open", {}) == ("Cover.Open", {"id": 0}), \
        "open -> Cover.Open"
