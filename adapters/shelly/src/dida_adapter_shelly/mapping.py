"""Shelly Gen2+ RPC component <-> canonical capability mapping.

A Shelly Gen2+ device exposes a flat status object keyed by `<type>:<id>`
("switch:0", "cover:0", "light:0", "temperature:0", "humidity:0",
"devicepower:0", ...) plus device-level blocks ("wifi", "sys"). We translate
the fields we understand into canonical capability values, normalising units
(Wh -> kWh, etc.). Unknown fields are ignored; they never reach the engine.

This is the ONE place the Shelly status shape lives. The engine stays
protocol-blind; the adapter speaks the device's own documented RPC.
"""

from __future__ import annotations

from dida_core import CAPABILITIES, CapabilityKind

Value = bool | int | float | str

# (capability, value, unit, diagnostic)
Reading = tuple[str, Value, str | None, bool]


def _unit(kind: CapabilityKind) -> str | None:
    return CAPABILITIES[kind].unit


def _num(v: object) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


def _emit(out: list[Reading], kind: CapabilityKind, value: Value, diagnostic: bool = False) -> None:
    out.append((kind.value, value, _unit(kind), diagnostic))


def _output_kind(ctype: str, st: dict) -> list[Reading]:
    """switch / light / cover share output + power-metering fields."""
    out: list[Reading] = []
    if isinstance(st.get("output"), bool):
        _emit(out, CapabilityKind.ON_OFF, st["output"])
    if ctype == "light" and _num(st.get("brightness")) is not None:
        _emit(out, CapabilityKind.BRIGHTNESS, max(0, min(100, round(float(st["brightness"])))))
    if ctype == "cover" and _num(st.get("current_pos")) is not None:
        _emit(out, CapabilityKind.OPEN_CLOSE, max(0, min(100, round(float(st["current_pos"])))))
    # Power metering (PM variants); voltage/current/freq/pf are diagnostic.
    if _num(st.get("apower")) is not None:
        _emit(out, CapabilityKind.POWER, float(st["apower"]))
    if _num(st.get("voltage")) is not None:
        _emit(out, CapabilityKind.VOLTAGE, float(st["voltage"]), True)
    if _num(st.get("current")) is not None:
        _emit(out, CapabilityKind.CURRENT, float(st["current"]), True)
    if _num(st.get("freq")) is not None:
        _emit(out, CapabilityKind.FREQUENCY, float(st["freq"]), True)
    if _num(st.get("pf")) is not None:
        _emit(out, CapabilityKind.POWER_FACTOR, float(st["pf"]), True)
    ae = st.get("aenergy")
    if isinstance(ae, dict) and _num(ae.get("total")) is not None:
        _emit(out, CapabilityKind.ENERGY, float(ae["total"]) / 1000.0)  # Wh -> kWh
    t = st.get("temperature")
    if isinstance(t, dict) and _num(t.get("tC")) is not None:
        # Internal device temperature — diagnostic, not a room reading.
        _emit(out, CapabilityKind.TEMPERATURE, float(t["tC"]), True)
    return out


def component_updates(ctype: str, st: dict) -> list[Reading]:
    """Map a single Shelly status component to (cap, value, unit, diagnostic).

    Handles partial payloads (NotifyStatus deltas) — only fields present emit.
    """
    if ctype in ("switch", "light", "cover"):
        return _output_kind(ctype, st)
    out: list[Reading] = []
    if ctype == "temperature" and _num(st.get("tC")) is not None:
        _emit(out, CapabilityKind.TEMPERATURE, float(st["tC"]))
    elif ctype == "humidity" and _num(st.get("rh")) is not None:
        _emit(out, CapabilityKind.HUMIDITY, float(st["rh"]))
    elif ctype == "illuminance" and _num(st.get("lux")) is not None:
        _emit(out, CapabilityKind.ILLUMINANCE, float(st["lux"]))
    elif ctype == "devicepower":
        b = st.get("battery")
        if isinstance(b, dict) and _num(b.get("percent")) is not None:
            _emit(out, CapabilityKind.BATTERY, float(b["percent"]))
    elif ctype == "wifi" and _num(st.get("rssi")) is not None:
        _emit(out, CapabilityKind.SIGNAL, float(st["rssi"]), True)
    return out


def command_to_rpc(ctype: str, cid: int, capability: str, command: str, args: dict) -> tuple[str, dict]:
    """Map a validated capability command to a Shelly RPC (method, params)."""
    if capability == CapabilityKind.ON_OFF.value:
        comp = ctype.capitalize()  # Switch / Light
        if command == "toggle":
            return (f"{comp}.Toggle", {"id": cid})
        return (f"{comp}.Set", {"id": cid, "on": command == "turn_on"})
    if capability == CapabilityKind.BRIGHTNESS.value:
        return ("Light.Set", {"id": cid, "brightness": int(args["value"])})
    if capability == CapabilityKind.OPEN_CLOSE.value:
        if command == "set_position":
            return ("Cover.GoToPosition", {"id": cid, "pos": int(args["value"])})
        return ({"open": "Cover.Open", "close": "Cover.Close", "stop": "Cover.Stop"}[command], {"id": cid})
    raise ValueError(f"no shelly mapping for {capability}/{command}")
