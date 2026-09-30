"""HomeKit (HAP) characteristic <-> canonical capability mapping.

A HAP accessory exposes services, each holding characteristics identified by a
type UUID (On, OccupancyDetected, CurrentTemperature, …). We translate the
characteristics we understand into canonical capability values, normalising
HAP's conventions (e.g. ContactSensorState 1 = "not detected" = open).

This is the ONE place HAP quirks live. The engine stays protocol-blind;
aiohomekit speaks the protocol, this maps its vocabulary onto ours.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiohomekit.model.characteristics import CharacteristicsTypes as C
from dida_core import CAPABILITIES, CapabilityKind

log = logging.getLogger("dida.adapter.homekit")

Value = bool | int | float | str


def unit_for(capability: str) -> str | None:
    try:
        return CAPABILITIES[CapabilityKind(capability)].unit
    except (ValueError, KeyError):
        return None


def _num(v: object) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    return None


def _bool(v: object) -> bool | None:
    """HAP booleans arrive as 0/1 ints (or bool). Return None for anything else.

    Critically, a HAP error/status read yields None (no "value" key) — that must
    NOT be silently coerced to False, which would publish e.g. lock=unlocked or
    motion=clear on a transient read error and fire phantom automations."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):  # HAP sends 0/1
        return bool(v)
    return None


# Readable characteristic UUID -> (capability, decoder, diagnostic)
READ_MAP: dict[str, tuple[str, Callable[[object], Value | None], bool]] = {
    C.ON: (CapabilityKind.ON_OFF.value, _bool, False),
    C.BRIGHTNESS: (CapabilityKind.BRIGHTNESS.value, lambda v: max(0, min(100, int(v))) if _num(v) is not None else None, False),
    C.OCCUPANCY_DETECTED: (CapabilityKind.OCCUPANCY.value, _bool, False),
    C.MOTION_DETECTED: (CapabilityKind.MOTION.value, _bool, False),
    # HAP ContactSensorState: 0 = detected (closed), 1 = not detected (open).
    C.CONTACT_STATE: (CapabilityKind.CONTACT.value, _bool, False),
    C.TEMPERATURE_CURRENT: (CapabilityKind.TEMPERATURE.value, _num, False),
    C.RELATIVE_HUMIDITY_CURRENT: (CapabilityKind.HUMIDITY.value, _num, False),
    C.LIGHT_LEVEL_CURRENT: (CapabilityKind.ILLUMINANCE.value, _num, False),
    C.BATTERY_LEVEL: (CapabilityKind.BATTERY.value, _num, True),
    # HAP LockCurrentState: 1 = secured; 0/2/3 = unsecured/jammed/unknown; None on error.
    C.LOCK_MECHANISM_CURRENT_STATE: (CapabilityKind.LOCK.value, lambda v: (v == 1) if isinstance(v, int) else None, False),
    C.POSITION_CURRENT: (CapabilityKind.OPEN_CLOSE.value, lambda v: max(0, min(100, int(v))) if _num(v) is not None else None, False),
}

# Writable target characteristic per capability (for actuators).
WRITE_MAP: dict[str, str] = {
    CapabilityKind.ON_OFF.value: C.ON,
    CapabilityKind.BRIGHTNESS.value: C.BRIGHTNESS,
    CapabilityKind.LOCK.value: C.LOCK_MECHANISM_TARGET_STATE,
    CapabilityKind.OPEN_CLOSE.value: C.POSITION_TARGET,
}


def normalize_type(t: str) -> str:
    """HAP characteristic types come as short ("71") or full UUIDs; canonicalise."""
    from aiohomekit.uuid import normalize_uuid

    try:
        return normalize_uuid(t)
    except Exception:
        log.debug("homekit: service type %r is not a UUID", t, exc_info=True)
        return t


def encode_command(capability: str, command: str, args: dict) -> Value:
    """Canonical command -> HAP target-characteristic value."""
    if capability == CapabilityKind.ON_OFF.value:
        return command == "turn_on"
    if capability == CapabilityKind.BRIGHTNESS.value:
        return int(args["value"])
    if capability == CapabilityKind.LOCK.value:
        return 1 if command == "lock" else 0
    if capability == CapabilityKind.OPEN_CLOSE.value:
        if command == "set_position":
            return int(args["value"])
        if command == "open":
            return 100
        if command == "close":
            return 0
        # There's no generic "stop" on the POSITION_TARGET characteristic — the old
        # fall-through mapped stop → 0, silently CLOSING the cover. Refuse it (fail
        # loud → logged as uncodable); a real HoldPosition write would need its own
        # target characteristic.
        raise ValueError(f"unsupported cover command: {command}")
    raise ValueError(f"no homekit mapping for {capability}/{command}")
