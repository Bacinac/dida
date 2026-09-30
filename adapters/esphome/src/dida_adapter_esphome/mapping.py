"""ESPHome native-API entity/state <-> canonical DIDA capability mapping.

ESPHome exposes a device's entities over its native API (port 6053). Each entity
already represents one function (a switch, a sensor, a light…), so we map one
ESPHome entity to one DIDA entity (`esphome:<device>:<object_id>`) carrying that
entity's capability(ies). The engine never sees ESPHome; this is the one place
its conventions live.

Pure mapping/no I/O. Unknown entity types/device-classes are skipped, not guessed.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field

import aioesphomeapi as api
from dida_core import CapabilityKind

Value = bool | int | float | str


def _parse_hex(s: str) -> tuple[float, float, float]:
    """"#RRGGBB" (or "RRGGBB") -> (r, g, b) floats in 0..1 for the ESPHome API."""
    h = s.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad colour {s!r}")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]

NAMESPACE = "esphome"

# ESPHome device_class strings reuse HA's vocabulary.
_SENSOR_CLASS = {
    "temperature": CapabilityKind.TEMPERATURE,
    "humidity": CapabilityKind.HUMIDITY,
    "illuminance": CapabilityKind.ILLUMINANCE,
    "power": CapabilityKind.POWER,
    "energy": CapabilityKind.ENERGY,
    "voltage": CapabilityKind.VOLTAGE,
    "current": CapabilityKind.CURRENT,
    "frequency": CapabilityKind.FREQUENCY,
    "power_factor": CapabilityKind.POWER_FACTOR,
    "battery": CapabilityKind.BATTERY,
    "signal_strength": CapabilityKind.SIGNAL,
    "duration": CapabilityKind.DURATION,
}
# Fallback by unit when device_class is missing/unknown (ESPHome often omits it
# for plain sensors). Only unambiguous units — never "%" (humidity/battery/…).
_SENSOR_UNIT = {
    "V": CapabilityKind.VOLTAGE,
    "A": CapabilityKind.CURRENT,
    "Hz": CapabilityKind.FREQUENCY,
    "dBm": CapabilityKind.SIGNAL,
    "W": CapabilityKind.POWER,
    "kWh": CapabilityKind.ENERGY,
    "lx": CapabilityKind.ILLUMINANCE,
    "°C": CapabilityKind.TEMPERATURE,
}
_BINARY_CLASS = {
    "motion": CapabilityKind.MOTION,
    "moving": CapabilityKind.MOTION,
    "occupancy": CapabilityKind.OCCUPANCY,
    "presence": CapabilityKind.OCCUPANCY,
    "door": CapabilityKind.CONTACT,
    "garage_door": CapabilityKind.CONTACT,
    "window": CapabilityKind.CONTACT,
    "opening": CapabilityKind.CONTACT,
}


@dataclass(slots=True)
class EntityMap:
    """What one ESPHome entity (identified by its API `key`) maps to in DIDA."""

    key: int
    object_id: str
    etype: str                 # switch|light|sensor|binary_sensor|cover|lock|number|select|climate|fan|button
    caps: list[str] = field(default_factory=list)
    unit: str | None = None
    has_brightness: bool = False
    has_color_temp: bool = False
    has_rgb: bool = False
    effects: list[str] = field(default_factory=list)
    supports_position: bool = False
    options: list[str] = field(default_factory=list)   # select options
    number_meta: dict | None = None                    # {min,max,step,unit,mode} for a number
    climate_modes: list[str] = field(default_factory=list)      # supported hvac modes
    climate_fan_modes: list[str] = field(default_factory=list)  # supported fan modes
    has_current_temp: bool = False                     # climate reports current temperature
    fan_speed_count: int = 0                           # fan speed levels (0 = no speed control)
    name: str = ""             # human-friendly entity name for the UI
    diagnostic: bool = False   # ESPHome entity_category == DIAGNOSTIC


def _map_sensor(info, key, oid) -> EntityMap:
    kind = _SENSOR_CLASS.get(info.device_class) or _SENSOR_UNIT.get(
        (info.unit_of_measurement or "").strip()
    ) or CapabilityKind.MEASUREMENT  # generic numeric fallback — never drop a readable sensor
    return EntityMap(key, oid, "sensor", [kind.value], info.unit_of_measurement or None)


def _map_binary_sensor(info, key, oid) -> EntityMap:
    kind = _BINARY_CLASS.get(info.device_class) or CapabilityKind.BINARY  # generic bool fallback
    return EntityMap(key, oid, "binary_sensor", [kind.value])


def _map_light(info, key, oid) -> EntityMap:
    modes = set(getattr(info, "supported_color_modes", []) or [])
    # supported_color_modes carries LightColorCapability bit flags:
    # BRIGHTNESS=2, COLOR_TEMPERATURE=8, RGB=32 (a ColorMode like RGB=35 is
    # ON_OFF|BRIGHTNESS|RGB). legacy_supports_* aren't reliable on modern
    # firmware (an RGB strip reports legacy_supports_rgb=False), so prefer bits.
    has_b = bool(info.legacy_supports_brightness) or any(int(m) & 2 for m in modes)
    has_ct = bool(info.legacy_supports_color_temperature) or any(int(m) & 8 for m in modes)
    has_rgb = bool(getattr(info, "legacy_supports_rgb", False)) or any(int(m) & 32 for m in modes)
    effects = [e for e in (getattr(info, "effects", None) or []) if e and e != "None"]
    caps = [CapabilityKind.ON_OFF.value] + [
        cap.value for cap, present in (
            (CapabilityKind.BRIGHTNESS, has_b), (CapabilityKind.COLOR_TEMP, has_ct),
            (CapabilityKind.COLOR_RGB, has_rgb), (CapabilityKind.EFFECT, bool(effects)),
        ) if present
    ]
    return EntityMap(key, oid, "light", caps, "%" if has_b else None, has_b, has_ct,
                     has_rgb=has_rgb, effects=effects)


def _map_number(info, key, oid) -> EntityMap:
    return EntityMap(
        key, oid, "number",
        [CapabilityKind.NUMBER.value, CapabilityKind.NUMBER_OPTIONS.value],
        unit=info.unit_of_measurement or None,
        number_meta={
            "min": info.min_value, "max": info.max_value, "step": info.step,
            "unit": info.unit_of_measurement or None,
            "mode": _enum_name(getattr(info, "mode", None)),
        },
    )


def _map_fan(info, key, oid) -> EntityMap:
    caps = [CapabilityKind.ON_OFF.value]
    count = int(getattr(info, "supported_speed_count", 0) or 0)
    if getattr(info, "supports_speed", False):
        caps.append(CapabilityKind.FAN_SPEED.value)
    return EntityMap(key, oid, "fan", caps, fan_speed_count=count)


def _map_climate(info, key, oid) -> EntityMap:
    modes = [_enum_name(m) for m in (info.supported_modes or [])]
    fan_modes = [_enum_name(m) for m in (getattr(info, "supported_fan_modes", None) or [])]
    caps = [
        CapabilityKind.HVAC_MODE.value, CapabilityKind.HVAC_MODE_OPTIONS.value,
        CapabilityKind.TARGET_TEMPERATURE.value,
    ]
    if getattr(info, "supports_current_temperature", False):
        caps.append(CapabilityKind.TEMPERATURE.value)
    if fan_modes:
        caps += [CapabilityKind.FAN_MODE.value, CapabilityKind.FAN_MODE_OPTIONS.value]
    return EntityMap(
        key, oid, "climate", caps,
        climate_modes=[m for m in modes if m],
        climate_fan_modes=[m for m in fan_modes if m],
        has_current_temp=bool(getattr(info, "supports_current_temperature", False)),
    )


_MAPPERS = {
    "SwitchInfo": lambda info, key, oid: EntityMap(key, oid, "switch", [CapabilityKind.ON_OFF.value]),
    "SensorInfo": _map_sensor,
    "TextSensorInfo": lambda info, key, oid: EntityMap(key, oid, "text", [CapabilityKind.TEXT.value]),
    "BinarySensorInfo": _map_binary_sensor,
    "LightInfo": _map_light,
    "CoverInfo": lambda info, key, oid: EntityMap(key, oid, "cover", [CapabilityKind.OPEN_CLOSE.value], "%",
                                                  supports_position=bool(info.supports_position)),
    "LockInfo": lambda info, key, oid: EntityMap(key, oid, "lock", [CapabilityKind.LOCK.value]),
    "NumberInfo": _map_number,
    "SelectInfo": lambda info, key, oid: EntityMap(
        key, oid, "select", [CapabilityKind.ENUM.value, CapabilityKind.ENUM_OPTIONS.value],
        options=list(info.options or [])),
    "ButtonInfo": lambda info, key, oid: EntityMap(key, oid, "button", [CapabilityKind.PRESS.value]),
    "FanInfo": _map_fan,
    "ClimateInfo": _map_climate,
}


def map_entity(info) -> EntityMap | None:
    """EntityInfo -> EntityMap, or None for unsupported types/classes."""
    mapper = _MAPPERS.get(type(info).__name__)
    return mapper(info, info.key, info.object_id) if mapper else None


def _enum_name(v) -> str:
    """aioesphomeapi enum (ClimateMode/ClimateFanMode/NumberMode) or int -> lower
    snake name ('cool', 'fan_only', …). Robust to enum, int, or string input."""
    name = getattr(v, "name", None)
    if name:
        return str(name).lower()
    return str(v).lower() if v is not None else ""


def is_diagnostic(info) -> bool:
    """True when the entity should be lean-hidden by default: ESPHome marks it
    DIAGNOSTIC (entity_category == 2), or it is a cumulative energy counter. A
    home dashboard shows live power (W/V/A), not lifetime kWh, so energy totals
    are hidden by default; the user can re-expose any of them per field in
    Settings → Adapters."""
    cat = getattr(info, "entity_category", None)
    try:
        if int(cat) == 2:  # EntityCategory.DIAGNOSTIC
            return True
    except (TypeError, ValueError):
        if "DIAGNOSTIC" in str(cat).upper():
            return True
    dc = str(getattr(info, "device_class", "") or "").lower()
    unit = str(getattr(info, "unit_of_measurement", "") or "")
    return dc == "energy" or unit == "kWh"


Decoded = list[tuple[str, Value, str | None]]


def _finite(raw) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf → no reading
        return None
    return v


def _decode_sensor(em: EntityMap, state) -> Decoded:
    v = _finite(state.state)
    return [] if v is None else [(em.caps[0], v, em.unit)]


def _decode_light(em: EntityMap, state) -> Decoded:
    out: Decoded = [(CapabilityKind.ON_OFF.value, bool(state.state), None)]
    if em.has_brightness:
        out.append((CapabilityKind.BRIGHTNESS.value, max(0, min(100, round(state.brightness * 100))), "%"))
    if em.has_color_temp and state.color_temperature and state.color_temperature > 0:
        out.append((CapabilityKind.COLOR_TEMP.value,
                    max(1700, min(6535, round(1_000_000 / state.color_temperature))), "K"))
    if em.has_rgb:
        r, g, b = (int(max(0, min(255, round(c * 255))) ) for c in
                   (state.red, state.green, state.blue))
        out.append((CapabilityKind.COLOR_RGB.value, f"#{r:02X}{g:02X}{b:02X}", None))
    if em.effects:
        out.append((CapabilityKind.EFFECT.value, str(getattr(state, "effect", "") or ""), None))
        out.append((CapabilityKind.EFFECT_OPTIONS.value, json.dumps(em.effects), None))
    return out


def _decode_cover(em: EntityMap, state) -> Decoded:
    if not em.supports_position:
        return []
    return [(CapabilityKind.OPEN_CLOSE.value, max(0, min(100, round(state.position * 100))), "%")]


def _decode_number(em: EntityMap, state) -> Decoded:
    v = _finite(state.state)
    if v is None:
        return []
    out: Decoded = [(CapabilityKind.NUMBER.value, v, em.unit)]
    if em.number_meta is not None:
        out.append((CapabilityKind.NUMBER_OPTIONS.value, json.dumps(em.number_meta), None))
    return out


def _decode_select(em: EntityMap, state) -> Decoded:
    if state.state is None:
        return []
    return [(CapabilityKind.ENUM.value, str(state.state), None),
            (CapabilityKind.ENUM_OPTIONS.value, json.dumps(em.options), None)]


def _decode_fan(em: EntityMap, state) -> Decoded:
    out: Decoded = [(CapabilityKind.ON_OFF.value, bool(state.state), None)]
    if em.fan_speed_count and CapabilityKind.FAN_SPEED.value in em.caps:
        lvl = int(getattr(state, "speed_level", 0) or 0)
        out.append((CapabilityKind.FAN_SPEED.value,
                    max(0, min(100, round(lvl / em.fan_speed_count * 100))), "%"))
    return out


def _decode_climate(em: EntityMap, state) -> Decoded:
    # Enum decode is guarded: firmware newer than the bundled aioesphomeapi can
    # report enum values our ClimateMode/ClimateFanMode don't know — that must
    # skip the one field, not raise out of the sync state callback.
    out: Decoded = []
    with contextlib.suppress(ValueError):
        out.append((CapabilityKind.HVAC_MODE.value, _enum_name(api.ClimateMode(int(state.mode))), None))
    out.append((CapabilityKind.HVAC_MODE_OPTIONS.value, json.dumps(em.climate_modes), None))
    tt = getattr(state, "target_temperature", None)
    if tt is not None and tt == tt:  # not NaN
        out.append((CapabilityKind.TARGET_TEMPERATURE.value, float(tt), "°C"))
    ct = getattr(state, "current_temperature", None) if em.has_current_temp else None
    if ct is not None and ct == ct:
        out.append((CapabilityKind.TEMPERATURE.value, float(ct), "°C"))
    if em.climate_fan_modes:
        with contextlib.suppress(ValueError):
            out.append((CapabilityKind.FAN_MODE.value,
                        _enum_name(api.ClimateFanMode(int(state.fan_mode))), None))
        out.append((CapabilityKind.FAN_MODE_OPTIONS.value, json.dumps(em.climate_fan_modes), None))
    return out


# button: write-only (press) — no readable state
_DECODERS = {
    "switch": lambda em, state: [(CapabilityKind.ON_OFF.value, bool(state.state), None)],
    "binary_sensor": lambda em, state: [(em.caps[0], bool(state.state), None)],
    "sensor": _decode_sensor,
    "text": lambda em, state: [] if state.state is None else [(CapabilityKind.TEXT.value, str(state.state), None)],
    "light": _decode_light,
    "cover": _decode_cover,
    "lock": lambda em, state: [(CapabilityKind.LOCK.value, int(state.state) == int(api.LockState.LOCKED), None)],
    "number": _decode_number,
    "select": _decode_select,
    "fan": _decode_fan,
    "climate": _decode_climate,
}


def decode_state(em: EntityMap, state) -> Decoded:
    """ESPHome state object -> list of (capability, value, unit). Empty to skip."""
    if getattr(state, "missing_state", False):
        return []
    decode = _DECODERS.get(em.etype)
    return decode(em, state) if decode else []


def _toggled(command: str, current_on: bool | None) -> str:
    if command == "toggle":
        return "turn_off" if current_on else "turn_on"
    return command


def _send_switch(client, em: EntityMap, command: str, args: dict) -> bool:
    if command not in ("turn_on", "turn_off"):
        return False
    _maybe_await(client.switch_command(em.key, command == "turn_on"))
    return True


def _light_kwargs(command: str, args: dict) -> dict | None:
    if command in ("turn_on", "turn_off"):
        return {"state": command == "turn_on"}
    if command == "set_brightness":
        return {"state": True, "brightness": int(args["value"]) / 100}
    if command == "set_color_temp":
        kelvin = int(args["value"])
        return {"state": True, "color_temperature": 1_000_000 / kelvin if kelvin > 0 else 0}
    if command == "set_color":
        # ColorMode RGB needs color_brightness=1.0 alongside rgb, else the
        # colour channel is scaled to 0 and the light shows nothing.
        return {"state": True, "rgb": _parse_hex(str(args["value"])), "color_brightness": 1.0}
    if command == "set_effect":
        return {"state": True, "effect": str(args["value"])}
    return None


def _send_light(client, em: EntityMap, command: str, args: dict) -> bool:
    kwargs = _light_kwargs(command, args)
    if kwargs is None:
        return False
    _maybe_await(client.light_command(em.key, **kwargs))
    return True


def _send_cover(client, em: EntityMap, command: str, args: dict) -> bool:
    if command == "set_position":
        kwargs = {"position": int(args["value"]) / 100}
    else:
        kwargs = {"open": {"position": 1.0}, "close": {"position": 0.0}, "stop": {"stop": True}}.get(command)
    if kwargs is None:
        return False
    _maybe_await(client.cover_command(em.key, **kwargs))
    return True


def _send_lock(client, em: EntityMap, command: str, args: dict) -> bool:
    action = {"lock": api.LockCommand.LOCK, "unlock": api.LockCommand.UNLOCK}.get(command)
    if action is None:
        return False
    _maybe_await(client.lock_command(em.key, action))
    return True


def _send_number(client, em: EntityMap, command: str, args: dict) -> bool:
    if command != "set_value":
        return False
    _maybe_await(client.number_command(em.key, float(args["value"])))
    return True


def _send_select(client, em: EntityMap, command: str, args: dict) -> bool:
    if command != "set_option":
        return False
    _maybe_await(client.select_command(em.key, str(args["value"])))
    return True


def _send_button(client, em: EntityMap, command: str, args: dict) -> bool:
    if command != "press":
        return False
    _maybe_await(client.button_command(em.key))
    return True


def _fan_kwargs(em: EntityMap, command: str, args: dict) -> dict | None:
    if command in ("turn_on", "turn_off"):
        return {"state": command == "turn_on"}
    if command != "set_fan_speed":
        return None
    pct = int(args["value"])
    if pct <= 0:
        return {"state": False}
    level = max(1, round(pct / 100 * em.fan_speed_count)) if em.fan_speed_count else 1
    return {"state": True, "speed_level": level}


def _send_fan(client, em: EntityMap, command: str, args: dict) -> bool:
    kwargs = _fan_kwargs(em, command, args)
    if kwargs is None:
        return False
    _maybe_await(client.fan_command(em.key, **kwargs))
    return True


def _climate_kwargs(command: str, args: dict) -> dict | None:
    if command == "set_temperature":
        return {"target_temperature": float(args["value"])}
    if command == "set_hvac_mode":
        mode = getattr(api.ClimateMode, str(args["value"]).upper(), None)
        return None if mode is None else {"mode": mode}
    if command == "set_fan_mode":
        fan_mode = getattr(api.ClimateFanMode, str(args["value"]).upper(), None)
        return None if fan_mode is None else {"fan_mode": fan_mode}
    return None


def _send_climate(client, em: EntityMap, command: str, args: dict) -> bool:
    kwargs = _climate_kwargs(command, args)
    if kwargs is None:
        return False
    _maybe_await(client.climate_command(em.key, **kwargs))
    return True


_SENDERS = {
    "switch": _send_switch, "light": _send_light, "cover": _send_cover, "lock": _send_lock,
    "number": _send_number, "select": _send_select, "button": _send_button,
    "fan": _send_fan, "climate": _send_climate,
}
_TOGGLES = {"switch", "light", "fan"}


async def send_command(client, em: EntityMap, command: str, args: dict, *, current_on: bool | None) -> bool:
    """Translate a DIDA command into an ESPHome native-API call. Returns False
    if not expressible. `current_on` is the cached on_off state (for toggle)."""
    send = _SENDERS.get(em.etype)
    if send is None:
        return False
    if em.etype in _TOGGLES:
        command = _toggled(command, current_on)
    return send(client, em, command, args)


def _maybe_await(result):
    # aioesphomeapi command methods send synchronously; tolerate either form.
    # When awaitable, run it SUPERVISED (home_core.tasks.spawn): a bare create_task made
    # command-send failures vanish — handle_command's try/except never saw them
    # and send_command returned True for sends that never happened.
    import inspect
    import logging

    from home_core.tasks import spawn

    if inspect.isawaitable(result):
        spawn(result, log=logging.getLogger("dida.adapter.esphome"), name="esphome command send")
