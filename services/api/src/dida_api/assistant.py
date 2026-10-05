"""DIDA assistant — natural-language home control via Claude tool-use.

Hybrid model strategy:
  * claude-sonnet-5 drives the conversation + tool loop (fast, cheap control and
    queries) on adaptive thinking at low effort.
  * claude-opus-4-8 is a sub-call inside the create_automation tool, where it
    synthesises a typed automation definition from a natural-language spec.

The turn is a STREAM (`assistant_events`); `run_assistant` is a drain of the same
generator, so the tool loop exists once.

Everything the assistant does goes through the SAME validated path as the REST
API: commands are built by prepare_command and published to the bus;
automations are checked with validate_definition before they're stored. The LLM
cannot bypass the capability model — a bad command is rejected at the boundary,
exactly like any other caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from anthropic import AsyncAnthropic
from dida_core import (
    CHOICE_CAPABILITIES,
    DEVICE_TYPES,
    Bus,
    CapabilityError,
    command_vocabulary,
    house_timezone,
    local_str,
    readable_capabilities,
    validate_definition,
)
from fastapi import HTTPException

from dida_api.auth import AuthUser
from dida_api.commands import dispatch_command
from dida_api.help_docs import HELP_TOPIC_NAMES, help_text
from dida_api.visibility import hidden_entity_ids, is_hidden

log = logging.getLogger("dida.api.assistant")

CONTROL_MODEL = "claude-sonnet-5"     # conversation + tool loop
AUTHOR_MODEL = "claude-opus-4-8"      # automation synthesis (harder reasoning)
# The tool loop runs on ADAPTIVE thinking: with thinking off, Sonnet 5 reaches for
# tools noticeably less, and an assistant that answers "the light is on" from memory
# instead of calling get_state is worse than a slow one. Effort stays low — this is
# lookup-and-act, not reasoning. The one-shot jobs below (explain, translate) keep
# thinking DISABLED explicitly: they are deterministic, and on Sonnet 5 an OMITTED
# thinking field means adaptive, which would eat into their max_tokens.
CONTROL_THINKING: dict[str, str] = {"type": "adaptive"}
CONTROL_EFFORT: dict[str, str] = {"effort": "low"}
ONE_SHOT_THINKING: dict[str, str] = {"type": "disabled"}
MAX_TOOL_ROUNDS = 8
# Control-channel subjects owned by services/automation. Defined here because
# dida_api.automations already imports from this module, so this is the one
# direction that carries no cycle — CHECK_SUBJECT is the sandbox compile+dry-run,
# RUN_SUBJECT the force-run request. Must match the automation service.
CHECK_SUBJECT = "dida.automation.check"
RUN_SUBJECT = "dida.automation.run"


def _render(prompt: str) -> str:
    """Substitute the generated capability blocks into a prompt.

    Sentinels, not f-strings or .format(): these prompts are dense with literal
    JSON and Starlark braces ({"value": …}, {entity_id, capability, value}), every
    one of which would have to be doubled — and a single missed pair is either an
    import-time NameError or, worse, a silently mangled prompt.
    """
    return prompt.replace("@VOCABULARY@", command_vocabulary()).replace(
        "@READ_ONLY@", readable_capabilities()
    )


_SYSTEM = _render("""You are the DIDA assistant — you control a real smart home through tools.

Rules:
- Answer briefly and clearly.
- Every timestamp a tool gives you is ALREADY the household's local time. Repeat it as
  written; never convert it and never append a zone.
- PLAIN TEXT ONLY. The UI prints your reply verbatim and renders no markdown, so
  asterisks and hashes reach the user as literal characters. No **bold**, no *italics*,
  no # headings, no `backticks`, no bullet or numbered lists. To enumerate, write short
  sentences or separate lines — never "1." or "- " prefixes.
- The user refers to rooms and devices by human names ("the kitchen light",
  "downstairs"). FIRST call list_entities (and get_state if needed) to find the
  exact entity_ids and their capabilities, and only then act. It returns each device's
  room, floor and type — use those filters for "the lights", "upstairs", "in the
  kitchen" rather than guessing from names or capabilities.
- ALWAYS give list_entities a filter. A bare call returns only a directory, on purpose:
  this house has hundreds of devices, and dragging all of them into the conversation
  makes every later step of the turn slower. Narrow by device_type / room / floor
  first, and use the directory to pick the narrowest filter that can answer.
- NEVER call get_state once per device. It filters — by capability, device_type, room,
  or a list of entity_ids — so a question about many devices is ONE call:
  "which lights are on" is get_state(capability="on_off", device_type="light").
  A dozen single-entity calls is a dozen round trips and is the slowest thing you can do.
- Read before you report: never state a device's state from memory or from earlier in
  the conversation — call get_state.
- NEVER name a tool, capability or entity_id to the user. They are internal. Say "I can
  start the mower — shall I?", never "I will run send_command / the mower capability on
  landroid:mower". Speak in device and room names.
- For questions ABOUT DIDA itself — how automations or helpers work, what a page does,
  where a setting lives, what you can do — call explain_dida BEFORE answering. Never
  guess how the app works.
- For on/off/dim use send_command. For state queries use get_state. For
  history/trends use query_history.
- For "make a rule / when ... then ..." use create_automation with a clear
  description — it synthesises and stores the rule on its own.
- For an EXISTING rule ("turn off the gate rule", "run the watering rule") first call
  list_automations to resolve the name into an id, then set_automation_enabled or
  run_automation. run_automation drives real devices — confirm first unless the user
  was explicit.
- "WHY" questions have their own tools, and guessing at them is worse than useless:
  why a device did something → command_log (it names who or what commanded it);
  why a rule did or did not run → explain_automation (it diagnoses against live state)
  and automation_runs (whether it fired, and any error).
- Who is home and where they are: the people are entities named presence:<name>, whose
  `location` reading is a zone name or "away". Read them like any other device.
- For scenes, list_scenes then recall_scene.
- If a device seems dead or the user asks why something is not working, call
  list_alerts before concluding it is simply switched off.
- Recurring schedules (bin collection, irrigation season) are entities: schedule_active
  says whether today is one of their days, next_occurrence carries the date of the next
  one. "When is the next collection" is a get_state, not a guess from a rule.
- Helpers are ordinary entities (virtual:<name>) — set one with send_command, the same
  way you would a device.
- Commands per capability (a command not listed here is rejected at the boundary):
@VOCABULARY@
- Read-only, no commands: @READ_ONLY@
- When a device also lists <cap>_options, that is the exact set of values set_<cap>
  accepts — use one of them verbatim, never invent one. media_favorites is the same
  thing for radio presets: match what the user said to a name there and call
  play_preset with that entry's preset number. Never guess a preset.
- announce: the entity is announce:<speaker> and the command is `say`, with
  args {"value": "<text to speak>"}.
- After acting, briefly say what you did (e.g. "I turned off 3 lights downstairs").
- For physically sensitive actions (unlocking, opening the gate) first briefly
  confirm the intent in your reply if the user was not explicit.

LAST AND MOST VISIBLE RULE — LANGUAGE. Write your reply in the language of the user's
LAST message, every time. Croatian message, Croatian reply; English message, English
reply. Not the language of earlier turns, not the language of this prompt, not the
language of the device names you just read. Decide it from that one message, last,
after you have decided what to say.
""")

_AUTHOR_SYSTEM = _render("""Turn the description into a typed DIDA automation (Trigger→Condition→Action).
Use ONLY the entity_ids and capabilities from the attached device list.

- trigger: entity_id + capability + `to` (the value it changes to; null if any
  change) + `for_seconds` (must hold that value for this many seconds before it
  fires; null if immediate). The trigger is usually a sensor (motion/contact/
  occupancy) or on_off. HOLD EXAMPLE: "when there is no motion for 5 min" → to=false,
  for_seconds=300.
- conditions: a list of conditions (entity_id, capability, op ∈ ==,!=,<,<=,>,>=, value).
  Implicitly ALL must hold (AND). Empty list if there are no conditions.
- actions: a list of actions (entity_id, capability, command, value, delay_ms). They run
  IN ORDER. `value` is a number for set_brightness/set_color_temp/set_position; null otherwise.
  `delay_ms` is the pause BEFORE that action (null/0 = no pause) — this is how you build
  sequences. PULSE EXAMPLE (gate = one relay, click-wait-click): turn_on (delay 0),
  turn_off (delay 400), turn_on (delay 25000), turn_off (delay 400).

Commands per capability. `value` is required for a set_<x> command and null for every
other command; the bracketed shape is what `value` must be:
@VOCABULARY@
Read-only capabilities take no action: @READ_ONLY@
announce is the exception to the null rule: `say` DOES take a value — the text to speak —
and its entity is announce:<speaker>, e.g. announce:kitchen.
Where the device list shows <cap>_options, use one of those values verbatim for set_<cap>.
""")

# Structured-output schema for the Opus automation synthesis. `value` lives at the
# top of each action (instead of a free-form args object) because structured
# outputs reject `additionalProperties` other than false. We map value→args after.
_AUTODEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["trigger", "conditions", "actions"],
    "properties": {
        "trigger": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entity_id", "capability", "to", "for_seconds"],
            "properties": {
                "entity_id": {"type": "string"},
                "capability": {"type": "string"},
                "to": {"type": ["boolean", "number", "string", "null"]},
                "for_seconds": {"type": ["number", "null"]},
            },
        },
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entity_id", "capability", "op", "value"],
                "properties": {
                    "entity_id": {"type": "string"},
                    "capability": {"type": "string"},
                    "op": {"type": "string", "enum": ["==", "!=", "<", "<=", ">", ">="]},
                    "value": {"type": ["boolean", "number", "string"]},
                },
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entity_id", "capability", "command", "value", "delay_ms"],
                "properties": {
                    "entity_id": {"type": "string"},
                    "capability": {"type": "string"},
                    "command": {"type": "string"},
                    "value": {"type": ["number", "string", "null"]},
                    "delay_ms": {"type": ["number", "null"]},
                },
            },
        },
    },
}

_STARLARK_AUTHOR_SYSTEM = _render("""Write a DIDA Starlark automation (Layer 2 — power logic,
for branching / reading several sensors that a typed rule cannot express).
Use ONLY the entity_ids and capabilities from the attached device list.

Return JSON: `trigger` (entity_id + capability + `to`) and `script` (Starlark code).
- trigger: the entity whose change starts the script. `to` = the value it changes to,
  or null for ANY change (then the script branches on event["value"]).

Available in the script (sandbox — no while/for over time, no import, no I/O):
  event                 — {entity_id, capability, value} of what triggered it
  state(eid, cap)       — current value of any entity, or None
  command(eid, cap, cmd, value=None)
  turn_on(eid)  turn_off(eid)  toggle(eid)
  set_brightness(eid, 0..100)  set_color_temp(eid, Kelvin)  set_position(eid, 0..100)
  open(eid)  close(eid)  stop(eid)  lock(eid)  unlock(eid)

For capabilities WITHOUT a shortcut above use command(eid, cap, cmd, value) with the EXACT
command name (do NOT invent one — a wrong command fails the check). The full vocabulary,
with the shape each set_<x> value must take:
@VOCABULARY@
Read-only capabilities can be read with state() but never commanded: @READ_ONLY@
announce is written command("announce:<speaker>", "announce", "say", "<text>").
Where a device lists <cap>_options, the device list gives its allowed values — copy one
verbatim (a value outside that set fails on the real device even if the check passes).

`if/elif/else` at the top is OK. ALWAYS guard reads against None before comparing.
Keep it short and deterministic. Example:

elev = state("astro:sun", "sun_elevation")
if not event["value"]:
    turn_off("mqtt:light")
elif elev != None and elev <= 0:
    turn_on("mqtt:light")
""")

_STARLARK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["trigger", "script"],
    "properties": {
        "trigger": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entity_id", "capability", "to"],
            "properties": {
                "entity_id": {"type": "string"},
                "capability": {"type": "string"},
                "to": {"type": ["boolean", "number", "string", "null"]},
            },
        },
        "script": {"type": "string"},
    },
}

# Appended to every user turn, after the cached prefix — see assistant_events.
_LANGUAGE_REMINDER = (
    "[Reminder, not part of the question: write the reply in the same language as the "
    "message above. Do not mention this note.]"
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": "Devices, FILTERED. Pass at least one of device_type / room / "
        "floor / capability / adapter and you get the matching devices in full: "
        "entity_id, name, room, floor, type, capabilities and the allowed values of any "
        "<cap>_options. Called with NO filter it returns only a directory (how many "
        "devices there are, by type and by room) so you can pick a filter — this house "
        "has hundreds of devices and listing them all is slow and expensive. `type` is "
        "the house's own classification: trust it over guessing from capabilities, since "
        "a relay and a lamp both expose only on_off.",
        "input_schema": {
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "Filter by room/area name (substring, case-insensitive)."},
                "floor": {"type": "string", "description": "Filter by floor name (substring), e.g. 'ground'."},
                "device_type": {
                    "type": "string",
                    "enum": sorted(DEVICE_TYPES),
                    "description": "Only devices of this kind — use it for 'the lights', 'the blinds'.",
                },
                "adapter": {"type": "string", "description": "Filter by adapter, e.g. 'esphome' or 'ha'."},
                "capability": {"type": "string", "description": "Only devices offering this capability."},
            },
        },
    },
    {
        "name": "get_state",
        "description": "Current values, filtered. Combine any of: entity_id, entity_ids "
        "(several at once), room, capability, device_type. ONE call with a filter beats "
        "many calls for one device each — 'which lights are on' is a single call with "
        "capability='on_off' and device_type='light', not a call per lamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Several entities in one call."},
                "room": {"type": "string", "description": "Filter by room/area name (substring)."},
                "capability": {"type": "string",
                               "description": "Only this reading, e.g. 'on_off' or 'temperature'."},
                "device_type": {"type": "string", "enum": sorted(DEVICE_TYPES)},
            },
        },
    },
    {
        "name": "send_command",
        "description": "Act on a device. Validated against the capability model before it reaches the device.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "capability": {"type": "string"},
                "command": {"type": "string"},
                "args": {"type": "object", "description": "e.g. {\"value\": 50} for set_brightness."},
            },
            "required": ["entity_id", "capability", "command"],
        },
    },
    {
        "name": "create_automation",
        "description": "Create a typed automation from a natural-language description. "
        "A separate model synthesises and validates the rule. Provide a clear name and description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string", "description": "Plain-language spec: when X happens (+ conditions) do Y."},
            },
            "required": ["name", "description"],
        },
    },
    {
        "name": "query_history",
        "description": "Time-series history for one entity_id + capability over the last N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "capability": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
            },
            "required": ["entity_id", "capability"],
        },
    },
    {
        "name": "list_automations",
        "description": "The existing rules: id, name, whether each is enabled, when it last "
        "fired and its last error. Call this before enabling, disabling or running one, to "
        "resolve the name the user said into an id. Administrators only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_automation_enabled",
        "description": "Enable or disable an existing rule. Re-enabling also clears a rule "
        "whose repeated failures had tripped it. Administrators only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            "required": ["automation_id", "enabled"],
        },
    },
    {
        "name": "run_automation",
        "description": "Run a rule's actions NOW, skipping its trigger and conditions. This "
        "DRIVES REAL DEVICES — running the gate rule opens the gate — so confirm with the "
        "user before calling it unless they were explicit. Administrators only.",
        "input_schema": {
            "type": "object",
            "properties": {"automation_id": {"type": "integer"}},
            "required": ["automation_id"],
        },
    },
    {
        "name": "explain_automation",
        "description": "Explain one rule in plain language AND diagnose it against the "
        "live state: would it fire right now, and if not, which condition is blocking "
        "it. Use it for 'how does X work', 'why doesn't X run', 'what does X do'. "
        "Administrators only.",
        "input_schema": {
            "type": "object",
            "properties": {"automation_id": {"type": "integer"}},
            "required": ["automation_id"],
        },
    },
    {
        "name": "automation_runs",
        "description": "When rules actually fired, and which failed — newest first. "
        "Omit automation_id for the whole house. Use it for 'did the watering run this "
        "morning', 'has anything been failing'. Administrators only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "command_log",
        "description": "WHO commanded a device, and when — a person, a named automation, "
        "this assistant, or a voice assistant. State says what happened; this says who "
        "asked. It is the only way to answer 'why did the light come on at 3am' or 'who "
        "opened the gate'. Omit entity_id for everything recent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
                "limit": {"type": "integer", "default": 40},
            },
        },
    },
    {
        "name": "list_alerts",
        "description": "System alerts firing right now — an adapter that stopped "
        "reporting, a device gone offline, a disk filling up. Use it for 'is anything "
        "wrong', 'why is X not working', or before concluding a device is simply off.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_scenes",
        "description": "The saved scenes: id and name. Use it to resolve a scene the user "
        "named before recalling it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recall_scene",
        "description": "Apply a scene — re-issues the device states it captured. Devices the "
        "user may not see or control are skipped; the result reports how many were applied "
        "and how many skipped.",
        "input_schema": {
            "type": "object",
            "properties": {"scene_id": {"type": "integer"}},
            "required": ["scene_id"],
        },
    },
    {
        "name": "explain_dida",
        "description": "How DIDA itself works. Call this whenever the user asks about the "
        "application rather than about a device — how automations, helpers, scenes, areas, "
        "zones, users, permissions, adapters, history, media, cameras, notifications or "
        "voice assistants work, what a page is for, where a setting lives, or what you as "
        "the assistant can do. Call it before answering such a question; do not answer "
        "from memory. Omit the topic to get the topic list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": list(HELP_TOPIC_NAMES),
                    "description": "Which topic to explain. Omit for the list of topics.",
                },
            },
        },
    },
]


@dataclass
class AssistantCtx:
    client: AsyncAnthropic
    pool: asyncpg.Pool
    bus: Bus
    ch: Any | None
    # The authenticated caller. The assistant must NOT be a permission bypass: it
    # drives the same bus/DB as the REST API, so every tool enforces the same
    # per-user boundary (control rules, view-hiding, admin-only writes). None is
    # admin-equivalent only for internal/editor callers that never expose tools.
    user: AuthUser | None = None
    # The live alert evaluator (app.state.alert_evaluator). None when it isn't
    # running — the tool then says so rather than reporting "no alerts", which is
    # the same sentence a broken watchdog would produce.
    alert_evaluator: Any | None = None
    actions: list[dict] = field(default_factory=list)

    @property
    def _restricted(self) -> bool:
        return self.user is not None and self.user.role != "admin"


async def _fetch_options(pool: asyncpg.Pool) -> dict[str, dict[str, Any]]:
    """entity_id -> {"source_options": [...], ...} from current_state.

    `<x>_options` is a CAPABILITY, so listing an entity's capabilities only ever
    said the name — the allowed values live in current_state as a JSON blob and
    never reached the model, which then invented source names that the device
    rejected. Both catalogues below carry the real values now.
    """
    rows = await pool.fetch(
        "SELECT entity_id, capability, value FROM current_state WHERE capability = ANY($1)",
        list(CHOICE_CAPABILITIES),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw = r["value"]
        if not isinstance(raw, str) or not raw:
            continue  # "" is the wire convention for cleared metadata
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue  # a malformed blob must not take the whole catalogue down
        out.setdefault(r["entity_id"], {})[r["capability"]] = _trim_choices(parsed)
    return out


def _trim_choices(parsed: Any) -> Any:
    """Keep what identifies a choice, drop what only costs tokens.

    A radio favourite carries a stream url and artwork alongside its preset number
    and name; the model needs the number to call play_preset and the name to match
    what the user said. Anything not shaped like that passes through untouched.
    """
    if not isinstance(parsed, list):
        return parsed
    trimmed = []
    for item in parsed:
        if isinstance(item, dict) and "name" in item:
            keep = {k: item[k] for k in ("preset", "name", "index", "id") if k in item}
            trimmed.append(keep or item)
        else:
            trimmed.append(item)
    return trimmed


async def _list_entities(ctx: AssistantCtx, room: str | None = None,
                         adapter: str | None = None, capability: str | None = None,
                         device_type: str | None = None, floor: str | None = None) -> Any:
    rows = await ctx.pool.fetch(
        "SELECT e.entity_id, e.name, e.adapter, e.capabilities, e.device_type, "
        "       COALESCE(a.name, a.kind) AS room, "
        # An entity's own floor placement wins; otherwise inherit its area's.
        "       COALESCE(f2.name, f1.name) AS floor "
        "FROM entities e "
        "LEFT JOIN areas a ON a.id = e.area_id "
        "LEFT JOIN floors f1 ON f1.key = a.fp_floor "
        "LEFT JOIN floors f2 ON f2.key = e.fp_floor "
        # Diagnostics (uptime, rssi, firmware) are noise to a person asking about
        # the house — the rule synthesiser already excludes them; this did not.
        "WHERE e.diagnostic = false "
        "ORDER BY e.entity_id"
    )
    hidden = await hidden_entity_ids(ctx.pool, ctx.user) if ctx._restricted else set()
    options = await _fetch_options(ctx.pool)
    out = []
    for r in rows:
        if r["entity_id"] in hidden:
            continue  # never reveal an entity the user isn't allowed to see
        if adapter and r["adapter"] != adapter:
            continue
        if room and (r["room"] or "").lower().find(room.lower()) < 0:
            continue
        if floor and (r["floor"] or "").lower().find(floor.lower()) < 0:
            continue
        if device_type and r["device_type"] != device_type:
            continue
        caps = r["capabilities"] or []
        if capability and capability not in caps:
            continue
        item = {"entity_id": r["entity_id"], "name": r["name"], "room": r["room"],
                "floor": r["floor"], "type": r["device_type"], "capabilities": caps}
        opts = options.get(r["entity_id"])
        if opts:
            item["options"] = opts  # allowed values for the matching set_<x>
        out.append(item)

    # An UNFILTERED call answers with a directory, not the whole house. Measured on
    # the real registry: 433 entities cost ~38k tokens, and because a tool result is
    # resent on every subsequent round of the loop, one careless call dominated the
    # latency and the bill of the entire turn. Trimming fields does not save it
    # (~27k even stripped bare) — the count is the cost. So the model gets what it
    # needs to CHOOSE a filter instead. Nothing is silently dropped: the totals are
    # exact and the reply says how to get the rest.
    if not any((room, adapter, capability, device_type, floor)):
        by_type: dict[str, int] = {}
        by_room: dict[str, int] = {}
        for e in out:
            by_type[e["type"] or "other"] = by_type.get(e["type"] or "other", 0) + 1
            if e["room"]:
                by_room[e["room"]] = by_room.get(e["room"], 0) + 1
        return {
            "total": len(out),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "by_room": dict(sorted(by_room.items(), key=lambda kv: -kv[1])),
            "note": "Directory only. Call again with device_type, room, floor, "
                    "capability or adapter to get the matching devices in full.",
        }
    return out


async def _get_state(ctx: AssistantCtx, entity_id: str | None = None,
                     entity_ids: list[str] | None = None, room: str | None = None,
                     capability: str | None = None, device_type: str | None = None) -> list[dict]:
    """Current values, filtered by any combination of entity / room / capability /
    type.

    The capability and entity_ids filters exist because of what the loop did without
    them: asked "which lights are on", the model could either pull every reading in
    the house or call this once PER DEVICE — and it chose the latter, eight to
    fourteen times, each one a full round of the tool loop that re-sends the whole
    conversation. `capability="on_off"` answers the same question in one call.
    """
    rows = await ctx.pool.fetch(
        "SELECT s.entity_id, s.capability, s.value, s.unit "
        "FROM current_state s "
        "JOIN entities e ON e.entity_id = s.entity_id "
        "LEFT JOIN areas a ON a.id = e.area_id "
        "WHERE e.diagnostic = false "
        "  AND ($1::text   IS NULL OR s.entity_id = $1) "
        "  AND ($2::text[] IS NULL OR s.entity_id = ANY($2)) "
        "  AND ($3::text   IS NULL OR lower(COALESCE(a.name, a.kind)) LIKE '%' || lower($3) || '%') "
        "  AND ($4::text   IS NULL OR s.capability = $4) "
        "  AND ($5::text   IS NULL OR e.device_type = $5) "
        "ORDER BY s.entity_id, s.capability LIMIT 500",
        entity_id, entity_ids or None, room, capability, device_type,
    )
    if ctx._restricted:
        hidden = await hidden_entity_ids(ctx.pool, ctx.user)
        return [dict(r) for r in rows if r["entity_id"] not in hidden]
    return [dict(r) for r in rows]


async def _send_command(ctx: AssistantCtx, entity_id: str, capability: str,
                       command: str, args: dict | None = None) -> dict:
    try:
        await dispatch_command(ctx.pool, ctx.bus, ctx.user, entity_id, capability, command, args,
                               source=f"assistant:{ctx.user.username}" if ctx.user else "assistant")
    except HTTPException as exc:
        raise CapabilityError("unknown device" if exc.status_code == 404 else str(exc.detail)) from exc
    ctx.actions.append({"type": "command", "entity_id": entity_id, "capability": capability, "command": command})
    return {"ok": True, "entity_id": entity_id, "command": command}


async def synthesize_definition(
    client: AsyncAnthropic, pool: asyncpg.Pool, description: str,
    bus: Bus | None = None, mode: str = "typed",
) -> dict[str, Any]:
    """NL spec → a validated automation definition (NOT saved). mode='typed' →
    Trigger→Condition→Action; mode='starlark' → a Layer-2 script, sandbox-checked
    (compile + dry-run + command validation) before it's returned. Shared by the
    assistant's create_automation tool and the editor's AI draft endpoint, so both
    produce the same canonical, capability-validated shape."""
    rows = await pool.fetch(
        "SELECT e.entity_id, e.name, COALESCE(ar.name, ar.kind) AS room, e.capabilities "
        "FROM entities e LEFT JOIN areas ar ON ar.id = e.area_id "
        "WHERE e.diagnostic = false ORDER BY e.entity_id"
    )
    # Both author prompts tell the model to take values from <cap>_options; without
    # this join the catalogue carried only the option capability's NAME, so that
    # instruction pointed at data the model never got and it fell back to guessing.
    options = await _fetch_options(pool)
    catalog = "\n".join(
        f"- {r['entity_id']} ({r['name']}, soba: {r['room']}) "
        f"caps: {', '.join(r['capabilities'] or [])}"
        + "".join(
            f" | {cap} = {json.dumps(values, ensure_ascii=False)}"
            for cap, values in sorted(options.get(r["entity_id"], {}).items())
        )
        for r in rows
    )
    if mode == "starlark":
        return await _synthesize_starlark(client, bus, catalog, description)

    # Self-correct on a rejected definition, exactly as the Starlark path does. The
    # boundary's errors are machine-actionable ("sun_state: 'above_horizon' not one
    # of ('night', 'dawn', 'day', 'dusk')"), so handing one back is far more useful
    # than making the user rephrase a rule the model got 95% right.
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": f"Devices:\n{catalog}\n\nRule description:\n{description}"}
    ]
    last_err = "unknown error"
    for _ in range(3):  # first attempt + two self-corrections
        resp = await client.messages.create(
            model=AUTHOR_MODEL,
            max_tokens=2048,
            system=_AUTHOR_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _AUTODEF_SCHEMA}},
            messages=msgs,
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        raw = json.loads(text)
        trig = {"entity_id": raw["trigger"]["entity_id"], "capability": raw["trigger"]["capability"]}
        if raw["trigger"].get("to") is not None:
            trig["to"] = raw["trigger"]["to"]
        if raw["trigger"].get("for_seconds"):
            trig["for_seconds"] = raw["trigger"]["for_seconds"]
        definition: dict[str, Any] = {
            "triggers": [trig],
            "conditions": raw.get("conditions", []),
            "actions": [
                {
                    "entity_id": a["entity_id"],
                    "capability": a["capability"],
                    "command": a["command"],
                    **({"args": {"value": a["value"]}} if a.get("value") is not None else {}),
                    **({"delay_ms": a["delay_ms"]} if a.get("delay_ms") else {}),
                }
                for a in raw.get("actions", [])
            ],
        }
        try:
            validate_definition(definition)
            return definition
        except CapabilityError as exc:
            last_err = str(exc)
            msgs += [
                {"role": "assistant", "content": text},
                {"role": "user",
                 "content": f"That definition is rejected by the boundary:\n{last_err}\n"
                            "Fix it and return the same JSON format."},
            ]
    raise CapabilityError(f"the rule does not pass validation: {last_err}")


async def _check_script(bus: Bus | None, script: str, trig: dict) -> dict:
    """Ask the automation service (owns the sandbox) to compile + dry-run the
    script and validate the commands it would emit. Returns {ok, error?, commands?}."""
    if bus is None:  # no bus wired (shouldn't happen for the editor path)
        return {"ok": True}
    payload = json.dumps({"script": script, "trigger": trig}).encode()
    try:
        reply = await bus.nc.request(CHECK_SUBJECT, payload, timeout=6.0)
        return json.loads(reply.data)
    except Exception as exc:
        log.warning("assistant: automation check not answered", exc_info=True)
        return {"ok": False, "error": f"automation servis ne odgovara: {exc}"}


async def _synthesize_starlark(
    client: AsyncAnthropic, bus: Bus | None, catalog: str, description: str
) -> dict[str, Any]:
    """Generate a Starlark rule, sandbox-check it, and self-correct once on a
    failing check (the error is fed back to the model). Same boundary a typed
    rule gets — only here we validate the BEHAVIOUR (emitted commands), not a
    static definition. Raises CapabilityError if it still doesn't pass."""
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": f"Devices:\n{catalog}\n\nRule description:\n{description}"}
    ]
    last_err = "unknown error"
    for _ in range(3):  # first attempt + two self-corrections
        resp = await client.messages.create(
            model=AUTHOR_MODEL,
            max_tokens=2048,
            system=_STARLARK_AUTHOR_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _STARLARK_SCHEMA}},
            messages=msgs,
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        raw = json.loads(text)
        trig: dict[str, Any] = {
            "entity_id": raw["trigger"]["entity_id"], "capability": raw["trigger"]["capability"],
        }
        if raw["trigger"].get("to") is not None:
            trig["to"] = raw["trigger"]["to"]
        definition = {"triggers": [trig], "conditions": [], "actions": [], "script": raw["script"]}
        validate_definition(definition)  # trigger must be a valid entity/capability
        chk = await _check_script(bus, raw["script"], trig)
        if chk.get("ok"):
            return definition
        last_err = chk.get("error", "unknown error")
        # Feed the failing check back and let the model fix its own script.
        msgs += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"The script fails the check:\n{last_err}\nFix it and return the same JSON format."},
        ]
    raise CapabilityError(f"the Starlark script does not pass the check: {last_err}")


_EXPLAIN_SYSTEM = """Explain this DIDA automation to the homeowner, plainly.
Format (no markdown, short sentences):
1) In one sentence: what the rule does.
2) DIAGNOSIS: whether it's enabled; when it last fired (if ever); and based on the
   attached CURRENT STATE — whether it would fire now, or which condition/trigger is
   currently preventing it. If there's a last error, explain it in lay terms.
Be concrete and use the human device names from the context, not entity_ids.
Every timestamp you are given is ALREADY the household's local time — repeat it as
written and never convert it."""


def _condition_entity_ids(conditions: list[dict]) -> list[str]:
    """Collect leaf entity_ids from conditions (flat leaves + nested and/or/not)."""
    out: list[str] = []
    for c in conditions or []:
        if c.get("kind"):
            out.extend(_condition_entity_ids(c.get("conditions", [])))
        elif c.get("entity_id"):
            out.append(c["entity_id"])
    return out


async def explain_automation(client: AsyncAnthropic, pool: asyncpg.Pool, automation_id: int) -> str:
    """Plain-language explanation + 'would it fire now / why not' diagnosis. Reads
    the rule plus the live state of the entities it depends on, so the answer is
    grounded in the real current state, not just the rule text."""
    rule = await pool.fetchrow(
        "SELECT name, enabled, definition, last_triggered_at, last_error FROM automations WHERE id = $1",
        automation_id,
    )
    if rule is None:
        raise CapabilityError("automation not found")
    defn = rule["definition"]
    if isinstance(defn, str):
        defn = json.loads(defn)
    # Entities the rule observes (trigger + condition leaves) → fetch live state +
    # friendly names so the diagnosis can reason about "would it fire now".
    eids = [defn.get("trigger", {}).get("entity_id"), *_condition_entity_ids(defn.get("conditions", []))]
    eids = sorted({e for e in eids if e})
    rows = await pool.fetch(
        "SELECT cs.entity_id, COALESCE(e.name, cs.entity_id) AS name, cs.capability, cs.value "
        "FROM current_state cs LEFT JOIN entities e ON e.entity_id = cs.entity_id "
        "WHERE cs.entity_id = ANY($1) ORDER BY cs.entity_id, cs.capability",
        eids,
    )
    state_lines = "\n".join(f"- {r['name']} ({r['entity_id']}) {r['capability']} = {r['value']}" for r in rows) or "(no readings)"
    # Local, not UTC. This sentence is read by the homeowner ("last fired at…"), and
    # the stores are UTC — two hours apart here for half the year.
    tz = await house_timezone(pool)
    last = local_str(rule["last_triggered_at"], tz) or "nikad"
    ctx_txt = (
        f"Name: {rule['name']}\nEnabled: {rule['enabled']}\n"
        f"Last trigger: {last}\nLast error: {rule['last_error'] or 'none'}\n\n"
        f"Definicija (JSON):\n{json.dumps(defn, ensure_ascii=False)}\n\n"
        f"CURRENT STATE of the observed devices:\n{state_lines}"
    )
    resp = await client.messages.create(
        model=CONTROL_MODEL, max_tokens=512, system=_EXPLAIN_SYSTEM,
        thinking=ONE_SHOT_THINKING,  # must be explicit: omitting it means adaptive on
        messages=[{"role": "user", "content": ctx_txt}],  # Sonnet 5, which would eat the 512
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def _create_automation(ctx: AssistantCtx, name: str, description: str) -> dict:
    # Creating an automation is an admin-only capability everywhere else (the REST
    # /automations POST requires admin); the assistant must not be a side door.
    if ctx._restricted:
        raise CapabilityError("only an administrator can create automations")
    definition = await synthesize_definition(ctx.client, ctx.pool, description)
    row = await ctx.pool.fetchrow(
        "INSERT INTO automations (name, enabled, definition) VALUES ($1, true, $2::jsonb) "
        "RETURNING id, name, definition",
        name, definition,
    )
    ctx.actions.append({"type": "automation", "id": row["id"], "name": row["name"]})
    return {"ok": True, "id": row["id"], "name": row["name"], "definition": definition}


def _require_admin(ctx: AssistantCtx, what: str) -> None:
    """Automations are admin-only over REST; this door is no different."""
    if ctx._restricted:
        raise CapabilityError(f"only an administrator can {what}")


async def _list_automations(ctx: AssistantCtx) -> list[dict]:
    _require_admin(ctx, "see the automations")
    tz = await house_timezone(ctx.pool)
    rows = await ctx.pool.fetch(
        "SELECT id, name, enabled, last_triggered_at, last_error FROM automations ORDER BY name"
    )
    return [
        {
            "id": r["id"], "name": r["name"], "enabled": r["enabled"],
            "last_triggered_at": local_str(r["last_triggered_at"], tz) or None,
            "last_error": r["last_error"],
        }
        for r in rows
    ]


async def _set_automation_enabled(ctx: AssistantCtx, automation_id: int, enabled: bool) -> dict:
    _require_admin(ctx, "enable or disable an automation")
    # Deferred: dida_api.automations imports this module for the LLM authoring
    # functions, so importing it at module level would cycle. LLM layer -> domain
    # layer is the natural direction; only the route reaching back up makes it one.
    from dida_api.automations import set_enabled

    row = await set_enabled(ctx.pool, automation_id, enabled)
    ctx.actions.append({"type": "automation_enabled", "id": row["id"],
                        "name": row["name"], "enabled": row["enabled"]})
    return {"ok": True, "id": row["id"], "name": row["name"], "enabled": row["enabled"]}


async def _run_automation(ctx: AssistantCtx, automation_id: int) -> dict:
    _require_admin(ctx, "run an automation")
    from dida_api.automations import run_now  # see _set_automation_enabled

    name = await ctx.pool.fetchval("SELECT name FROM automations WHERE id = $1", automation_id)
    await run_now(ctx.pool, ctx.bus, automation_id)
    ctx.actions.append({"type": "automation_run", "id": automation_id, "name": name})
    return {"ok": True, "id": automation_id, "name": name}


async def _explain_rule(ctx: AssistantCtx, automation_id: int) -> dict:
    """Plain-language explanation + a 'would it fire now, and if not what is
    blocking it' diagnosis. The capability already existed for the rule editor's
    Explain button; the assistant had no way to reach it, so "why doesn't the
    watering rule run" got a guess instead of the answer."""
    _require_admin(ctx, "inspect an automation")
    return {"explanation": await explain_automation(ctx.client, ctx.pool, automation_id)}


async def _automation_runs(ctx: AssistantCtx, automation_id: int | None = None,
                           limit: int = 20) -> list[dict]:
    """Did it fire, when, and did it fail — the record the Automations page shows."""
    _require_admin(ctx, "see automation history")
    tz = await house_timezone(ctx.pool)
    limit = max(1, min(int(limit), 100))
    if automation_id is None:
        rows = await ctx.pool.fetch(
            "SELECT automation_id, name, outcome, detail, fired_at FROM automation_runs "
            "ORDER BY fired_at DESC LIMIT $1", limit)
    else:
        rows = await ctx.pool.fetch(
            "SELECT automation_id, name, outcome, detail, fired_at FROM automation_runs "
            "WHERE automation_id = $1 ORDER BY fired_at DESC LIMIT $2", automation_id, limit)
    return [
        {"automation_id": r["automation_id"], "name": r["name"], "outcome": r["outcome"],
         "detail": r["detail"], "fired_at": local_str(r["fired_at"], tz)}
        for r in rows
    ]


async def _command_log(ctx: AssistantCtx, entity_id: str | None = None,
                       hours: int = 24, limit: int = 40) -> Any:
    """WHO told a device to do what, and when.

    States say what happened; this says who asked — a user, an automation (by
    name), this assistant, or a voice assistant. It is the only thing that answers
    "why did the light come on at 3am", which no amount of current state can.
    """
    _require_admin(ctx, "see command history")
    if ctx.ch is None:
        return {"error": "history store unavailable"}
    tz = await house_timezone(ctx.pool)
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(1, min(int(limit), 200))
    where = "ts >= now() - toIntervalHour({h:UInt32})"
    params: dict[str, Any] = {"h": hours, "n": limit}
    if entity_id:
        where += " AND entity_id = {e:String}"
        params["e"] = entity_id
    res = await ctx.ch.query(
        f"SELECT ts, entity_id, capability, command, source FROM command_history "  # noqa: S608
        f"WHERE {where} ORDER BY ts DESC LIMIT {{n:UInt32}}",
        parameters=params,
    )
    rows = [
        {"ts": local_str(ts, tz), "entity_id": eid, "capability": cap,
         "command": cmd, "source": src}
        for ts, eid, cap, cmd, src in res.result_rows
    ]
    return rows


async def _list_alerts(ctx: AssistantCtx) -> dict:
    """Currently-firing system alerts, from the same live evaluator /system/alerts
    reads. Read-only ops data, open to any signed-in user exactly as that route is.
    History is deliberately left out — the question here is "is anything wrong now",
    and a hundred past rows would be prompt cost with no answer in it."""
    ev = ctx.alert_evaluator
    if ev is None:
        return {"error": "the alert evaluator is not running"}
    return {"active": ev.active_list()}


async def _list_scenes(ctx: AssistantCtx) -> list[dict]:
    rows = await ctx.pool.fetch("SELECT id, name FROM scenes ORDER BY name")
    return [{"id": r["id"], "name": r["name"]} for r in rows]


async def _recall_scene(ctx: AssistantCtx, scene_id: int) -> dict:
    """Recall through the shared apply_scene, which runs the same per-entity
    boundary as the REST route — a scene is never a permission-escalation path."""
    from dida_api.scenes import apply_scene

    if ctx.user is None:
        # apply_scene resolves the per-entity boundary against a real user. The tool
        # layer is only reachable from the authenticated endpoint, so a missing user
        # is a wiring bug — fail loud rather than recall with no boundary at all.
        raise CapabilityError("scene recall needs an authenticated user")
    name = await ctx.pool.fetchval("SELECT name FROM scenes WHERE id = $1", scene_id)
    result = await apply_scene(
        ctx.pool, ctx.bus, ctx.user, scene_id,
        source=f"assistant:{ctx.user.username}:scene:{scene_id}",
    )
    ctx.actions.append({"type": "scene", "id": scene_id, "name": name,
                        "applied": result["applied"]})
    return {"name": name, **result}


async def _query_history(ctx: AssistantCtx, entity_id: str, capability: str, hours: int = 24) -> Any:
    if ctx.ch is None:
        return {"error": "history store unavailable"}
    # Same view boundary as GET /history and the other tools: a restricted user
    # must not read the history of an entity hidden from them (don't leak it exists).
    if ctx._restricted and await is_hidden(ctx.pool, ctx.user, entity_id):
        return {"error": "unknown device"}
    hours = max(1, min(int(hours), 24 * 30))
    res = await ctx.ch.query(
        "SELECT toUnixTimestamp64Milli(ts) AS ms, value_num, value_str FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND ts >= now() - toIntervalHour({h:UInt32}) ORDER BY ts LIMIT 2000",
        parameters={"e": entity_id, "c": capability, "h": hours},
    )
    pts = [{"ts": int(ms), "value": vn if vn is not None else vs} for ms, vn, vs in res.result_rows]
    return {"points": pts, "count": len(pts)}


_TOOL_HANDLERS = {
    "list_entities": _list_entities,
    "get_state": _get_state,
    "send_command": _send_command,
    "create_automation": _create_automation,
    "query_history": _query_history,
    "list_automations": _list_automations,
    "set_automation_enabled": _set_automation_enabled,
    "run_automation": _run_automation,
    "explain_automation": _explain_rule,
    "automation_runs": _automation_runs,
    "command_log": _command_log,
    "list_alerts": _list_alerts,
    "list_scenes": _list_scenes,
    "recall_scene": _recall_scene,
}


async def _execute_tool(ctx: AssistantCtx, name: str, tool_input: dict) -> str:
    handler = _TOOL_HANDLERS.get(name)
    try:
        if name == "explain_dida":
            # Static text — no DB, no permission surface: the same documentation
            # every user is entitled to. Returned raw, not JSON-wrapped.
            return help_text(tool_input.get("topic"))
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"})
        return json.dumps(await handler(ctx, **tool_input), default=str)
    except CapabilityError as exc:
        return json.dumps({"error": f"rejected (capability): {exc}"})
    except Exception as exc:
        log.exception("assistant tool %s failed", name)
        return json.dumps({"error": str(exc)})


async def assistant_events(
    ctx: AssistantCtx, history: list[dict], user_message: str
) -> AsyncIterator[dict]:
    """One assistant turn as a stream of events — THE tool loop; `run_assistant`
    below is just a drain of it, so there is one implementation, not two.

    Yields `{"type": "tool", "name": …}` as each tool is dispatched and exactly one
    terminal `{"type": "done", "reply": …, "actions": [...]}`. The tool events exist
    because the slow part of a turn is the rounds, not the prose: a question that
    touches three tools can sit for tens of seconds, and a spinner that says nothing
    is indistinguishable from a hang.
    """
    messages: list[dict] = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    # The language rule also rides ADJACENT to the message, not only in the system
    # prompt. In the prompt alone — even as its last and loudest line — it was still
    # ignored on roughly one English question in eight, because it competes with ~20
    # other rules and 4k tokens of vocabulary. Here it is the last thing read before
    # answering. It sits in `messages`, which render AFTER the cached prefix, so it
    # costs the cache nothing; and it is attached to this turn only, so it never
    # accumulates in the history the client replays.
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": user_message},
            {"type": "text", "text": _LANGUAGE_REMINDER},
        ],
    })

    final = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await ctx.client.messages.create(
            model=CONTROL_MODEL,
            # Headroom: adaptive thinking spends from the same budget as the reply,
            # and Sonnet 5's tokenizer counts a given text higher than 4.6's did.
            max_tokens=8192,
            thinking=CONTROL_THINKING,
            output_config=CONTROL_EFFORT,
            # Cache the tools+system prefix. Render order is tools → system →
            # messages, so a breakpoint on the last system block covers both: 4.2k
            # tokens that were re-sent in full on EVERY round of every turn. It is
            # byte-identical for every user and every turn — the prompt carries no
            # locale or user name — so the whole household shares one entry.
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            final = "I can't help with that request."
            break
        if resp.stop_reason != "tool_use":
            final = "".join(b.text for b in resp.content if b.type == "text")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                yield {"type": "tool", "name": block.name}
                out = await _execute_tool(ctx, block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})
    else:
        final = "I stopped — too many steps. Try again more precisely."

    yield {"type": "done", "reply": final or "(empty answer)", "actions": ctx.actions}


async def run_assistant(
    ctx: AssistantCtx, history: list[dict], user_message: str
) -> dict:
    """Run one turn and return only the result — the non-streaming caller's view."""
    done: dict = {"reply": "(empty answer)", "actions": ctx.actions}
    async for event in assistant_events(ctx, history, user_message):
        if event["type"] == "done":
            done = {"reply": event["reply"], "actions": event["actions"]}
    return done
