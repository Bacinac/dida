"""Harmony ↔ canonical-capability translation.

The hub is ONE `remote` device: a `source` picker (the activities — the only
real, readable state) plus a fixed cluster of momentary `press` buttons (D-pad /
transport / volume) exposed as sibling entities. Harmony has no global key map,
only a per-activity one: a press is routed, through the CURRENT activity's
control group, to whichever device owns that key.

Keeps the protocol-specific bits (entity ids, the canonical key set, the
activity/control-group parsing) out of the adapter's control flow.
"""

from __future__ import annotations

import json

NAMESPACE = "harmony"
HUB_ENTITY = f"{NAMESPACE}:hub"
POWER_OFF = "PowerOff"

# Canonical remote key set (essential + nav) — suffix -> (candidate Harmony
# function names in priority order, label). The button entity id is
# HUB_ENTITY + "_" + suffix; the UI's Remote widget lays them out spatially.
# Only the FIRST candidate present in the current activity is used, since hubs
# name the same key slightly differently across device profiles. Numbers /
# channel keys are deliberately omitted — with activity-based control you pick an
# activity then navigate; they add many tiny targets for near-zero use.
REMOTE_KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "up":        (("DirectionUp",), "Up"),
    "down":      (("DirectionDown",), "Down"),
    "left":      (("DirectionLeft",), "Left"),
    "right":     (("DirectionRight",), "Right"),
    "ok":        (("Select", "OK", "DPadSelect"), "Select"),
    "back":      (("Back", "Return", "Exit"), "Back"),
    "home":      (("Home",), "Home"),
    "menu":      (("Menu",), "Menu"),
    "play":      (("Play",), "Play"),
    "pause":     (("Pause",), "Pause"),
    "stop":      (("Stop",), "Stop"),
    "skip_back": (("SkipBackward", "PreviousTrack"), "Previous"),
    "skip_fwd":  (("SkipForward", "NextTrack"), "Next"),
    "vol_down":  (("VolumeDown",), "Volume down"),
    "vol_up":    (("VolumeUp",), "Volume up"),
    "mute":      (("Mute",), "Mute"),
}


def button_entity(suffix: str) -> str:
    """Sibling entity id for one remote key (e.g. 'ok' -> harmony:hub_ok)."""
    return f"{HUB_ENTITY}_{suffix}"


def activity_options(config: dict) -> tuple[dict[str, int], list[str]]:
    """(label -> activity-id map, ordered source options). PowerOff is the "off"
    state and leads the list; the activities follow in the hub's own order."""
    acts = (config or {}).get("activity", [])
    by_label = {a["label"]: a["id"] for a in acts}
    labels = [a["label"] for a in acts]
    options = ([POWER_OFF] if POWER_OFF in labels else []) + [x for x in labels if x != POWER_OFF]
    return by_label, options


def _function_devices(activity: dict) -> dict[str, str]:
    """function name -> deviceId across every control group of one activity. The
    deviceId lives in each function's `action` JSON blob."""
    out: dict[str, str] = {}
    for cg in activity.get("controlGroup", []):
        for f in cg.get("function", []):
            name = f.get("name")
            action = f.get("action")
            dev = None
            if isinstance(action, str):
                try:
                    dev = json.loads(action).get("deviceId")
                except (ValueError, TypeError):
                    dev = None
            if name and dev:
                out.setdefault(name, dev)
    return out


def build_button_map(config: dict, activity_id: object) -> dict[str, tuple[str, str]]:
    """suffix -> (deviceId, Harmony function name) for the given activity. A key
    absent from this activity is simply not in the map (its press is a no-op).
    Empty when there's no active activity (PowerOff)."""
    act = None
    for a in (config or {}).get("activity", []):
        if str(a.get("id")) == str(activity_id):
            act = a
            break
    if not act:
        return {}
    fn_dev = _function_devices(act)
    out: dict[str, tuple[str, str]] = {}
    for suffix, (candidates, _label) in REMOTE_KEYS.items():
        for cand in candidates:
            if cand in fn_dev:
                out[suffix] = (fn_dev[cand], cand)
                break
    return out
