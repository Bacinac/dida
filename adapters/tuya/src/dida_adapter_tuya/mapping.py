"""Tuya data-point (DPS) <-> canonical capability mapping.

A Tuya device exposes numeric "data points" (DPS) whose meaning is NOT
self-describing on the local protocol — you get `{"1": true, "5": "Level 2"}`
with no semantics. So each device carries an explicit `dps` map (dp id ->
capability) in the adapter's device file; this module only normalises the
VALUE for a known capability (Tuya scales -> canonical units).

This is the ONE place Tuya value quirks live. The engine stays protocol-blind.
"""

from __future__ import annotations

import colorsys

from dida_core import CAPABILITIES, CapabilityKind

Value = bool | int | float | str
NUMERIC_CAPS = {"temperature", "humidity", "illuminance", "power", "energy", "voltage", "current", "battery"}

# Tuya light scales: bright/temp DPS run 0..1000.
_TUYA_MAX = 1000
_K_MIN, _K_MAX = 2700, 6500  # warm..cool ends Tuya maps its 0..1000 temp onto


def _colour_to_hex(raw: object) -> str | None:
    """Tuya `colour_data` HSV hex (HHHHSSSSVVVV: H 0..360, S/V 0..1000) -> '#RRGGBB'.
    This is the format the music/RGB strips use (colour + brightness in one DP)."""
    s = str(raw)
    if len(s) < 12:
        return None
    try:
        h = (int(s[0:4], 16) % 361) / 360.0
        sat = min(1.0, int(s[4:8], 16) / _TUYA_MAX)
        val = min(1.0, int(s[8:12], 16) / _TUYA_MAX)
    except ValueError:
        return None
    r, g, b = colorsys.hsv_to_rgb(h, sat, val)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _hex_to_colour(hexstr: str) -> str:
    """'#RRGGBB' -> Tuya `colour_data` HSV hex (HHHHSSSSVVVV)."""
    hx = hexstr.lstrip("#")
    r, g, b = (int(hx[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    return f"{round(h * 360):04x}{round(sat * _TUYA_MAX):04x}{round(val * _TUYA_MAX):04x}"


def unit_for(capability: str) -> str | None:
    try:
        return CAPABILITIES[CapabilityKind(capability)].unit
    except (ValueError, KeyError):
        return None


def _num(v: object) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


def decode(capability: str, raw: object, *, scale: int = 0, unit: str | None = None) -> Value | None:
    """Tuya DPS value -> canonical capability value (None if unmappable)."""
    if capability == CapabilityKind.ON_OFF.value:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "on", "1")
    if capability == CapabilityKind.BRIGHTNESS.value:
        v = _num(raw)
        return None if v is None else max(0, min(100, round(v / _TUYA_MAX * 100)))
    if capability == CapabilityKind.COLOR_TEMP.value:
        v = _num(raw)
        if v is None:
            return None
        return max(1700, min(6535, round(_K_MIN + v / _TUYA_MAX * (_K_MAX - _K_MIN))))
    if capability == CapabilityKind.COLOR_RGB.value:
        return _colour_to_hex(raw)
    if capability == CapabilityKind.OPEN_CLOSE.value:
        v = _num(raw)
        return None if v is None else max(0, min(100, round(v)))
    if capability in NUMERIC_CAPS:
        value = _num(raw)
        if value is None:
            return None
        if type(scale) is not int or not 0 <= scale <= 12:
            raise ValueError("numeric DP scale must be an integer between 0 and 12")
        value /= 10 ** scale
        normalized = (unit or "").strip().lower().replace("℃", "°c").replace("℉", "°f")
        if capability == "temperature" and normalized in ("f", "°f"):
            return (value - 32) * 5 / 9
        aliases = {"c": "°c", "percent": "%"}
        normalized = aliases.get(normalized, normalized)
        if normalized and normalized != (unit_for(capability) or "").lower():
            factors = {("power", "kw"): 1000, ("energy", "wh"): 0.001,
                       ("voltage", "mv"): 0.001, ("current", "ma"): 0.001,
                       ("illuminance", "klx"): 1000}
            factor = factors.get((capability, normalized))
            if factor is None:
                raise ValueError(f"unsupported unit {unit!r} for {capability}")
            value *= factor
        return value
    return None


def encode(capability: str, command: str, args: dict) -> Value:
    """Canonical command -> Tuya DPS value to write (on_off is pre-resolved to
    turn_on/turn_off by the adapter, which holds last state for toggle)."""
    if capability == CapabilityKind.ON_OFF.value:
        return command == "turn_on"
    if capability == CapabilityKind.BRIGHTNESS.value:
        return max(10, min(_TUYA_MAX, round(int(args["value"]) / 100 * _TUYA_MAX)))
    if capability == CapabilityKind.COLOR_TEMP.value:
        k = int(args["value"])
        return max(0, min(_TUYA_MAX, round((k - _K_MIN) / (_K_MAX - _K_MIN) * _TUYA_MAX)))
    if capability == CapabilityKind.COLOR_RGB.value:
        return _hex_to_colour(str(args["value"]))
    if capability == CapabilityKind.OPEN_CLOSE.value:
        if command == "set_position":
            return int(args["value"])
        return {"open": "open", "close": "close", "stop": "stop"}[command]
    raise ValueError(f"no tuya mapping for {capability}/{command}")
