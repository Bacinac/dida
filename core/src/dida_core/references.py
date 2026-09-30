"""Which entities a stored object REFERS to.

An `entity_id` is the identity of everything in DIDA, and it is referenced from
places the database cannot police: automation definitions, scene states, area
configs, schedule params, settings — JSON, with no foreign key anywhere except
`current_state`. So a reference can go dangling and nothing says a word. The
automation still loads, still evaluates, and simply never fires; the scene recalls
every device except one. That is the failure mode this project exists to avoid, and
it is currently the easiest one to hit: rename a device in zigbee2mqtt and its
entity_id changes underneath every rule that names it.

This module is the one place that knows how to read a reference OUT of a stored
object. It lives in core because two services must agree on it — the automation
engine executes these references and the api reports on them — which is exactly the
bar for belonging here.

It deliberately does NOT resolve or repair anything. Finding is a separate job from
fixing, and the finding is what turns a silent failure into a visible one.
"""

from __future__ import annotations

import json
import re

# Keys whose VALUE is an entity_id, wherever they appear in a stored structure.
# `entity` and `entity_id` are both in use: the typed automation model and the
# entity-picker config field settled on different names, and both are persisted.
ENTITY_KEYS = ("entity_id", "entity")

# Starlark functions whose FIRST argument is an entity_id. Kept here rather than in
# the automation service because the api scans scripts it does not execute, and two
# copies of this list would drift the moment one gained a function — the scan would
# then quietly stop covering it, which is worse than not scanning at all.
#
# `notify` is deliberately ABSENT: its first argument is a delivery target (a user,
# a topic), not an entity.
STARLARK_ENTITY_FUNCS = (
    "state", "command", "set_state",
    "turn_on", "turn_off", "toggle",
    "set_brightness", "set_color_temp", "set_color",
    "set_position", "open", "close", "stop",
    "lock", "unlock",
)

# `func("entity:id"` — the first argument only, single or double quoted. A call
# whose entity is computed (`turn_on(target)`) is invisible to any static scan;
# that is a known limit, stated rather than papered over, and it is why the
# detector reports what it FOUND rather than claiming a clean bill of health.
_CALL = re.compile(
    r"\b(?:" + "|".join(STARLARK_ENTITY_FUNCS) + r")\s*\(\s*(['\"])(?P<eid>[^'\"]+)\1"
)

# An entity_id is `<adapter>:<rest>`; anything without a colon is a capability
# name, a command, or free text that happens to sit under one of these keys.
_LOOKS_LIKE_EID = re.compile(r"^[a-z0-9_-]+:[^\s]+$")


def _walk(obj, found: set[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ENTITY_KEYS:
                if isinstance(value, str) and _LOOKS_LIKE_EID.match(value):
                    found.add(value)
                elif isinstance(value, list):
                    # A picker that allows several targets stores a list.
                    found.update(v for v in value
                                 if isinstance(v, str) and _LOOKS_LIKE_EID.match(v))
            _walk(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, found)


def entity_references(obj) -> set[str]:
    """Every entity_id a stored structure refers to, at any depth.

    Handles the typed automation model (triggers/conditions/actions), scene states
    and schedule params with one traversal, because they all name the entity the
    same way. A structure carrying a Starlark script is also scanned as
    one — an automation is routinely both."""
    found: set[str] = set()
    _walk(obj, found)
    if isinstance(obj, dict) and isinstance(obj.get("script"), str):
        found |= starlark_references(obj["script"])
    return found


def starlark_references(script: str) -> set[str]:
    """Entity ids named as literals in a Starlark automation body."""
    return {m.group("eid") for m in _CALL.finditer(script or "")}


def _rewrite(obj, mapping: dict[str, str]):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in ENTITY_KEYS:
                if isinstance(value, str):
                    out[key] = mapping.get(value, value)
                    continue
                if isinstance(value, list):
                    out[key] = [mapping.get(v, v) if isinstance(v, str) else _rewrite(v, mapping)
                               for v in value]
                    continue
            out[key] = _rewrite(value, mapping)
        if isinstance(out.get("script"), str):
            out["script"] = rewrite_starlark(out["script"], mapping)
        return out
    if isinstance(obj, list):
        return [_rewrite(item, mapping) for item in obj]
    return obj


def rewrite_references(obj, mapping: dict[str, str]):
    """The same structure with every reference in `mapping` replaced.

    The write counterpart of `entity_references`, kept beside it so the two cannot
    disagree about where a reference lives — a rewriter that misses a location the
    finder knows about would report a rename as complete while leaving a rule dead,
    which is the original bug wearing a different hat.

    Returns a NEW structure; the input is not modified."""
    return _rewrite(obj, mapping)


def rewrite_starlark(script: str, mapping: dict[str, str]) -> str:
    """Replace entity literals in a Starlark body, and ONLY those.

    Not a text substitution: an entity id can legitimately appear inside a string
    that is not an entity argument (a notify message naming a device, a comment),
    and rewriting those would corrupt the script's meaning while looking correct.
    Only the first argument of a call in STARLARK_ENTITY_FUNCS is touched."""
    def sub(m: re.Match[str]) -> str:
        eid = m.group("eid")
        new = mapping.get(eid)
        if new is None:
            return m.group(0)
        return m.group(0)[: -(len(eid) + 1)] + new + m.group(1)

    return _CALL.sub(sub, script or "")


# Tables whose JSON names entities under ENTITY_KEYS at any depth, or in a Starlark
# body. Every one of them has `id` and `name` columns.
WALKED_COLUMNS = (
    ("automations", "definition"),
    ("scenes", "states"),
    ("schedules", "params"),
    ("computed_helpers", "definition"),
)

# Everywhere else an entity id is stored, as paths into the stored value: `.name` a
# field, `[]` every item of a list, `{}` every key of an object, and the empty path
# the value itself. A trailing `@` marks reading keys, `<entity_id>:<capability>`.
# These structures name entities under their own field names, so the walk above
# would pass them by — and did: a rename left room configs pointing at the old id.
AREA_REFERENCES: dict[str, tuple[str, ...]] = {
    "heating_config": (".sensor", ".valves[]"),
    "media_config": (".sources[].remote", ".sources[].avr", ".sources[].nowplaying",
                     ".sources[].player", ".sources[].zone"),
    "sensor_config": (".excluded[]@", ".included[]@"),
}

# EVERY app_settings key, with the places its value names an entity (none: `()`).
# The core writers refuse a key missing here, so a new setting cannot hold an id a
# rename would leave behind without someone deciding it does not.
SETTING_REFERENCES: dict[str, tuple[str, ...]] = {
    "alert_recipients": ("[]",),
    "announce_lang": (),
    "anthropic_api_key": (),
    "app_url": (),
    "backup_schedule": (),
    "camera_order": ("[]",),
    "camera_spans": ("{}",),
    "contacts_last_sync": (),
    "contacts_sync_requested": (),
    "discovery_subnets": (),
    "energy_config": ("{}",),
    "entry_controls": (".car", ".pedestrian", ".door", ".state_car", ".state_pedestrian",
                       ".state_door", ".state_door_fallback"),
    "fcm_service_account": (),
    "heating": (".boiler", ".outdoor", ".away_helper"),
    "lan_ip": (),
    "lan_status": (),
    "managed_vlans": (),
    "net_parent": (),
    "openai_api_key": (),
    "opus_token": (),
    "opus_url": (),
    "owntracks_secret": (),
    "panel_config": (".presence_entity",),
    "panel_seen_at": (),
    "panel_token": (),
    "presence_hidden": ("[]",),
    "public_url": (),
    "radio_current_id": (),
    "radio_player": ("",),
    "smartthings_locations": (),
    "vlan_config": (),
    "vlan_status": (),
    "volume_presets": (),
    "webpush_contact": (),
    "webpush_vapid_private": (),
    "webpush_vapid_public": (),
}

_STEP = re.compile(r"\.[a-z_]+|\[\]|\{\}|@")


def _steps(path: str) -> list[str]:
    return _STEP.findall(path)


def _found(value, steps: list[str], out: set[str]) -> None:
    if not steps:
        if isinstance(value, str) and value:
            out.add(value)
        return
    step, rest = steps[0], steps[1:]
    if step == "@":
        if isinstance(value, str) and value.count(":") >= 2:
            out.add(value.rsplit(":", 1)[0])
    elif step == "[]":
        for item in value if isinstance(value, list) else ():
            _found(item, rest, out)
    elif step == "{}":
        for key in value if isinstance(value, dict) else ():
            _found(key, rest, out)
    elif isinstance(value, dict):
        _found(value.get(step[1:]), rest, out)


def _rewritten(value, steps: list[str], mapping: dict[str, str]):
    if not steps:
        return mapping.get(value, value) if isinstance(value, str) else value
    step, rest = steps[0], steps[1:]
    if step == "@":
        if not (isinstance(value, str) and value.count(":") >= 2):
            return value
        eid, cap = value.rsplit(":", 1)
        return f"{mapping.get(eid, eid)}:{cap}"
    if step == "[]":
        return [_rewritten(v, rest, mapping) for v in value] if isinstance(value, list) else value
    if step == "{}":
        if not isinstance(value, dict):
            return value
        return {_rewritten(k, rest, mapping): v for k, v in value.items()}
    if not isinstance(value, dict) or step[1:] not in value:
        return value
    return {**value, step[1:]: _rewritten(value[step[1:]], rest, mapping)}


def path_references(value, paths: tuple[str, ...]) -> set[str]:
    """Entity ids at `paths` inside a stored value."""
    found: set[str] = set()
    for path in paths:
        _found(value, _steps(path), found)
    return found


def rewrite_paths(value, paths: tuple[str, ...], mapping: dict[str, str]):
    """The value with every id at `paths` in `mapping` replaced; a new structure."""
    for path in paths:
        value = _rewritten(value, _steps(path), mapping)
    return value


def _decoded(raw: str):
    # Most settings are JSON; a few are stored as bare text (`radio_player`).
    try:
        return json.loads(raw), True
    except (TypeError, ValueError):
        return raw, False


def setting_references(key: str, raw: str | None) -> set[str]:
    paths = SETTING_REFERENCES.get(key, ())
    if not paths or not raw:
        return set()
    return path_references(_decoded(raw)[0], paths)


def rewrite_setting(key: str, raw: str, mapping: dict[str, str]) -> str:
    value, is_json = _decoded(raw)
    new = rewrite_paths(value, SETTING_REFERENCES.get(key, ()), mapping)
    return json.dumps(new) if is_json else new
