"""Automation definition model — the typed Trigger → Condition → Action rule.

This is Layer 1 of the automation engine (PROPOSAL §6): a strict, validated,
deterministic rule shape. ~90% of home automations fit it, it's built visually
in the UI, and it's stored as data (jsonb) in Postgres — never YAML on disk.
Layer 2 (a Starlark sandbox for power logic) is additive on top of this.

Both the API (validates on write) and the automation service (decodes on load)
share this module, so a definition that the API accepted always decodes cleanly
in the engine. Validation is fail-loud: a malformed rule is rejected at the
boundary, exactly like a bad capability value.
"""

from __future__ import annotations

import math

import msgspec

from dida_core.capabilities import (
    CapabilityError,
    Value,
    validate_command_args,
    validate_state,
)

# Comparison operators allowed in a (leaf) condition.
OPERATORS: frozenset[str] = frozenset({"==", "!=", "<", "<=", ">", ">="})
# Boolean group kinds for nesting conditions (a flat list is implicitly AND-ed).
GROUP_KINDS: frozenset[str] = frozenset({"and", "or", "not"})


class Trigger(msgspec.Struct, frozen=True):
    """Fires when `entity_id`'s `capability` reports a new value. If `to` is
    set, fires only when the new value equals it (e.g. contact -> true);
    otherwise fires on any reported value (change/refresh).

    `for_seconds` adds a hold: the trigger fires only after the value has held
    (uninterrupted) for that many seconds — HA's `for:`. The single most-used
    primitive in real automations (motion off held 30 s -> lights off). The
    timer restarts on every qualifying report and is cancelled the moment the
    value moves away, so a flicker never fires it. None/0 = fire immediately."""

    entity_id: str
    capability: str
    to: Value | None = None
    for_seconds: float | None = None
    id: str = ""  # optional label; a Starlark script branches on it via event["trigger_id"]


class Condition(msgspec.Struct, frozen=True):
    """Either a LEAF — `entity_id`/`capability` compared (`op`) to `value`
    against current state — or a GROUP: `kind` in and/or/not over nested
    `conditions`. A rule's top-level condition list is implicitly AND-ed;
    groups nest OR/NOT inside that (e.g. weather Cold OR Freezing)."""

    entity_id: str = ""
    capability: str = ""
    op: str = ""
    value: Value | None = None
    kind: str = ""  # "" = leaf; "and" | "or" | "not" = group over `conditions`
    conditions: list[Condition] = []


class Action(msgspec.Struct, frozen=True):
    """A capability command published to the bus for the owning adapter.

    `delay_ms` is a pause BEFORE this action runs; with a list of actions the
    engine executes them in order, honouring each delay — that's how timed
    sequences (a gate's click-wait-click pulse, a light blink) are expressed."""

    entity_id: str
    capability: str
    command: str
    args: dict[str, Value] = {}
    delay_ms: float = 0


class AutomationDef(msgspec.Struct, frozen=True):
    """Either a typed rule (actions/conditions) OR a Starlark script (Layer 2).
    `script` non-empty selects Layer 2; the trigger(s) always say WHEN to run.

    Multi-trigger: `triggers` holds one OR MORE triggers — ANY of them fires the
    rule. A typed rule runs the SAME conditions+actions whichever fires (uniform);
    a Starlark rule reads `event["trigger_id"]` to branch per trigger (an ON/OFF
    pair in ONE rule)."""

    triggers: list[Trigger] = []     # one or more; any fires the rule
    actions: list[Action] = []
    conditions: list[Condition] = []
    script: str = ""
    cooldown_seconds: float | None = None


def validate_definition(raw: object) -> AutomationDef:
    """Decode + semantically validate an automation definition. Raises
    CapabilityError on any bad shape, unknown capability/command, or bad
    operator — the API turns that into a 400 and never stores it. (Starlark
    syntax is checked when the automation service compiles the script, surfaced
    via the rule's last_error.)"""
    try:
        defn = msgspec.convert(raw, AutomationDef)
    except msgspec.ValidationError as exc:
        raise CapabilityError(f"malformed automation: {exc}") from exc

    # One OR MORE triggers; any fires the rule. Each is validated the same way.
    trigs = defn.triggers
    if not trigs:
        raise CapabilityError("automation has no trigger")
    for trig in trigs:
        # A hold duration, if given, must be a FINITE non-negative number of seconds
        # (NaN slips through a bare `< 0` check and later kills asyncio.sleep).
        fs = trig.for_seconds
        if fs is not None and not (math.isfinite(fs) and fs >= 0):
            raise CapabilityError(f"trigger for_seconds must be a finite number >= 0, got {fs}")
        # An empty trigger entity_id = a rule that can never fire — reject loudly.
        if not trig.entity_id.strip():
            raise CapabilityError("trigger entity_id must not be empty")
        # Trigger capability must be real; a fixed `to` must be a valid value.
        if trig.to is not None:
            validate_state(trig.capability, trig.to)
        else:
            # Touch the spec so an unknown capability is rejected even without `to`.
            validate_state(trig.capability, _probe_value(trig.capability))

    # A cooldown silences REPEATS of a rule that keeps matching truthfully — a zone
    # people walk through reports every crossing, and one visit crosses it both ways.
    # It is the operator's "don't tell me twice", never a patch over a lying signal:
    # the mirrored state stays exact, only the firing is spaced.
    cd = defn.cooldown_seconds
    if cd is not None and not (math.isfinite(cd) and cd >= 0):
        raise CapabilityError(f"cooldown_seconds must be a finite number >= 0, got {cd}")

    if defn.script.strip():
        # Layer 2 (Starlark): the script owns the logic; typed actions/conditions
        # must not also be set, to keep the two layers cleanly separated.
        if defn.actions or defn.conditions:
            raise CapabilityError("starlark automation must not also have typed actions/conditions")
        return defn

    # Layer 1 (typed): require + validate actions and conditions.
    if not defn.actions:
        raise CapabilityError("automation has no actions")
    for cond in defn.conditions:
        _validate_condition(cond)
    for act in defn.actions:
        if not act.entity_id.strip():
            # An empty action target publishes to a subject no adapter owns —
            # a silently dead rule.
            raise CapabilityError("action entity_id must not be empty")
        if not (math.isfinite(act.delay_ms) and act.delay_ms >= 0):
            raise CapabilityError(f"action delay_ms must be a finite number >= 0, got {act.delay_ms}")
        validate_command_args(act.capability, act.command, dict(act.args))

    return defn


def _validate_condition(cond: Condition) -> None:
    """Recursively validate a leaf or and/or/not group (fail loud)."""
    if cond.kind == "":  # leaf
        if not cond.entity_id.strip():
            raise CapabilityError("leaf condition entity_id must not be empty")
        if cond.op not in OPERATORS:
            raise CapabilityError(f"condition: bad operator {cond.op!r}; allowed: {sorted(OPERATORS)}")
        if cond.value is None:
            raise CapabilityError("leaf condition requires a value")
        if cond.conditions:
            raise CapabilityError("leaf condition must not nest other conditions")
        validate_state(cond.capability, cond.value)
        if cond.op in ("<", "<=", ">", ">="):
            # Ordering only means anything for numbers — `compare()` returns False
            # for any string/bool ordering, so such a condition would validate clean
            # yet be permanently unsatisfiable. Reject it at the boundary (F13).
            from dida_core.capabilities import ValueType, _spec
            if _spec(cond.capability).value_type not in (ValueType.INT, ValueType.FLOAT):
                raise CapabilityError(
                    f"ordering operator {cond.op!r} needs a numeric capability; "
                    f"{cond.capability!r} is not numeric"
                )
        return
    if cond.kind not in GROUP_KINDS:
        raise CapabilityError(f"condition: bad kind {cond.kind!r}; leaf or one of {sorted(GROUP_KINDS)}")
    if not cond.conditions:
        raise CapabilityError(f"{cond.kind!r} condition group is empty")
    for sub in cond.conditions:
        _validate_condition(sub)


def eval_condition(cond: Condition, snapshot: dict[tuple[str, str], Value]) -> bool:
    """Evaluate a condition against a state snapshot. Only a DEFINITE true fires:
    an indeterminate result (a leaf whose entity has no value) is not a pass, so a
    rule is never armed on unknown data."""
    return _eval_tristate(cond, snapshot) is True


def _eval_tristate(cond: Condition, snapshot: dict[tuple[str, str], Value]) -> bool | None:
    """Three-valued (Kleene) evaluation: True / False / None(=unknown). A missing
    leaf value is UNKNOWN, not False — the difference matters under `not`, where the
    old two-valued logic turned an unknown into a pass (`not all([False]) == True`),
    so a guard like "heat IF NOT(window == open)" fired the moment the window sensor
    went silent. Unknown propagates by the standard Kleene rules: False dominates an
    AND, True dominates an OR, and NOT(unknown) stays unknown — so a group resting on
    a dead sensor resolves to don't-fire rather than a silent yes."""
    if cond.kind == "":  # leaf
        val = snapshot.get((cond.entity_id, cond.capability))
        if val is None or cond.value is None:
            return None  # no value to judge — unknown, never a silent pass
        return compare(cond.op, val, cond.value)
    subs = [_eval_tristate(c, snapshot) for c in cond.conditions]
    if cond.kind == "and":
        if any(s is False for s in subs):
            return False
        return None if any(s is None for s in subs) else True
    if cond.kind == "or":
        if any(s is True for s in subs):
            return True
        return None if any(s is None for s in subs) else False
    if cond.kind == "not":
        # NOT over the implicit-AND of the nested conditions.
        inner = True
        if any(s is False for s in subs):
            inner = False
        elif any(s is None for s in subs):
            inner = None
        return None if inner is None else not inner
    return False


def _probe_value(capability: str) -> Value:
    """A throwaway VALID value just to force capability existence + type checks in
    validate_state when a trigger has no fixed `to`. Uses `_spec` so an unknown
    capability raises CapabilityError (a clean 400), not a bare ValueError (a 500),
    and returns a value that satisfies any choices/json/pattern refinement."""
    from dida_core.capabilities import ValueType, _spec

    spec = _spec(capability)
    if spec.choices:
        return spec.choices[0]
    if spec.value_type is ValueType.BOOL:
        return True
    if spec.value_type is ValueType.STRING:
        if spec.is_json:
            return "[]"
        if spec.pattern is not None:
            # A pattern-refined string (e.g. color_rgb `#RRGGBB`) rejects "" — the
            # capability EXISTS (checked above via _spec), but the probe value must
            # satisfy the pattern or a `to=None` trigger on it fails validation
            # spuriously (F14). Synthesize a matching candidate.
            import re
            for cand in ("#000000", "0", "a", "x"):
                if re.fullmatch(spec.pattern, cand):
                    return cand
        return ""
    return spec.minimum if spec.minimum is not None else 0


def compare(op: str, a: Value, b: Value) -> bool:
    """Evaluate one condition. Equality works for every type; ordering only
    for numbers (a string/bool ordering compare is treated as not-met)."""
    if op in ("==", "!="):
        # Python's `1 == True` is True — but a rule comparing a numeric sensor to
        # a boolean (or vice versa) is a type mismatch, not a match. Treat bool
        # and number as distinct; numbers compare across int/float as usual.
        if isinstance(a, bool) is not isinstance(b, bool):
            return op == "!="
        return (a == b) if op == "==" else (a != b)
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int | float) or not isinstance(b, int | float):
        return False
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    return False


def trigger_matches(trigger: Trigger, entity_id: str, capability: str, value: Value) -> bool:
    if trigger.entity_id != entity_id or trigger.capability != capability:
        return False
    return trigger.to is None or trigger.to == value
