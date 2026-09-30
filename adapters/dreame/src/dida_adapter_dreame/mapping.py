"""Dreame vacuum MIoT surface → canonical capabilities.

The siid/piid/aiid map and the value vocabularies come from the device's MIoT
spec as catalogued by the dreame-vacuum project (`dreame.vacuum.r9419*`, the
L40s Pro Ultra family). Pure functions — unit-testable without an account.
"""

from __future__ import annotations

from dataclasses import dataclass

# The properties one status poll reads. `did` is a local label here; the cloud
# call rewrites it to the device id before sending.
PROPS: list[dict] = [
    {"did": "state", "siid": 2, "piid": 1},
    {"did": "error_code", "siid": 2, "piid": 2},
    {"did": "battery_level", "siid": 3, "piid": 1},
    {"did": "charging_status", "siid": 3, "piid": 2},
    {"did": "suction_level", "siid": 4, "piid": 4},
]

ACTION_START = (2, 1)
ACTION_PAUSE = (2, 2)
ACTION_DOCK = (3, 1)
PROP_SUCTION = (4, 4)
PROP_WATER_VOLUME = (4, 5)

# Device run-state → canonical `vacuum` state. Station chores (washing, drying,
# mop install/remove) happen ON the dock, so they read as docked rather than
# inventing states the capability does not have.
_STATE: dict[int, str] = {
    1: "cleaning",     # sweeping
    2: "idle",
    3: "paused",
    4: "error",
    5: "returning",
    6: "charging",
    7: "cleaning",     # mopping
    8: "docked",       # drying
    9: "docked",       # washing
    10: "returning",   # returning to wash
    11: "cleaning",    # building the map
    12: "cleaning",    # sweeping and mopping
    13: "docked",      # charging completed
    14: "docked",      # upgrading
    15: "cleaning",    # clean summon
    16: "docked",      # station reset
    17: "returning",   # returning to install mop
    18: "returning",   # returning to remove mop
    19: "docked",      # water check
}

# Suction level ↔ canonical option token (the `enum` capability's vocabulary).
SUCTION_OPTIONS: list[str] = ["silent", "basic", "strong", "max"]
_SUCTION_BY_ID: dict[int, str] = {0: "silent", 1: "basic", 2: "strong", 3: "max"}
_SUCTION_BY_NAME: dict[str, int] = {v: k for k, v in _SUCTION_BY_ID.items()}

ERROR_CODES: dict[int, str] = {
    0: 'no error',
    1: 'drop',
    2: 'cliff',
    3: 'bumper',
    4: 'gesture',
    5: 'bumper repeat',
    6: 'drop repeat',
    7: 'optical flow',
    8: 'box',
    9: 'tankbox',
    10: 'waterbox empty',
    11: 'box full',
    12: 'brush',
    13: 'side brush',
    14: 'fan',
    15: 'left wheel motor',
    16: 'right wheel motor',
    17: 'turn suffocate',
    18: 'forward suffocate',
    19: 'charger get',
    20: 'battery low',
    21: 'charge fault',
    22: 'battery percentage',
    23: 'heart',
    24: 'camera occlusion',
    25: 'move',
    26: 'flow shielding',
    27: 'infrared shielding',
    28: 'charge no electric',
    29: 'battery fault',
    30: 'fan speed error',
    31: 'leftwhell speed',
    32: 'rightwhell speed',
    33: 'bmi055 acce',
    34: 'bmi055 gyro',
    35: 'xv7001',
    36: 'left magnet',
    37: 'right magnet',
    38: 'flow error',
    39: 'infrared fault',
    40: 'camera fault',
    41: 'strong magnet',
    42: 'water pump',
    43: 'rtc',
    44: 'auto key trig',
    45: 'p3v3',
    46: 'camera idle',
    47: 'blocked',
    48: 'lds error',
    49: 'lds bumper',
    50: 'water pump 2',
    51: 'filter blocked',
    54: 'edge',
    55: 'carpet',
    56: 'laser',
    57: 'edge 2',
    58: 'ultrasonic',
    59: 'no go zone',
    61: 'route',
    62: 'route 2',
    63: 'blocked 2',
    64: 'blocked 3',
    65: 'restricted',
    66: 'restricted 2',
    67: 'restricted 3',
    68: 'remove mop',
    69: 'mop removed',
    70: 'mop removed 2',
    71: 'mop pad stop rotate',
    72: 'mop pad stop rotate 2',
    74: 'mop install failed',
    75: 'low battery turn off',
    76: 'dirty tank not installed',
    78: 'robot in hidden room',
    79: 'lds failed to lift',
    80: 'robot stuck',
    81: 'robot stuck repeat',
    82: 'slippery floor',
    84: 'unknown error',
    85: 'check mop install',
    86: 'dirty water tank full',
    88: 'retractable leg stuck',
    89: 'internal error',
    90: 'robot stuck 2',
    91: 'robot stuck on tables',
    92: 'robot stuck on passage',
    93: 'robot stuck on threshold',
    94: 'robot stuck on low lying area',
    95: 'robot stuck on ramp',
    96: 'robot stuck on obstacle',
    97: 'robot stuck on pet',
    98: 'robot stuck on slippery surface',
    99: 'robot stuck on carpet',
    101: 'bin full',
    102: 'bin open',
    103: 'bin open 2',
    104: 'bin full 2',
    105: 'water tank',
    106: 'dirty water tank',
    107: 'water tank dry',
    108: 'dirty water tank 2',
    109: 'dirty water tank blocked',
    110: 'dirty water tank pump',
    111: 'mop pad',
    112: 'wet mop pad',
    114: 'clean mop pad',
    116: 'clean tank level',
    117: 'station disconnected',
    118: 'dirty tank level',
    119: 'washboard level',
    120: 'no mop in station',
    121: 'dust bag full',
    122: 'unknown warning',
    123: 'self test failed',
    124: 'washboard not working',
    125: 'drainage failed',
    126: 'mop not detected',
    127: 'mop holder error',
    128: 'dock error',
    129: 'wash failed',
    200: 'robot stuck on curtain',
    201: 'edge mop stop rotate',
    202: 'edge mop detached',
    203: 'chassis lift malfunction',
    207: 'internal error 2',
    209: 'mop cover error',
    210: 'roller mop error',
    212: 'robotic arm stopped',
    213: 'onboard water tank empty',
    214: 'onboard dirty water tank full',
    215: 'mop not installed',
    217: 'lds error 2',
    218: 'roller mop error 2',
    222: 'fluffing roller error',
    223: 'mop cover error 2',
    224: 'mop cover error 3',
    225: 'roller mop error 3',
    226: 'blocked by obstacle',
    227: 'drainage outlet filter',
    228: 'main wheels error',
    229: 'internal error 3',
    230: 'internal error 4',
    1000: 'return to charge failed',
}


def suction_id(option: str) -> int | None:
    return _SUCTION_BY_NAME.get(option)


def status_caps(results: list[dict]) -> tuple[dict[str, object], str | None]:
    """MIoT get_properties result → ({capability: value}, fault description).

    Only readings the device actually answered (code == 0) are mapped — a poll
    where one property errored still publishes the rest. The fault description
    is None when the robot reports no error."""
    read: dict[str, object] = {}
    for r in results:
        if r.get("code") == 0 and "value" in r:
            read[r.get("did", "")] = r["value"]

    caps: dict[str, object] = {}
    state_id = read.get("state")
    if isinstance(state_id, int):
        caps["vacuum"] = _STATE.get(state_id, "idle")
    battery = read.get("battery_level")
    if isinstance(battery, int | float) and not isinstance(battery, bool):
        caps["battery"] = float(battery)
    suction = read.get("suction_level")
    if isinstance(suction, int) and suction in _SUCTION_BY_ID:
        caps["enum"] = _SUCTION_BY_ID[suction]

    fault: str | None = None
    code = read.get("error_code")
    if caps.get("vacuum") == "error":
        fault = ERROR_CODES.get(code, f"error code {code}") if isinstance(code, int) else "unknown fault"
    return caps, fault


# ── the rest of the robot: consumables, station, and the job in progress ───────
#
# The app exposes ~235 properties; almost all of them are settings that belong in
# the app, not in a house dashboard. These are the ones DIDA has a use for: the
# parts that wear out (so an alert can fire before something fails unnoticed), the
# station's own stock and chores, and how far the current job has got.

@dataclass(frozen=True)
class Extra:
    key: str                 # label inside the poll result
    siid: int
    piid: int
    entity: str              # entity id suffix (dreame:<entity>)
    name: str
    capability: str          # "number" | "enum" | "duration"
    unit: str | None = None
    values: dict[int, str] | None = None   # raw -> option token, for enums
    scale: float = 1.0
    diagnostic: bool = True
    writable: bool = False


EXTRAS: tuple[Extra, ...] = (
    # Wear parts, as a percentage left.
    Extra("main_brush_left", 9, 2, "main_brush", "Main brush", "number", unit="%"),
    Extra("side_brush_left", 10, 2, "side_brush", "Side brush", "number", unit="%"),
    Extra("filter_left", 11, 1, "filter", "Filter", "number", unit="%"),
    Extra("sensor_dirty_left", 16, 1, "sensors", "Sensors", "number", unit="%"),
    # The station's stock: each has its own vocabulary, and "ok" is always the
    # value that needs no attention.
    Extra("dust_bag_status", 27, 3, "dust_bag", "Dust bag", "enum",
          values={0: "ok", 1: "missing", 2: "check"}),
    Extra("clean_water_tank_status", 27, 1, "clean_water", "Clean water tank", "enum",
          values={0: "ok", 1: "missing", 2: "low", 3: "checking"}),
    Extra("dirty_water_tank_status", 27, 2, "dirty_water", "Waste water tank", "enum",
          values={0: "ok", 1: "full"}),
    Extra("detergent_status", 27, 4, "detergent", "Detergent", "enum",
          values={0: "ok", 1: "disabled", 2: "low"}),
    # What the station is doing right now — visible, not diagnostic: a robot that
    # is washing its mops is unavailable for the next half hour.
    Extra("self_wash_base_status", 4, 25, "station", "Station", "text", diagnostic=False,
          values={0: "idle", 1: "washing", 2: "drying", 3: "returning", 4: "paused",
                  5: "adding water", 6: "adding water", 7: "returning to dry"}),
    # The job in progress.
    Extra("cleaned_area", 4, 3, "cleaned_area", "Cleaned area", "number", unit="m²"),
    Extra("cleaning_time", 4, 2, "cleaning_time", "Cleaning time", "duration", scale=60.0),
    # Mop wetness, the one station setting worth steering from the house: it is the
    # difference between a damp floor and a wet one, and it is per-job.
    Extra("water_volume", 4, 5, "water_volume", "Water level", "enum", diagnostic=False,
          writable=True, values={1: "low", 2: "medium", 3: "high"}),
)

EXTRA_PROPS: list[dict] = [{"did": e.key, "siid": e.siid, "piid": e.piid} for e in EXTRAS]
_BY_KEY: dict[str, Extra] = {e.key: e for e in EXTRAS}


def extra_options(extra: Extra) -> list[str]:
    """The enum vocabulary, de-duplicated and in the device's own order (two raw
    values can mean the same thing — the station reports adding water twice)."""
    seen: list[str] = []
    for token in (extra.values or {}).values():
        if token not in seen:
            seen.append(token)
    return seen


def extra_value(extra: Extra, raw: object) -> object | None:
    # A vocabulary decides the value whatever it is published as: `station` reads the
    # same raw numbers as an enum but is READ-ONLY, so it goes out as text — an enum
    # would render a picker whose choices nothing could honour.
    if extra.values is not None:
        return extra.values.get(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw) * extra.scale
    return None


def extra_caps(results: list[dict]) -> dict[str, object]:
    """Poll result → {entity suffix: value}, skipping anything the robot did not
    answer or whose raw value is outside the vocabulary we know."""
    out: dict[str, object] = {}
    for r in results:
        extra = _BY_KEY.get(r.get("did", ""))
        if extra is None or r.get("code") != 0 or "value" not in r:
            continue
        value = extra_value(extra, r["value"])
        if value is not None:
            out[extra.entity] = value
    return out


def water_volume_id(option: str) -> int | None:
    extra = _BY_KEY["water_volume"]
    for raw, token in (extra.values or {}).items():
        if token == option:
            return raw
    return None


# ── what the robot does with the floor: vacuum, mop, or both ──────────────────
#
# `cleaning_mode` is not a plain enum: the robot packs three settings into one
# integer — the mode in the low two bits, the self-clean interval in the next
# byte, the mopping route above that. A write therefore has to REPLACE the low
# bits and leave the rest exactly as it found them, or changing "also mop" would
# silently reset how often the mop goes back to be washed.
#
# The low bits are also not the mode number: on a machine whose mop pad lifts —
# ours does, it washes and empties on its own — 0 and 2 are swapped, so the 0
# this robot reports IS "vacuum and mop in one pass".
PROP_CLEANING_MODE = (4, 23)
CLEANING_MODES: list[str] = ["sweeping", "mopping", "sweeping and mopping", "mopping after sweeping"]
_STORED_TO_MODE: dict[int, int] = {0: 2, 1: 1, 2: 0, 3: 3}
_MODE_TO_STORED: dict[int, int] = {mode: stored for stored, mode in _STORED_TO_MODE.items()}


def cleaning_mode_name(packed: object) -> str | None:
    """The packed reading → one of CLEANING_MODES, or None if it isn't an int."""
    if not isinstance(packed, int) or isinstance(packed, bool):
        return None
    return CLEANING_MODES[_STORED_TO_MODE[packed & 0x03]]


def cleaning_mode_packed(packed: int, option: str) -> int | None:
    """`packed` with its mode replaced by `option`, everything else untouched."""
    if option not in CLEANING_MODES:
        return None
    return (packed & ~0x03) | _MODE_TO_STORED[CLEANING_MODES.index(option)]
