"""Layer 2 — Starlark sandbox for power automations (PROPOSAL §6).

Starlark is a deterministic Python subset (from Bazel): no I/O, no `while`, no
unbounded recursion. A user's script therefore *structurally* cannot hang or
crash the engine — the worst case is a bounded-but-slow `for`, which the
service caps with a thread + timeout and the per-rule circuit breaker. The
script can't touch the network, filesystem, or clock; it only sees the values
we inject and the commands it emits come back to us for validation.

The user writes plain statements (if/for/locals allowed). We parse with a dialect
that permits top-level statements, so the script runs AS WRITTEN — we deliberately
do NOT text-wrap it in a function (that old trick re-indented every physical line
and silently injected leading spaces into multi-line \"\"\"...\"\"\" string values).

Injected into every script:
  event   dict: {"entity_id","capability","value","trigger_id"} (the firing trigger;
          `trigger_id` lets a multi-trigger script branch, e.g. ON vs OFF)
  state(entity, capability)  -> current value or None (from a pre-fetched snapshot)
  command(entity, capability, command, value=None)   emit a command; a dict `value`
          is the full arg map (multi-arg commands), a scalar is the single `value` arg
  turn_on / turn_off / toggle (entity)               sugar over command()
  set_brightness / set_color_temp / set_position / set_color (entity, value)
  notify(target, title, message, camera)   open / close / stop (entity)   lock / unlock (entity)
  set_state(entity, capability, value)   write a derived entity's state directly

`set_state` is how DIDA expresses HA template sensors: a script triggered by
its inputs computes a value and writes it to a derived entity (e.g. a "night"
boolean, a weather-bucket enum). Unlike command(), it publishes STATE, not a
command — validated at the engine boundary like any device update.
"""

from __future__ import annotations

import starlark
from dida_core.capabilities import Value

# A parse/compile failure raises this — the service records it as last_error.
StarlarkError = starlark.StarlarkError

# Standard Starlark (no `while`, no unbounded recursion) MINUS load() so a script
# still can't reach the filesystem, PLUS top-level statements so bare if/for parse.
# Reused across parses; the worker process is single-threaded so the shared object
# is safe. NOTE: dialect attrs are write-only, hence the imperative construction.
_DIALECT = starlark.Dialect.standard()
_DIALECT.enable_top_level_stmt = True
_DIALECT.enable_load = False  # no I/O — the script only sees injected values

# What one run may hand back. A loop quick enough to finish inside the time cap could
# otherwise return a hundred thousand commands, each checked against the database
# and sent to a device; past the cap the run fails and counts toward the breaker.
MAX_EMITS = 200


def compile_script(script: str) -> starlark.AstModule:
    """Parse the user's script exactly as written (string literals byte-preserved).
    Raises starlark.StarlarkError on a syntax error."""
    return starlark.parse("automation.star", script or "pass", _DIALECT)


def run(
    ast: starlark.AstModule,
    event: dict,
    snapshot: dict[tuple[str, str], Value],
) -> tuple[list[tuple[str, str, str, dict[str, Value]]], list[tuple[str, str, Value]]]:
    """Execute a compiled script. Returns (commands, states):
      commands: (entity_id, capability, command, args) tuples
      states:   (entity_id, capability, value) tuples written via set_state()
    Validation + publishing happen in the caller (fail loud before any publish)."""
    emitted: list[tuple[str, str, str, dict[str, Value]]] = []
    emitted_states: list[tuple[str, str, Value]] = []

    def room() -> None:
        if len(emitted) + len(emitted_states) >= MAX_EMITS:
            raise ValueError(f"a run may emit at most {MAX_EMITS} commands and states")

    def emit(entity_id: str, capability: str, command: str, value=None) -> None:
        # A dict `value` IS the arg map (multi-arg commands like notify's
        # title+message); a scalar is the single `value` arg; None = no args.
        if isinstance(value, dict):
            args: dict[str, Value] = {str(k): v for k, v in value.items()}
        elif value is None:
            args = {}
        else:
            args = {"value": value}
        room()
        emitted.append((str(entity_id), str(capability), str(command), args))

    def set_state(entity_id: str, capability: str, value) -> None:
        room()
        emitted_states.append((str(entity_id), str(capability), value))

    def state(entity_id: str, capability: str):
        return snapshot.get((str(entity_id), str(capability)))

    module = starlark.Module()
    module["event"] = event
    module.add_callable("state", state)
    module.add_callable("command", emit)
    module.add_callable("set_state", set_state)
    # Sugar — thin wrappers over emit(), the same set the typed layer exposes.
    module.add_callable("turn_on", lambda e: emit(e, "on_off", "turn_on"))
    module.add_callable("turn_off", lambda e: emit(e, "on_off", "turn_off"))
    module.add_callable("toggle", lambda e: emit(e, "on_off", "toggle"))
    module.add_callable("set_brightness", lambda e, v: emit(e, "brightness", "set_brightness", v))
    module.add_callable("set_color_temp", lambda e, v: emit(e, "color_temp", "set_color_temp", v))
    module.add_callable("set_position", lambda e, v: emit(e, "open_close", "set_position", v))
    module.add_callable("open", lambda e: emit(e, "open_close", "open"))
    module.add_callable("close", lambda e: emit(e, "open_close", "close"))
    module.add_callable("stop", lambda e: emit(e, "open_close", "stop"))
    module.add_callable("lock", lambda e: emit(e, "lock", "lock"))
    module.add_callable("unlock", lambda e: emit(e, "lock", "unlock"))
    module.add_callable("set_color", lambda e, v: emit(e, "color_rgb", "set_color", v))
    # `camera` is optional and names a camera entity (or any of its children — a
    # zone, the bell): the push then carries that camera's frame.
    module.add_callable("notify", lambda target, title, message="", camera="": emit(
        target, "notify", "notify",
        {"title": title, "message": message, **({"camera": camera} if camera else {})}))

    starlark.eval(module, ast, starlark.Globals.standard())
    return emitted, emitted_states


def run_value(ast: starlark.AstModule, snapshot: dict[tuple[str, str], Value]) -> Value:
    """Evaluate a COMPUTED-HELPER definition and return the single value it computes.

    A computed helper is a PURE derivation of state: the script reads `state(entity,
    capability)` and assigns its answer to `value`. This runtime gives it `state` and
    NOTHING ELSE — no `command`, no `set_state`, no `turn_on`/…: a helper structurally
    CANNOT act on a device, so it can never be part of a feedback loop, which is the
    whole reason helpers are a layer BELOW automations. The engine takes the returned
    value and publishes it as the helper's state; the script itself emits nothing.
    """
    def state(entity_id: str, capability: str):
        return snapshot.get((str(entity_id), str(capability)))

    module = starlark.Module()
    module.add_callable("state", state)
    starlark.eval(module, ast, starlark.Globals.standard())
    value = module["value"]  # the script's `value = …` assignment (None if never set)
    if value is None:
        # An unset global reads back as None, and None is never a valid helper value
        # anyway (the capability boundary rejects it) — so both "forgot to assign" and
        # "assigned None" are the same error, reported the same clear way.
        raise StarlarkError("a computed helper must assign a non-None `value = …`")
    return value
