"""Roidmi (roidmi.vacuum.v60, Eve series) MiOT surface → canonical capabilities.

The siid/piid map and the value vocabularies come from the device's public MiOT
spec (as catalogued by python-miio's roidmivacuum_miot integration). Pure
functions — everything here is unit-testable without a device.
"""

from __future__ import annotations

# The properties one status poll reads.
PROPS: list[dict] = [
    {"did": "state", "siid": 2, "piid": 1},
    {"did": "error_code", "siid": 2, "piid": 2},
    {"did": "fanspeed_mode", "siid": 2, "piid": 4},
    {"did": "battery_level", "siid": 3, "piid": 1},
]

# MiOT actions / writable properties the commands use.
ACTION_START = (2, 1)
ACTION_STOP = (2, 2)   # halts in place — the closest thing to "pause" the spec has
ACTION_HOME = (3, 1)
PROP_FANSPEED = (2, 4)

# Device run-state → canonical `vacuum` state. Rfctrl is manual remote drive —
# the robot is moving and cleaning, so it reads as active.
_STATE: dict[int, str] = {
    1: "idle",        # Dormant
    2: "idle",
    3: "paused",
    4: "cleaning",    # Sweeping
    5: "returning",   # GoCharging
    6: "charging",
    7: "error",
    8: "cleaning",    # Rfctrl
    9: "docked",      # Fullcharge
    10: "idle",       # Shutdown
    11: "paused",     # FindChargerPause
}

# Suction level ↔ canonical option token (the `enum` capability's vocabulary).
FAN_OPTIONS: list[str] = ["silent", "basic", "strong", "max"]
_FAN_BY_ID: dict[int, str] = {1: "silent", 2: "basic", 3: "strong", 4: "max"}
_FAN_BY_NAME: dict[str, int] = {v: k for k, v in _FAN_BY_ID.items()}

ERROR_CODES: dict[int, str] = {
    0: "no fault",
    1: "low battery, finding charger",
    2: "low battery, powering off",
    3: "wheel trapped",
    4: "collision error",
    5: "tilted during task",
    6: "lidar point error",
    7: "front wall sensor error",
    8: "PSD sensor dirty",
    9: "main brush fatal",
    10: "side brush error",
    11: "fan speed error",
    12: "lidar covered",
    13: "dustbin full",
    14: "dustbin out",
    15: "dustbin full and out",
    16: "physically trapped",
    17: "picked up during task",
    18: "no water box during task",
    19: "water box empty",
    20: "cannot reach target",
    21: "start forbidden zone",
    22: "drop detected",
    23: "water pump error",
    24: "find charger failed",
    25: "low power clean",
}


def fan_id(option: str) -> int | None:
    return _FAN_BY_NAME.get(option)


def status_caps(results: list[dict]) -> tuple[dict[str, object], str | None]:
    """MiOT get_properties result → ({capability: value}, fault description).

    Only readings the device actually answered (code == 0) are mapped — a poll
    where one property errored still publishes the rest. The fault description
    is None when the robot reports no error state."""
    read: dict[str, object] = {}
    for r in results:
        if r.get("code") == 0 and "value" in r:
            read[r.get("did", "")] = r["value"]

    caps: dict[str, object] = {}
    state_id = read.get("state")
    if isinstance(state_id, int):
        caps["vacuum"] = _STATE.get(state_id, "idle")
    battery = read.get("battery_level")
    if isinstance(battery, int | float):
        caps["battery"] = float(battery)
    fan = read.get("fanspeed_mode")
    if isinstance(fan, int) and fan in _FAN_BY_ID:
        caps["enum"] = _FAN_BY_ID[fan]

    fault: str | None = None
    code = read.get("error_code")
    if caps.get("vacuum") == "error":
        fault = ERROR_CODES.get(code, f"error code {code}") if isinstance(code, int) else "unknown fault"
    return caps, fault
