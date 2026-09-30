"""SmartThings capability taxonomy → DIDA canonical capabilities.

SmartThings has its OWN large capability model (`switch`, `airConditionerMode`,
`washerOperatingState`, `samsungvd.mediaInputSource`, …). This module is the ONE
place that translation lives — mirroring how the tuya adapter maps raw DP codes.
We read the RAW device status (a plain nested dict) rather than pysmartthings'
typed enums, so Samsung-specific capabilities (`samsungce.*`, `samsungvd.*`,
`custom.*`) that the library's enums don't cover still onboard.

A SmartThings device has one or more *components* (`main`, `cooler`, `freezer`,
…); each component becomes one DIDA entity carrying several capabilities (a
climate device decomposes into scalars, exactly like DIDA's own model). Command
routing needs to know which ST capability a DIDA capability came from ON THIS
device, so `map_component` returns a per-capability ROUTE the adapter caches and
`translate_command` consumes.
"""

from __future__ import annotations

import colorsys
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ── canonical value maps ─────────────────────────────────────────────────────

# Samsung airConditionerMode → DIDA hvac_mode (closed set in the capability spec).
_AC_MODE: dict[str, str] = {
    "cool": "cool", "heat": "heat", "dry": "dry", "wind": "fan_only",
    "auto": "auto", "aIComfort": "auto", "sleep": "auto", "fanOnly": "fan_only",
}
# thermostatMode → DIDA hvac_mode.
_TH_MODE: dict[str, str] = {
    "off": "off", "cool": "cool", "heat": "heat", "auto": "auto",
    "emergency heat": "heat", "dryair": "dry", "dry": "dry",
    "fanonly": "fan_only", "fan only": "fan_only", "fanOnly": "fan_only",
}
# Samsung fan mode → DIDA fan_mode (closed set).
_FAN_MODE: dict[str, str] = {
    "auto": "auto", "low": "low", "medium": "medium", "high": "high",
    "turbo": "max", "windFree": "silent", "windfree": "silent",
    "quiet": "quiet", "silent": "silent", "max": "max", "middle": "middle",
}
# mediaPlayback.playbackStatus → DIDA media_transport (closed set).
_PLAYBACK: dict[str, str] = {
    "playing": "playing", "paused": "paused", "stopped": "stopped",
    "fast forwarding": "playing", "rewinding": "playing", "buffering": "buffering",
}


def _rev(mapping: dict[str, str], supported: list[str]) -> dict[str, str]:
    """DIDA value → the FIRST supported native value mapping to it (for commands)."""
    out: dict[str, str] = {}
    for native in supported:
        dida = mapping.get(native)
        if dida and dida not in out:
            out[dida] = native
    return out


def hs_to_hex(hue: float, sat: float) -> str:
    """ST hue/saturation (0..100) → #RRGGBB (full value, for display)."""
    r, g, b = colorsys.hsv_to_rgb((hue or 0) / 100.0, (sat or 0) / 100.0, 1.0)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def hex_to_hs(hexstr: str) -> dict[str, float]:
    """#RRGGBB → {"hue":0..100,"saturation":0..100} for colorControl.setColor."""
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
    return {"hue": round(hue * 100, 1), "saturation": round(sat * 100, 1)}


# ── mapping output ───────────────────────────────────────────────────────────

@dataclass(slots=True)
class Reading:
    cap: str
    value: object
    unit: str | None = None
    category: str = "control"      # control | config | diagnostic
    diagnostic: bool = False


@dataclass(slots=True)
class Route:
    """How to command a DIDA capability back onto a ST capability, for one entity."""

    capability: str                       # the ST capability that owns this facet
    setter: str = ""                      # the ST command for a set_<x> value command
    modes: dict[str, str] = field(default_factory=dict)  # DIDA value → native value
    has_level: bool = False               # open_close: windowShadeLevel present


def _val(comp: dict, stcap: str, attr: str):
    try:
        return comp[stcap][attr].get("value")
    except (KeyError, AttributeError, TypeError):
        return None


def _unit(comp: dict, stcap: str, attr: str) -> str | None:
    try:
        return comp[stcap][attr].get("unit")
    except (KeyError, AttributeError, TypeError):
        return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _seconds_until(iso: object) -> int | None:
    """Seconds from now until an ISO-8601 instant (UTC), or None. Clamped at 0 so a
    just-finished / stale completionTime reads 0, not a negative countdown."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, round((end - datetime.now(UTC)).total_seconds()))


def _to_c(v, unit) -> float | None:
    n = _num(v)
    if n is None:
        return None
    return round((n - 32) * 5 / 9, 1) if str(unit).upper() == "F" else n


def _emit_lighting(out: list[Reading], routes: dict[str, Route], comp: dict) -> None:
    has = comp.__contains__
    if has("switch"):
        out.append(Reading("on_off", _val(comp, "switch", "switch") == "on"))
        routes["on_off"] = Route("switch")
    if has("switchLevel"):
        lvl = _num(_val(comp, "switchLevel", "level"))
        if lvl is not None:
            out.append(Reading("brightness", int(lvl), "%"))
            routes["brightness"] = Route("switchLevel", "setLevel")
    if has("colorTemperature"):
        ct = _num(_val(comp, "colorTemperature", "colorTemperature"))
        if ct is not None and ct > 0:
            out.append(Reading("color_temp", int(ct), "K"))
            routes["color_temp"] = Route("colorTemperature", "setColorTemperature")
    if has("colorControl"):
        hue = _num(_val(comp, "colorControl", "hue"))
        sat = _num(_val(comp, "colorControl", "saturation"))
        if hue is not None and sat is not None:
            out.append(Reading("color_rgb", hs_to_hex(hue, sat)))
            routes["color_rgb"] = Route("colorControl", "setColor")


def _emit_media(out: list[Reading], routes: dict[str, Route], comp: dict) -> None:
    has = comp.__contains__
    if has("audioVolume"):
        vol = _num(_val(comp, "audioVolume", "volume"))
        if vol is not None:
            out.append(Reading("volume", int(vol), "%"))
            routes["volume"] = Route("audioVolume", "setVolume")
    if has("audioMute"):
        out.append(Reading("mute", _val(comp, "audioMute", "mute") == "muted"))
        routes["mute"] = Route("audioMute")
    if has("mediaPlayback"):
        st = str(_val(comp, "mediaPlayback", "playbackStatus") or "").strip()
        out.append(Reading("media_transport", _PLAYBACK.get(st, "idle")))
        routes["media_transport"] = Route("mediaPlayback")
    for src_cap in ("mediaInputSource", "samsungvd.mediaInputSource"):
        if has(src_cap):
            cur = _val(comp, src_cap, "inputSource")
            opts = _val(comp, src_cap, "supportedInputSources") or _val(comp, src_cap, "supportedInputSourcesMap")
            ids = _source_ids(opts)
            if cur is not None:
                out.append(Reading("source", str(cur)))
                routes["source"] = Route(src_cap, "setInputSource")
            if ids:
                out.append(Reading("source_options", json.dumps(ids), category="config"))
            break


def _emit_climate(out: list[Reading], routes: dict[str, Route], comp: dict) -> None:
    has = comp.__contains__
    if has("temperatureMeasurement"):
        t = _to_c(_val(comp, "temperatureMeasurement", "temperature"),
                  _unit(comp, "temperatureMeasurement", "temperature"))
        if t is not None:
            out.append(Reading("temperature", t, "°C"))
    if has("relativeHumidityMeasurement"):
        h = _num(_val(comp, "relativeHumidityMeasurement", "humidity"))
        if h is not None:
            out.append(Reading("humidity", h, "%"))
    if has("airConditionerMode"):
        cur = str(_val(comp, "airConditionerMode", "airConditionerMode") or "")
        supported = [str(x) for x in (_val(comp, "airConditionerMode", "supportedAcModes") or [])]
        _emit_mode(out, routes, "airConditionerMode", "setAirConditionerMode", _AC_MODE, cur, supported)
    elif has("thermostatMode"):
        cur = str(_val(comp, "thermostatMode", "thermostatMode") or "")
        supported = [str(x) for x in (_val(comp, "thermostatMode", "supportedThermostatModes") or [])]
        _emit_mode(out, routes, "thermostatMode", "setThermostatMode", _TH_MODE, cur, supported)
    if has("thermostatCoolingSetpoint"):
        sp = _to_c(_val(comp, "thermostatCoolingSetpoint", "coolingSetpoint"),
                   _unit(comp, "thermostatCoolingSetpoint", "coolingSetpoint"))
        if sp is not None:
            out.append(Reading("target_temperature", sp, "°C"))
            routes["target_temperature"] = Route("thermostatCoolingSetpoint", "setCoolingSetpoint")
    elif has("thermostatHeatingSetpoint"):
        sp = _to_c(_val(comp, "thermostatHeatingSetpoint", "heatingSetpoint"),
                   _unit(comp, "thermostatHeatingSetpoint", "heatingSetpoint"))
        if sp is not None:
            out.append(Reading("target_temperature", sp, "°C"))
            routes["target_temperature"] = Route("thermostatHeatingSetpoint", "setHeatingSetpoint")
    if has("airConditionerFanMode"):
        cur = str(_val(comp, "airConditionerFanMode", "fanMode") or "")
        supported = [str(x) for x in (_val(comp, "airConditionerFanMode", "supportedAcFanModes") or [])]
        _emit_fan(out, routes, "airConditionerFanMode", cur, supported)


def _emit_openings(out: list[Reading], routes: dict[str, Route], comp: dict) -> None:
    has = comp.__contains__
    if has("lock"):
        lk = _val(comp, "lock", "lock")
        out.append(Reading("lock", lk == "locked"))
        routes["lock"] = Route("lock")
    if has("windowShade") or has("windowShadeLevel"):
        lvl = _num(_val(comp, "windowShadeLevel", "shadeLevel"))
        if lvl is None:
            shade = str(_val(comp, "windowShade", "windowShade") or "")
            lvl = 100.0 if shade in ("open", "opening") else 0.0
        out.append(Reading("open_close", int(lvl), "%"))
        routes["open_close"] = Route("windowShade", has_level=has("windowShadeLevel"))
    elif has("doorControl"):
        door = str(_val(comp, "doorControl", "door") or "")
        out.append(Reading("open_close", 100 if door in ("open", "opening") else 0, "%"))
        routes["open_close"] = Route("doorControl")


def _emit_sensors(out: list[Reading], comp: dict) -> None:
    has = comp.__contains__
    if has("contactSensor"):
        out.append(Reading("contact", _val(comp, "contactSensor", "contact") == "open"))
    if has("motionSensor"):
        out.append(Reading("motion", _val(comp, "motionSensor", "motion") == "active"))
    if has("presenceSensor"):
        out.append(Reading("occupancy", _val(comp, "presenceSensor", "presence") == "present"))
    if has("smokeDetector"):
        out.append(Reading("smoke", _val(comp, "smokeDetector", "smoke") == "detected"))

    if has("battery"):
        b = _num(_val(comp, "battery", "battery"))
        if b is not None:
            out.append(Reading("battery", b, "%", category="diagnostic", diagnostic=True))


def map_component(comp: dict) -> tuple[list[Reading], dict[str, Route]]:
    """One component's status → (DIDA readings, per-capability command routes).

    `comp` is the raw ST shape {st_capability: {attribute: {value, unit}}}.
    """
    out: list[Reading] = []
    routes: dict[str, Route] = {}
    _emit_lighting(out, routes, comp)
    _emit_media(out, routes, comp)
    _emit_climate(out, routes, comp)
    _emit_openings(out, routes, comp)
    _emit_sensors(out, comp)
    _emit_power(out, comp)
    _emit_appliance(out, routes, comp)
    _emit_robot(out, routes, comp)

    # ONE reading per DIDA capability per entity — the model is one value per
    # (entity, capability). A duplicate cap (e.g. two appliance status texts, or a
    # device exposing both doorControl and windowShade) would put a duplicate field
    # on the entity and break the UI's keyed render (the card won't expand). Keep
    # the first occurrence, drop the rest.
    seen: set[str] = set()
    deduped: list[Reading] = []
    for r in out:
        if r.cap in seen:
            continue
        seen.add(r.cap)
        deduped.append(r)
    return deduped, routes


def _source_ids(opts) -> list[str]:
    """supportedInputSources may be a list of ids or a list of {id,name} maps."""
    if not isinstance(opts, list):
        return []
    ids: list[str] = []
    for o in opts:
        if isinstance(o, dict):
            v = o.get("id") or o.get("value") or o.get("name")
            if v:
                ids.append(str(v))
        elif o:
            ids.append(str(o))
    return ids


def _emit_mode(out, routes, stcap, setter, mapping, cur, supported):
    dida = mapping.get(cur, "auto")
    out.append(Reading("hvac_mode", dida))
    opts = sorted({mapping.get(m, "auto") for m in supported} or {dida})
    out.append(Reading("hvac_mode_options", json.dumps(opts), category="config"))
    r = Route(stcap, setter)
    r.modes = _rev(mapping, supported)
    routes["hvac_mode"] = r


def _emit_fan(out, routes, stcap, cur, supported):
    dida = _FAN_MODE.get(cur, "auto")
    out.append(Reading("fan_mode", dida))
    opts = sorted({_FAN_MODE.get(m, m) for m in supported} or {dida})
    out.append(Reading("fan_mode_options", json.dumps(opts), category="config"))
    r = Route(stcap, "setFanMode")
    r.modes = _rev(_FAN_MODE, supported)
    routes["fan_mode"] = r


def _emit_power(out, comp):
    if "powerConsumptionReport" in comp:
        pc = _val(comp, "powerConsumptionReport", "powerConsumption")
        if isinstance(pc, dict):
            p = _num(pc.get("power"))
            if p is not None:
                out.append(Reading("power", p, "W", category="diagnostic", diagnostic=True))
            e = _num(pc.get("energy"))
            if e is not None:  # Wh → kWh
                out.append(Reading("energy", round(e / 1000, 3), "kWh", category="diagnostic", diagnostic=True))
    elif "powerMeter" in comp:
        p = _num(_val(comp, "powerMeter", "power"))
        if p is not None:
            out.append(Reading("power", p, "W", category="diagnostic", diagnostic=True))


# Appliance operating-state capabilities → a settable machineState enum (run/
# pause/stop) + read-only job/progress text. One settable enum per component.
_MACHINE = {
    "washerOperatingState": ("setMachineState", "washerJobState"),
    "dryerOperatingState": ("setMachineState", "dryerJobState"),
    "dishwasherOperatingState": ("setMachineState", "dishwasherJobState"),
    "ovenOperatingState": ("setMachineState", "ovenJobState"),
}


def _emit_appliance(out, routes, comp):
    for stcap, (setter, job_attr) in _MACHINE.items():
        if stcap not in comp:
            continue
        state = _val(comp, stcap, "machineState")
        supported = [str(x) for x in (_val(comp, stcap, "supportedMachineStates") or ["run", "pause", "stop"])]
        running = str(state or "").lower() in ("run", "running")
        if state is not None:
            out.append(Reading("enum", str(state)))
            out.append(Reading("enum_options", json.dumps(supported), category="config"))
            r = Route(stcap, setter)
            routes["enum"] = r
        # The job phase (wash/rinse/spin/…) is the one useful status text — but ONLY
        # while running. When the cycle ends, clear it ("") so the last phase doesn't
        # stay latched in current_state forever (the adapter publishes only changes).
        job = _val(comp, stcap, job_attr)
        out.append(Reading("text", str(job) if (running and job is not None) else "", category="config"))
        # "Time left": completionTime is a raw ISO instant (UTC) → remaining SECONDS
        # (timezone-agnostic; the UI renders it as "45 min"). Emit 0 — not nothing —
        # once the job isn't running or completionTime is past, so a finished washer's
        # last non-zero countdown doesn't stay latched forever.
        left = _seconds_until(_val(comp, stcap, "completionTime"))
        out.append(Reading("remaining", left if (running and left) else 0))
        return  # one operating-state cap per component


# Robot vacuum: movement is read-only text; cleaning mode is the settable enum.
def _emit_robot(out, routes, comp):
    if "robotCleanerMovement" in comp:
        mv = _val(comp, "robotCleanerMovement", "robotCleanerMovement")
        if mv is not None:
            out.append(Reading("text", str(mv)))
    if "robotCleanerCleaningMode" in comp and "enum" not in routes:
        cur = _val(comp, "robotCleanerCleaningMode", "robotCleanerCleaningMode")
        supported = [str(x) for x in (_val(comp, "robotCleanerCleaningMode", "supportedCleaningMode") or [])]
        if cur is not None:
            out.append(Reading("enum", str(cur)))
            if supported:
                out.append(Reading("enum_options", json.dumps(supported), category="config"))
            routes["enum"] = Route("robotCleanerCleaningMode", "setRobotCleanerCleaningMode")


# ── command translation ──────────────────────────────────────────────────────

# Named (non-value) DIDA commands → the ST command on the route's capability.
_NAMED: dict[tuple[str, str], str] = {
    ("on_off", "turn_on"): "on", ("on_off", "turn_off"): "off",
    ("mute", "mute"): "mute", ("mute", "unmute"): "unmute",
    ("volume", "volume_up"): "volumeUp", ("volume", "volume_down"): "volumeDown",
    ("media_transport", "play"): "play", ("media_transport", "pause"): "pause",
    ("media_transport", "stop"): "stop",
    ("lock", "lock"): "lock", ("lock", "unlock"): "unlock",
}


_SETTERS: dict[str, Callable[[object, Route], object]] = {
    "set_brightness": lambda v, r: int(v),
    "set_color_temp": lambda v, r: int(v),
    "set_volume": lambda v, r: int(v),
    "set_hvac_mode": lambda v, r: r.modes.get(str(v), str(v)),
    "set_fan_mode": lambda v, r: r.modes.get(str(v), str(v)),
    "set_temperature": lambda v, r: float(v),
    "set_option": lambda v, r: str(v),
}


def translate_command(cap: str, command: str, args: dict, route: Route) -> tuple[str, str, list | None]:
    """(DIDA cap, command, args, route) → (ST capability, ST command, argument).

    Raises ValueError for a command this device can't take, so the adapter drops
    it loud rather than firing a bogus cloud call. `toggle`/`play_pause` are
    resolved to a concrete command by the adapter before calling this.
    """
    stcap = route.capability
    val = args.get("value") if args else None

    if (cap, command) in _NAMED:
        return stcap, _NAMED[(cap, command)], None

    # media next/previous live on a DIFFERENT ST capability than play/pause.
    if cap == "media_transport" and command in ("next", "previous"):
        return "mediaTrackControl", ("nextTrack" if command == "next" else "previousTrack"), None

    if command in _SETTERS:
        return stcap, route.setter, [_SETTERS[command](val, route)]
    if command == "set_color":
        return stcap, "setColor", [hex_to_hs(str(val))]
    if command == "set_source":
        return stcap, "setInputSource", [str(val)]
    if cap == "open_close":
        if command == "set_position":
            return "windowShadeLevel", "setShadeLevel", [int(val)]
        if command in ("open", "close"):
            return stcap, command, None
        if command == "stop":
            return stcap, "pause", None

    raise ValueError(f"no ST translation for {cap}/{command}")
