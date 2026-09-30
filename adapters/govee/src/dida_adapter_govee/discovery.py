"""Home Assistant MQTT Discovery → canonical DIDA capability mapping.

A device/integration publishes a (usually retained) JSON config to
  <prefix>/<component>/[<node_id>/]<object_id>/config
describing one HA *entity* (its state_topic, command_topic, device_class, …).
Several HA entities belong to one physical *device* (via `device.identifiers`);
we group them so a multi-sensor becomes ONE DIDA entity with many capabilities —
exactly DIDA's model. We translate the components/device_classes we understand
into canonical capabilities and ignore the rest.

This module is pure mapping/parsing (no I/O). It's the ONE place HA's
conventions live; the engine stays protocol-blind. Unknown shapes are skipped,
loudly, not guessed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from dida_core import CapabilityKind

log = logging.getLogger("dida.adapter.govee.discovery")

NAMESPACE = "govee"

# HA binary_sensor device_class -> canonical boolean capability.
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

# HA sensor device_class -> canonical numeric capability.
_SENSOR_CLASS = {
    "temperature": CapabilityKind.TEMPERATURE,
    "humidity": CapabilityKind.HUMIDITY,
    "illuminance": CapabilityKind.ILLUMINANCE,
    "power": CapabilityKind.POWER,
    "energy": CapabilityKind.ENERGY,
    "battery": CapabilityKind.BATTERY,
}

# Pull the key out of the common `{{ value_json.key }}` / `{{ value_json['key'] }}`
# templates without a full Jinja engine. Anything fancier falls back to raw.
_VJSON_DOT = re.compile(r"value_json\.([A-Za-z_][A-Za-z0-9_]*)")
_VJSON_IDX = re.compile(r"value_json\[['\"]([^'\"]+)['\"]\]")


def _json_key(template: str | None) -> str | None:
    if not template:
        return None
    m = _VJSON_DOT.search(template) or _VJSON_IDX.search(template)
    return m.group(1) if m else None


# HA MQTT Discovery abbreviations (Tasmota and friends publish these instead of
# full keys). Only the subset relevant to the components we map.
_ABBREV = {
    "dev": "device", "ids": "identifiers", "o": "origin",
    "p": "platform", "cmps": "components",
    "stat_t": "state_topic", "cmd_t": "command_topic",
    "dev_cla": "device_class", "unit_of_meas": "unit_of_measurement",
    "val_tpl": "value_template", "stat_val_tpl": "state_value_template",
    "pl_on": "payload_on", "pl_off": "payload_off",
    "uniq_id": "unique_id",
    "bri_stat_t": "brightness_state_topic", "bri_cmd_t": "brightness_command_topic",
    "bri_scl": "brightness_scale", "bri_val_tpl": "brightness_value_template",
    "clr_temp_stat_t": "color_temp_state_topic", "clr_temp_cmd_t": "color_temp_command_topic",
    "clr_temp_val_tpl": "color_temp_value_template",
    "pos_t": "position_topic", "set_pos_t": "set_position_topic", "pos_tpl": "position_template",
    "pl_open": "payload_open", "pl_cls": "payload_close", "pl_stop": "payload_stop",
    "stat_open": "state_open", "stat_clsd": "state_closed",
    "pl_lock": "payload_lock", "pl_unlk": "payload_unlock",
}

# Topic-valued keys where a leading/embedded `~` expands to the config's base.
_TOPIC_KEYS = (
    "state_topic", "command_topic", "brightness_state_topic", "brightness_command_topic",
    "color_temp_state_topic", "color_temp_command_topic", "position_topic", "set_position_topic",
)


def expand_config(cfg: dict) -> dict:
    """De-abbreviate a discovery payload and resolve the `~` base-topic shortcut.
    Full-name payloads pass through unchanged."""
    out: dict = {}
    for k, v in cfg.items():
        out[_ABBREV.get(k, k)] = expand_config(v) if isinstance(v, dict) else v
    base = out.get("~")
    if base:
        for tk in _TOPIC_KEYS:
            val = out.get(tk)
            if isinstance(val, str) and "~" in val:
                out[tk] = val.replace("~", base)
    return out


@dataclass(slots=True)
class StateBinding:
    """How to turn an inbound MQTT payload on `topic` into a capability value."""

    capability: str
    topic: str
    json_key: str | None      # extract value_json[key]; else use the raw payload
    transform: str            # onoff | number | brightness | mireds | position | lock | cover
    unit: str | None = None
    payload_on: str = "ON"
    payload_off: str = "OFF"
    scale: float = 255.0      # brightness scale


@dataclass(slots=True)
class CommandSpec:
    """How to turn a DIDA command for `capability` into an MQTT publish."""

    capability: str
    topic: str
    kind: str                 # onoff | brightness | mireds | cover | position | lock
    payload_on: str = "ON"
    payload_off: str = "OFF"
    payload_stop: str = "STOP"
    scale: float = 255.0
    json_schema: bool = False  # light json schema: command is a JSON object


@dataclass(slots=True)
class Entity:
    entity_id: str
    name: str
    device_type: str | None = None
    states: list[StateBinding] = field(default_factory=list)
    commands: dict[str, CommandSpec] = field(default_factory=dict)


def _device_key(payload: dict, object_id: str) -> str:
    dev = payload.get("device") or {}
    ident = dev.get("identifiers")
    if isinstance(ident, list) and ident:
        ident = ident[0]
    key = ident or payload.get("unique_id") or object_id
    return f"{NAMESPACE}:{key}"


def _name(payload: dict, object_id: str) -> str:
    dev = payload.get("device") or {}
    return dev.get("name") or payload.get("name") or object_id


class Registry:
    """Accumulates discovery configs into DIDA entities and indices."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        # state_topic -> list of (entity_id, StateBinding)
        self.by_topic: dict[str, list[tuple[str, StateBinding]]] = {}

    def state_topics(self) -> set[str]:
        return set(self.by_topic)

    def add_config(self, component: str, object_id: str, payload: dict) -> tuple[Entity | None, bool]:
        """Ingest one discovery config. Returns (entity, changed) — `changed` is
        True only when a new entity/capability/command was actually added, so the
        caller can skip re-logging/re-saving identical re-announcements.
        Handles both classic component-based and newer device-based discovery,
        and de-abbreviated payloads."""
        if component == "device":
            return self._add_device_based(payload)
        return self._ingest(component, object_id, expand_config(payload))

    def _add_device_based(self, payload: dict) -> tuple[Entity | None, bool]:
        """homeassistant/device/<id>/config: one payload bundling many entities
        under `components`, sharing the device + `~` base topic."""
        top = expand_config(payload)
        base = payload.get("~")
        dev = top.get("device")
        last: Entity | None = None
        changed = False
        for oid, raw in (top.get("components") or {}).items():
            if not isinstance(raw, dict):
                continue
            cmp = dict(raw)
            if base is not None:
                cmp.setdefault("~", base)
            if dev is not None:
                cmp.setdefault("device", dev)
            cfg = expand_config(cmp)
            platform = cfg.get("platform")
            if not platform:
                continue
            ent, ch = self._ingest(platform, oid, cfg)
            last = ent or last
            changed = changed or ch
        return last, changed

    def _ingest(self, component: str, object_id: str, cfg: dict) -> tuple[Entity | None, bool]:
        states, commands = _parse(component, cfg)
        if not states and not commands:
            return None, False
        key = _device_key(cfg, object_id)
        ent = self.entities.get(key)
        changed = False
        if ent is None:
            ent = Entity(entity_id=key, name=_name(cfg, object_id), device_type=component)
            self.entities[key] = ent
            changed = True
        for sb in states:
            # First binding for a capability wins (avoid a 2nd sensor clobbering).
            if any(s.capability == sb.capability for s in ent.states):
                continue
            ent.states.append(sb)
            self.by_topic.setdefault(sb.topic, []).append((key, sb))
            changed = True
        for cs in commands:
            if cs.capability not in ent.commands:
                ent.commands[cs.capability] = cs
                changed = True
        return ent, changed

    def snapshot(self) -> dict:
        """Serialise the registry so it survives an adapter restart (discovery
        configs aren't retained on the broker — without this, a restart loses
        every device until a bridge re-announces)."""
        return {
            eid: {
                "entity_id": ent.entity_id,
                "name": ent.name,
                "device_type": ent.device_type,
                "states": [dataclasses.asdict(s) for s in ent.states],
                "commands": {k: dataclasses.asdict(v) for k, v in ent.commands.items()},
            }
            for eid, ent in self.entities.items()
        }

    def load(self, data: dict) -> None:
        # Rebuild from scratch. _restore_registry runs on EVERY HA reconnect against
        # this same long-lived Registry; without clearing, by_topic accumulates
        # duplicate (entity_id, binding) pairs and each inbound state is then
        # published N times (once per reconnect).
        self.entities.clear()
        self.by_topic.clear()
        for eid, e in data.items():
            ent = Entity(e["entity_id"], e["name"], e.get("device_type"))
            for s in e.get("states", []):
                sb = StateBinding(**s)
                ent.states.append(sb)
                self.by_topic.setdefault(sb.topic, []).append((eid, sb))
            for k, v in e.get("commands", {}).items():
                ent.commands[k] = CommandSpec(**v)
            self.entities[eid] = ent


def _parse(component: str, p: dict) -> tuple[list[StateBinding], list[CommandSpec]]:
    on = str(p.get("payload_on", "ON"))
    off = str(p.get("payload_off", "OFF"))

    if component == "switch":
        st, cmd = p.get("state_topic"), p.get("command_topic")
        states = [StateBinding(CapabilityKind.ON_OFF.value, st, _json_key(p.get("value_template")),
                               "onoff", None, str(p.get("state_on", on)), str(p.get("state_off", off)))] if st else []
        commands = [CommandSpec(CapabilityKind.ON_OFF.value, cmd, "onoff", on, off)] if cmd else []
        return states, commands

    if component == "binary_sensor":
        kind = _BINARY_CLASS.get(p.get("device_class"))
        st = p.get("state_topic")
        if not kind or not st:
            log.debug("ha discovery: skipping binary_sensor (class=%r, topic=%r)",
                      p.get("device_class"), st)
            return [], []
        return [StateBinding(kind.value, st, _json_key(p.get("value_template")), "onoff", None, on, off)], []

    if component == "sensor":
        kind = _SENSOR_CLASS.get(p.get("device_class"))
        st = p.get("state_topic")
        if not kind or not st:
            log.debug("ha discovery: skipping sensor (class=%r, topic=%r)",
                      p.get("device_class"), st)
            return [], []
        return [StateBinding(kind.value, st, _json_key(p.get("value_template")), "number",
                             p.get("unit_of_measurement"))], []

    if component == "lock":
        st, cmd = p.get("state_topic"), p.get("command_topic")
        states = [StateBinding(CapabilityKind.LOCK.value, st, _json_key(p.get("value_template")),
                               "lock", None, str(p.get("state_locked", "LOCKED")),
                               str(p.get("state_unlocked", "UNLOCKED")))] if st else []
        commands = [CommandSpec(CapabilityKind.LOCK.value, cmd, "lock",
                                str(p.get("payload_lock", "LOCK")), str(p.get("payload_unlock", "UNLOCK")))] if cmd else []
        return states, commands

    if component == "cover":
        states: list[StateBinding] = []
        commands: list[CommandSpec] = []
        pos_t, set_pos_t = p.get("position_topic"), p.get("set_position_topic")
        if pos_t:
            states.append(StateBinding(CapabilityKind.OPEN_CLOSE.value, pos_t,
                                       _json_key(p.get("position_template")), "position", "%"))
        elif p.get("state_topic"):
            states.append(StateBinding(CapabilityKind.OPEN_CLOSE.value, p["state_topic"], None, "cover", "%",
                                       str(p.get("state_open", "open")), str(p.get("state_closed", "closed"))))
        cmd = p.get("command_topic")
        if cmd or set_pos_t:
            commands.append(CommandSpec(CapabilityKind.OPEN_CLOSE.value, cmd or set_pos_t, "cover",
                                        str(p.get("payload_open", "OPEN")), str(p.get("payload_close", "CLOSE")),
                                        str(p.get("payload_stop", "STOP"))))
            if set_pos_t:
                commands.append(CommandSpec("__set_position__", set_pos_t, "position"))
        return states, commands

    if component == "light":
        return _parse_light(p)

    # The docstring promises unknown shapes are skipped LOUDLY — a Zigbee sensor
    # with an unmapped component otherwise disappears without a trace.
    log.info("ha discovery: unsupported component %r — skipped", component)
    return [], []


def _parse_light(p: dict) -> tuple[list[StateBinding], list[CommandSpec]]:
    on = str(p.get("payload_on", "ON"))
    off = str(p.get("payload_off", "OFF"))
    states: list[StateBinding] = []
    commands: list[CommandSpec] = []
    json_schema = p.get("schema") == "json"
    cmd = p.get("command_topic")
    bscale = float(p.get("brightness_scale", 255))

    if json_schema:
        st = p.get("state_topic")
        if st:
            states.append(StateBinding(CapabilityKind.ON_OFF.value, st, "state", "onoff", None, "ON", "OFF"))
            states.append(StateBinding(CapabilityKind.BRIGHTNESS.value, st, "brightness", "brightness", "%", scale=bscale))
            if p.get("color_temp", True):
                states.append(StateBinding(CapabilityKind.COLOR_TEMP.value, st, "color_temp", "mireds", "K"))
        if cmd:
            commands.append(CommandSpec(CapabilityKind.ON_OFF.value, cmd, "onoff", on, off, json_schema=True))
            commands.append(CommandSpec(CapabilityKind.BRIGHTNESS.value, cmd, "brightness", scale=bscale, json_schema=True))
            commands.append(CommandSpec(CapabilityKind.COLOR_TEMP.value, cmd, "mireds", json_schema=True))
        return states, commands

    # default schema: separate per-attribute topics
    if p.get("state_topic"):
        states.append(StateBinding(CapabilityKind.ON_OFF.value, p["state_topic"],
                                   _json_key(p.get("state_value_template")), "onoff", None, on, off))
    if p.get("brightness_state_topic"):
        states.append(StateBinding(CapabilityKind.BRIGHTNESS.value, p["brightness_state_topic"],
                                   _json_key(p.get("brightness_value_template")), "brightness", "%", scale=bscale))
    if p.get("color_temp_state_topic"):
        states.append(StateBinding(CapabilityKind.COLOR_TEMP.value, p["color_temp_state_topic"],
                                   _json_key(p.get("color_temp_value_template")), "mireds", "K"))
    if cmd:
        commands.append(CommandSpec(CapabilityKind.ON_OFF.value, cmd, "onoff", on, off))
    if p.get("brightness_command_topic"):
        commands.append(CommandSpec(CapabilityKind.BRIGHTNESS.value, p["brightness_command_topic"], "brightness", scale=bscale))
    if p.get("color_temp_command_topic"):
        commands.append(CommandSpec(CapabilityKind.COLOR_TEMP.value, p["color_temp_command_topic"], "mireds"))
    return states, commands


# --- decode / encode -------------------------------------------------------

Value = bool | int | float | str


def _binary(binding: StateBinding, raw: object, on: Value, off: Value) -> Value | None:
    s = str(raw)
    if s == binding.payload_on:
        return on
    if s == binding.payload_off:
        return off
    return None


def _mireds_to_kelvin(m: float) -> int | None:
    return max(1700, min(6535, round(1_000_000 / m))) if m > 0 else None


_NUMERIC: dict[str, Callable[[StateBinding, float], Value | None]] = {
    "number": lambda b, v: v,
    "brightness": lambda b, v: max(0, min(100, round(v / b.scale * 100))),
    "mireds": lambda b, v: _mireds_to_kelvin(v),
    "position": lambda b, v: max(0, min(100, round(v))),
}

# payload_on / payload_off carry state_open / state_closed for a cover.
_BINARY: dict[str, tuple[Value, Value]] = {"onoff": (True, False), "lock": (True, False), "cover": (100, 0)}


def decode(binding: StateBinding, raw_payload: bytes) -> Value | None:
    """Turn an inbound MQTT payload into a canonical capability value (or None
    to skip). Raises nothing — bad input is dropped by the caller's None check."""
    try:
        text = raw_payload.decode().strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    raw: object = text
    if binding.json_key is not None:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or binding.json_key not in obj:
            return None
        raw = obj[binding.json_key]

    t = binding.transform
    if t in _BINARY:
        return _binary(binding, raw, *_BINARY[t])
    if t in _NUMERIC:
        try:
            return _NUMERIC[t](binding, float(raw))
        except (TypeError, ValueError):
            return None
    return None


def encode(spec: CommandSpec, command: str, args: dict) -> tuple[str, str] | None:
    """Turn a DIDA command into (topic, payload). None if not expressible."""
    k = spec.kind
    if k == "onoff":
        if command == "turn_on":
            payload = _json_state(spec, True) if spec.json_schema else spec.payload_on
        elif command == "turn_off":
            payload = _json_state(spec, False) if spec.json_schema else spec.payload_off
        else:
            return None
        return spec.topic, payload
    if k == "lock":
        if command == "lock":
            return spec.topic, spec.payload_on
        if command == "unlock":
            return spec.topic, spec.payload_off
        return None
    if k == "brightness":
        v = int(args.get("value", 0))
        raw = round(v / 100 * spec.scale)
        if spec.json_schema:
            return spec.topic, json.dumps({"state": "ON", "brightness": raw})
        return spec.topic, str(raw)
    if k == "mireds":
        v = int(args.get("value", 0))
        m = round(1_000_000 / v) if v > 0 else 0
        if spec.json_schema:
            return spec.topic, json.dumps({"state": "ON", "color_temp": m})
        return spec.topic, str(m)
    if k == "cover":
        mapping = {"open": spec.payload_on, "close": spec.payload_off, "stop": spec.payload_stop}
        return (spec.topic, mapping[command]) if command in mapping else None
    if k == "position":
        return spec.topic, str(int(args.get("value", 0)))
    return None


def _json_state(spec: CommandSpec, on: bool) -> str:
    return json.dumps({"state": "ON" if on else "OFF"})
