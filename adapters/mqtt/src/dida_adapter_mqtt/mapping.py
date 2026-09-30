"""zigbee2mqtt field <-> canonical capability mapping.

zigbee2mqtt publishes a JSON object per device on `<prefix>/<friendly_name>`,
e.g. {"state":"ON","brightness":254,"linkquality":120}. We translate the
fields we understand into canonical capability values (normalising units —
brightness 0..254 -> 0..100 %, color_temp mireds -> Kelvin, etc.) and ignore
the rest. Unknown fields are simply not mapped; they never reach the engine.

This is the ONE place protocol quirks live. The engine stays protocol-blind.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from dida_core import CAPABILITIES, CapabilityKind

Value = bool | int | float | str


def _unit(kind: CapabilityKind) -> str | None:
    return CAPABILITIES[kind].unit


def _on_off(v: object) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.upper() == "ON"
    return None


def _brightness(v: object) -> int | None:
    # z2m brightness is 0..254; canonical is 0..100 %.
    if isinstance(v, int | float) and not isinstance(v, bool):
        return max(0, min(100, round(v / 254 * 100)))
    return None


def _color_temp(v: object) -> int | None:
    # z2m color_temp is in mireds; canonical is Kelvin.
    if isinstance(v, int | float) and not isinstance(v, bool) and v > 0:
        return max(1700, min(6535, round(1_000_000 / v)))
    return None


def _number(v: object) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


def _bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def _contact_open(v: object) -> bool | None:
    # z2m `contact: true` means CLOSED. Canonical CONTACT is open=true.
    return (not v) if isinstance(v, bool) else None


def _action(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _position(v: object) -> int | None:
    # NOT `_number(v) and round(...)`: position 0 is falsy, so the old lambda
    # short-circuited and emitted the float 0.0 for an INT capability.
    n = _number(v)
    return None if n is None else round(n)


def _lock_state(v: object) -> bool | None:
    # z2m locks report state "LOCK"/"UNLOCK" on the same `state` field switches
    # use for ON/OFF — reading them through _on_off made a lock look like a
    # permanently-off switch. locked=True mirrors the LOCK capability contract.
    if isinstance(v, str) and v.upper() in ("LOCK", "LOCKED"):
        return True
    if isinstance(v, str) and v.upper() in ("UNLOCK", "UNLOCKED"):
        return False
    return None


# field name in z2m payload -> (capability, converter)
_FIELD_MAP: dict[str, tuple[CapabilityKind, Callable[[object], Value | None]]] = {
    "state": (CapabilityKind.ON_OFF, _on_off),
    "brightness": (CapabilityKind.BRIGHTNESS, _brightness),
    "color_temp": (CapabilityKind.COLOR_TEMP, _color_temp),
    "position": (CapabilityKind.OPEN_CLOSE, _position),
    "temperature": (CapabilityKind.TEMPERATURE, _number),
    # Tuya TRVs / thermostats report room temp as `local_temperature` (already
    # calibrated by z2m); plain sensors use `temperature`. A device sends one or
    # the other, so mapping both to TEMPERATURE is safe. NB: `device_temperature`
    # (internal MCU temp) is deliberately NOT mapped — it's not the room reading.
    "local_temperature": (CapabilityKind.TEMPERATURE, _number),
    # TRV/thermostat setpoint (z2m already in °C) — the one climate control worth
    # surfacing; renders as a number input, no enum-options plumbing needed.
    "current_heating_setpoint": (CapabilityKind.TARGET_TEMPERATURE, _number),
    "humidity": (CapabilityKind.HUMIDITY, _number),
    "illuminance_lux": (CapabilityKind.ILLUMINANCE, _number),
    "illuminance": (CapabilityKind.ILLUMINANCE, _number),
    "pm25": (CapabilityKind.PM25, _number),
    "voc_index": (CapabilityKind.VOC_INDEX, _number),
    "power": (CapabilityKind.POWER, _number),
    "energy": (CapabilityKind.ENERGY, _number),
    "battery": (CapabilityKind.BATTERY, _number),
    "voltage": (CapabilityKind.VOLTAGE, _number),
    "current": (CapabilityKind.CURRENT, _number),
    "ac_frequency": (CapabilityKind.FREQUENCY, _number),
    "contact": (CapabilityKind.CONTACT, _contact_open),
    "occupancy": (CapabilityKind.OCCUPANCY, _bool),
    # Tuya mmWave/PIR sensors report presence as `presence` (bool), not the
    # `occupancy` field Hue/Xiaomi use. Same canonical meaning.
    "presence": (CapabilityKind.OCCUPANCY, _bool),
    "motion": (CapabilityKind.MOTION, _bool),
    "smoke": (CapabilityKind.SMOKE, _bool),
    "action": (CapabilityKind.BUTTON, _action),
}

# Caps surfaced as technical/diagnostic readings rather than primary tiles —
# kept consistent with the curated-vs-diagnostic split used elsewhere in the UI.
# linkquality (LQI 0..255) has no clean dBm mapping so we drop it entirely.
DIAGNOSTIC_CAPS: frozenset[str] = frozenset(
    {
        CapabilityKind.VOLTAGE.value,
        CapabilityKind.CURRENT.value,
        CapabilityKind.FREQUENCY.value,
        CapabilityKind.POWER_FACTOR.value,
        CapabilityKind.SIGNAL.value,
    }
)


def build_state_updates(payload: dict) -> list[tuple[str, Value, str | None]]:
    """Map a z2m device payload to (capability, value, unit) tuples."""
    out: list[tuple[str, Value, str | None]] = []
    for field, (kind, conv) in _FIELD_MAP.items():
        if field not in payload:
            continue
        # z2m locks share the `state` field with switches but speak LOCK/UNLOCK —
        # route those to the lock capability instead of a false on_off=False.
        if field == "state":
            locked = _lock_state(payload[field])
            if locked is not None:
                out.append((CapabilityKind.LOCK.value, locked, None))
                continue
        value = conv(payload[field])
        if value is None:
            continue
        out.append((kind.value, value, _unit(kind)))
    return out


def command_to_mqtt(capability: str, command: str, args: dict) -> dict:
    """Map a validated capability command to a z2m `set` payload."""
    if capability == CapabilityKind.ON_OFF.value:
        return {"state": {"turn_on": "ON", "turn_off": "OFF", "toggle": "TOGGLE"}[command]}
    if capability == CapabilityKind.BRIGHTNESS.value:  # canonical 0..100 -> z2m 0..254
        return {"brightness": round(int(args["value"]) / 100 * 254)}
    if capability == CapabilityKind.COLOR_TEMP.value:  # Kelvin -> mireds
        return {"color_temp": round(1_000_000 / int(args["value"]))}
    if capability == CapabilityKind.OPEN_CLOSE.value:
        if command == "set_position":
            return {"position": int(args["value"])}
        return {"state": {"open": "OPEN", "close": "CLOSE", "stop": "STOP"}[command]}
    if capability == CapabilityKind.LOCK.value:
        return {"state": {"lock": "LOCK", "unlock": "UNLOCK"}[command]}
    if capability == CapabilityKind.TARGET_TEMPERATURE.value:  # set_temperature {value: °C}
        return {"current_heating_setpoint": float(args["value"])}
    raise ValueError(f"no mqtt mapping for {capability}/{command}")


# ---------------------------------------------------------------------------
# Generic (exposes-driven) surfacing of EVERY OTHER field a device reports.
#
# The semantic map above gives known fields a proper capability (icon/unit/
# control). Everything else a z2m device publishes — a TRV's dozen tuning knobs,
# an mmWave sensor's detection params, a light's power-on behaviour — is surfaced
# here as a single-capability facet: WRITABLE fields (z2m `set` access) become
# editable controls (BOOLEAN toggle / NUMBER / ENUM dropdown) that command the
# device, read-only ones become sensors. These land as separate entities
# (`mqtt:<device>:<field>`) grouped under the device and flagged diagnostic, so
# they default hidden and the user re-exposes what they want in Settings →
# Adapters ("expose everything, toggle the noise off — and edit what's editable").
# ---------------------------------------------------------------------------

# Transport / bookkeeping fields that are never a device reading.
_IGNORE: frozenset[str] = frozenset(
    {"linkquality", "update", "update_available", "last_seen", "elapsed", "update_state"}
)


@dataclass(frozen=True, slots=True)
class Expose:
    """The subset of a z2m `exposes` entry we need to type/label/CONTROL a field.

    `access` bit2 (set) is what makes a field editable: a writable field is
    surfaced as a control (BOOLEAN toggle / NUMBER / ENUM dropdown) rather than a
    read-only sensor. `values` (enum) and `vmin/vmax/vstep` (numeric) drive the
    control's option list / bounds."""

    type: str = ""                 # binary | numeric | enum | text | (composite skipped)
    access: int = 1                # bit1=published, bit2=set, bit4=get
    unit: str | None = None
    label: str | None = None
    value_on: object = None        # binary: raw value meaning True
    value_off: object = None       # binary: raw value meaning False
    category: str | None = None    # "config" | "diagnostic" | None
    values: tuple[str, ...] = ()   # enum: the allowed options
    vmin: float | None = None      # numeric bounds (for the NUMBER control)
    vmax: float | None = None
    vstep: float | None = None

    @property
    def writable(self) -> bool:
        return bool(self.access & 2)


def parse_exposes(exposes: object) -> dict[str, Expose]:
    """Flatten a device's z2m `exposes` tree (composites carry `features`) into
    field-name -> Expose. Best-effort: anything odd is simply omitted."""
    out: dict[str, Expose] = {}

    def walk(lst: object) -> None:
        if not isinstance(lst, list):
            return
        for e in lst:
            if not isinstance(e, dict):
                continue
            if isinstance(e.get("features"), list):
                walk(e["features"])
                continue
            prop = e.get("property")
            if not isinstance(prop, str) or not prop:
                continue
            out[prop] = Expose(
                type=str(e.get("type") or ""),
                access=int(e.get("access") or 1),
                unit=e.get("unit"),
                label=e.get("label") if isinstance(e.get("label"), str) else None,
                value_on=e.get("value_on"),
                value_off=e.get("value_off"),
                category=e.get("category") if isinstance(e.get("category"), str) else None,
                values=tuple(v for v in (e.get("values") or []) if isinstance(v, str)),
                vmin=e.get("value_min"),
                vmax=e.get("value_max"),
                vstep=e.get("value_step"),
            )

    walk(exposes)
    return out


# z2m states a device's physical category on the top-level exposes composite
# (`{"type": "light", "features": [...]}`) — capture it so an ON/OFF-only light
# (no brightness) isn't misread as a bare switch by capability sniffing.
_Z2M_DEVICE_TYPES = ("light", "switch", "cover", "lock")


def device_type_from_exposes(exposes: object) -> str | None:
    """The device's DeviceType hint from the top-level z2m exposes composite
    (light / switch / cover / lock). None when the device is just sensors."""
    if not isinstance(exposes, list):
        return None
    for e in exposes:
        if isinstance(e, dict) and e.get("type") in _Z2M_DEVICE_TYPES:
            return str(e.get("type"))
    return None


def _humanize(field: str) -> str:
    return field.replace("_", " ").strip().capitalize() or field


# Some z2m binary sub-features carry a generic label ("State") rather than a
# descriptive one — which would render several toggles on one device all named
# "State". Fall back to the (always-meaningful) property name in that case.
_GENERIC_LABELS: frozenset[str] = frozenset({"state", ""})


def _facet_name(field: str, expose: Expose | None) -> str:
    label = (expose.label or "").strip() if expose else ""
    if label and label.lower() not in _GENERIC_LABELS:
        return label
    return _humanize(field)


# Fields that are the PRIMARY, day-to-day control of their device — promoted to
# the card hero (visible by default) even though z2m lists them alongside a pile
# of settings. Everything else writable is treated as configuration; read-only
# technical fields as diagnostics.
_CONTROL_FIELDS: frozenset[str] = frozenset({
    "system_mode", "preset",  # thermostat / TRV: the mode + preset you actually touch
})
# Read-only technical / status fields → the collapsed diagnostics section.
_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset({
    "battery_low", "linkquality", "voltage", "power_outage_count", "device_temperature",
    "running_state", "window_open", "position", "update_available", "power_on_behavior_reported",
})


def facet_category(field: str, expose: Expose | None) -> str:
    """Classify a facet for the device card's display grouping:
      "control"    — a primary, everyday control (hero, visible by default),
      "config"     — a writable setting (collapsed "settings" section, opt-in),
      "diagnostic" — a read-only technical/status field (collapsed, opt-in).
    z2m's own `category` is honoured when the converter set it; most converters
    don't, so a curated field list + the writable/read-only split fills the gap."""
    if field in _CONTROL_FIELDS:
        return "control"
    if field in _DIAGNOSTIC_FIELDS:
        return "diagnostic"
    if expose is not None and expose.category in ("config", "diagnostic"):
        return expose.category
    if expose is not None and not expose.writable:
        return "diagnostic"  # read-only leftover technical reading
    return "config"          # writable knob = a setting


@dataclass(frozen=True, slots=True)
class Facet:
    """One non-semantic field resolved to a DIDA single-cap facet. A WRITABLE
    field carries a control capability (BOOLEAN/NUMBER/ENUM) plus its companion
    *_options metadata; a read-only one is a BINARY/MEASUREMENT/TEXT sensor."""

    field: str
    cap: str
    value: Value
    unit: str | None
    name: str
    options_cap: str | None = None   # "number_options" / "enum_options"
    options_val: str | None = None   # the companion JSON blob (bounds / option list)
    category: str = "control"        # display grouping: control | config | diagnostic


def generic_field_spec(field: str, value: object, expose: Expose | None) -> Facet | None:
    """Map ONE non-semantic field to a Facet. A field the device lets you SET
    (z2m `set` access) becomes an editable control; the rest are read-only
    sensors. None for fields we can't represent (composites, lists, nulls)."""
    if field in _IGNORE or value is None:
        return None
    etype = expose.type if expose else ""
    name = _facet_name(field, expose)
    unit = expose.unit if expose else None
    w = expose.writable if expose else False
    cat = facet_category(field, expose)

    # Binary → editable BOOLEAN toggle if writable, else read-only BINARY.
    if etype == "binary" or (not etype and isinstance(value, bool)):
        if isinstance(value, bool):
            b = value
        elif expose is not None and value == expose.value_on:
            b = True
        elif expose is not None and value == expose.value_off:
            b = False
        else:
            return None
        cap = CapabilityKind.BOOLEAN.value if w else CapabilityKind.BINARY.value
        return Facet(field, cap, b, None, name, category=cat)

    # Numeric → editable NUMBER (with bounds) if writable, else read-only MEASUREMENT.
    if etype == "numeric" or (not etype and isinstance(value, (int, float)) and not isinstance(value, bool)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if w:
            opts = {k: v for k, v in (("min", expose.vmin), ("max", expose.vmax),
                                      ("step", expose.vstep), ("unit", unit)) if v is not None}
            return Facet(field, CapabilityKind.NUMBER.value, float(value), unit, name,
                         CapabilityKind.NUMBER_OPTIONS.value, json.dumps(opts), category=cat)
        return Facet(field, CapabilityKind.MEASUREMENT.value, float(value), unit, name, category=cat)

    # Enum → editable ENUM dropdown (from `values`) if writable, else read-only TEXT.
    if etype in ("enum", "text") or isinstance(value, str):
        if etype == "enum" and w and expose and expose.values:
            return Facet(field, CapabilityKind.ENUM.value, str(value), None, name,
                         CapabilityKind.ENUM_OPTIONS.value, json.dumps(list(expose.values)), category=cat)
        return Facet(field, CapabilityKind.TEXT.value, str(value), None, name, category=cat)

    return None  # composite / list / unknown — skip


def build_generic_updates(payload: dict, exposes: dict[str, Expose]) -> list[Facet]:
    """Every non-semantic field in a payload -> a Facet (control or sensor)."""
    out: list[Facet] = []
    for field, value in payload.items():
        if field in _FIELD_MAP:  # semantic — handled by build_state_updates
            continue
        f = generic_field_spec(field, value, exposes.get(field))
        if f is not None:
            out.append(f)
    return out


def facet_meta(field: str, expose: Expose | None) -> tuple[str, str | None, str | None, str, str | None, str] | None:
    """Resolve a field's facet SHAPE from its exposes alone (no value) →
    (cap, options_cap, options_val, name, unit, category). Lets the adapter
    announce the FULL editable catalog the instant it reads `bridge/devices`
    (retained), for every field the device declares — including write-only ones
    (a light's `effect`) and query-only config that never appear in the state
    stream. None for non-semantic fields we can't type or that the semantic map owns."""
    if field in _IGNORE or field in _FIELD_MAP or expose is None:
        return None
    name = _facet_name(field, expose)
    cat = facet_category(field, expose)
    w = expose.writable
    if expose.type == "binary":
        return (CapabilityKind.BOOLEAN.value if w else CapabilityKind.BINARY.value, None, None, name, None, cat)
    if expose.type == "numeric":
        if w:
            opts = {k: v for k, v in (("min", expose.vmin), ("max", expose.vmax),
                                      ("step", expose.vstep), ("unit", expose.unit)) if v is not None}
            return (CapabilityKind.NUMBER.value, CapabilityKind.NUMBER_OPTIONS.value,
                    json.dumps(opts), name, expose.unit, cat)
        return (CapabilityKind.MEASUREMENT.value, None, None, name, expose.unit, cat)
    if expose.type == "enum":
        if w and expose.values:
            return (CapabilityKind.ENUM.value, CapabilityKind.ENUM_OPTIONS.value,
                    json.dumps(list(expose.values)), name, None, cat)
        return (CapabilityKind.TEXT.value, None, None, name, None, cat)
    if expose.type == "text":
        return (CapabilityKind.TEXT.value, None, None, name, None, cat)
    return None  # composite / list / unknown


def generic_command(
    field: str, capability: str, command: str, args: dict, expose: Expose | None, *, current: bool | None = None
) -> dict:
    """Convert a facet control command to a z2m `{field: value}` /set payload.

    `current` is the field's last known boolean value; it resolves a `toggle`
    (which z2m's generic /set can't express) into the opposite turn_on/turn_off.
    A toggle with no known state raises — the adapter logs it, never a silent no-op."""
    if capability == CapabilityKind.BOOLEAN.value:
        if command == "toggle":
            if current is None:
                raise ValueError(f"boolean facet {field}: cannot toggle without a known state")
            command = "turn_off" if current else "turn_on"
        if command == "turn_on":
            return {field: expose.value_on if (expose and expose.value_on is not None) else True}
        if command == "turn_off":
            return {field: expose.value_off if (expose and expose.value_off is not None) else False}
        raise ValueError(f"boolean facet {field}: unsupported command {command}")
    if capability == CapabilityKind.NUMBER.value and command == "set_value":
        v = float(args["value"])
        return {field: int(v) if v.is_integer() else v}
    if capability == CapabilityKind.ENUM.value and command == "set_option":
        return {field: str(args["value"])}
    raise ValueError(f"no facet set mapping for {capability}/{command}")


def shape_networkmap(value: object, slug_by_ieee: dict[str, str]) -> dict:
    """Reduce zigbee2mqtt's raw networkmap to what the UI graph needs.

    z2m's `type: raw` payload is a node list (each with its routing table and a
    failed-scan list we don't draw) and a link list of neighbour relations. We keep
    one node per device — typed coordinator/router/end-device and tied to our own
    device slug when the ieee is one we know — and one link per relation carrying
    its link quality, the single number a person reads off a mesh map."""
    if not isinstance(value, dict):
        return {"nodes": [], "links": []}
    nodes = []
    for n in value.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        ieee = n.get("ieeeAddr") or ""
        nodes.append({
            "ieee": ieee,
            "name": n.get("friendlyName") or ieee,
            "type": str(n.get("type") or "").lower(),  # coordinator/router/enddevice
            "slug": slug_by_ieee.get(ieee),
            "lastSeen": n.get("lastSeen"),
        })
    links = []
    for link in value.get("links") or []:
        if not isinstance(link, dict):
            continue
        src = link.get("source")
        tgt = link.get("target")
        src_ieee = src.get("ieeeAddr") if isinstance(src, dict) else None
        tgt_ieee = tgt.get("ieeeAddr") if isinstance(tgt, dict) else None
        if not src_ieee or not tgt_ieee:
            continue
        lqi = link.get("lqi")
        links.append({
            "source": src_ieee,
            "target": tgt_ieee,
            "lqi": lqi if lqi is not None else link.get("linkquality"),
            "relationship": link.get("relationship"),
            "depth": link.get("depth"),
        })
    return {"nodes": nodes, "links": links}
