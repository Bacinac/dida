from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass

import asyncpg
import msgspec
from dida_core import (
    CORE,
    AutomationDef,
    Bus,
    CapabilityError,
    Command,
    Condition,
    EntityInfo,
    StateUpdate,
    apply_migrations,
    attach_log_bus,
    emit_journal,
    eval_condition,
    jsonb_init,
    pg_pool,
    prepare_command,
    run_service,
    setup_logging,
    trigger_matches,
    validate_definition,
    validate_state,
)
from home_core.health import HealthMarker
from home_core.tasks import spawn

from dida_automation.heating import HeatingController
from dida_automation.manual import ManualOverrides
from dida_automation.sandbox import StarlarkPool, StarlarkTimeout
from dida_automation.starlark_runtime import StarlarkError, compile_script

setup_logging()
log = logging.getLogger("dida.automation")

REFRESH_INTERVAL = 3.0   # how often to reload enabled rules from Postgres
ACTION_TIMEOUT = 5.0     # per-firing wall-clock cap (conditions + actions)
STARLARK_WORKERS = 3     # warm sandbox worker processes
STARLARK_TIMEOUT = 2.0   # per-evaluation wall-clock cap (real: overrun → SIGKILL)
RUN_SUBJECT = "dida.automation.run"  # API → engine: force-run a rule's actions now
CHECK_SUBJECT = "dida.automation.check"  # API → engine: compile + dry-run a Starlark script (request/reply)
BREAKER_THRESHOLD = 5    # consecutive errors before a rule is auto-disabled
# JetStream replays events missed while we were down (at-least-once). Replay is
# wanted for the last-value CACHE (converge on reality) but NOT for actions: a
# motion event from 20 minutes ago must not switch lights on now. Events the bus
# stored longer ago than this are "stale": they update the cache and may CANCEL a
# pending hold (safe side), but never fire an action or arm a hold.
STALE_EVENT_NS = 60 * 1_000_000_000  # 60 s
# A trigger skipped as stale is written to the rule's run log once per burst (a
# replay after downtime delivers them by the hundred), with the count.
STALE_NOTE_DELAY_S = 30.0
# Schedule-class triggers: time and sun reach the engine as transitions from these
# adapters, so a restart spanning one is the only case where a REPLAYED (stale)
# event still deserves to act — see reconstruct_schedule_triggers.
SCHEDULE_NAMESPACES = ("astro:", "calendar:")
# How late a missed schedule step may still fire. Long enough to cover a deploy or
# a crash-restart, short enough that a machine off all day doesn't wake up and run
# the evening's rules at breakfast.
SCHEDULE_RECOVER_S = 20 * 60
# The error breaker only counts EXCEPTIONS. A rule that keeps firing SUCCESSFULLY
# in a self-perpetuating loop (X writes a derived value that triggers Y that writes
# X, …) would never trip it. Cap the successful firing rate too: more than this many
# fires inside the window is not a home automation, it's a feedback loop → auto-
# disable it (same UI surfacing as the error breaker). A real loop blows past this
# in well under REFRESH_INTERVAL, so the in-memory `disabled` flag halts it at once.
FIRE_RATE_LIMIT = 30
FIRE_RATE_WINDOW = 10.0  # seconds
# Fleet-wide ceiling over the same window. The per-rule cap can't see a loop that
# SPANS rules (A triggers B triggers A: each stays under its own limit while the
# pair runs forever), so this catches the aggregate. Set well above what a busy
# house does legitimately — a scene recall or a presence change fires a burst —
# so only a self-sustaining loop reaches it.
FLEET_RATE_LIMIT = 120
HEATING_INTERVAL = 30.0  # seconds between heating control passes (a house is slow)

_MISSING = object()  # sentinel: this (entity, capability) has no cached last value yet


def _condition_entities(conditions) -> list[str]:
    """All distinct entity ids referenced by a condition tree (leaves only), so
    the snapshot fetch covers and/or/not groups too."""
    out: set[str] = set()

    def walk(c) -> None:
        if c.kind == "":
            if c.entity_id:
                out.add(c.entity_id)
        else:
            for sub in c.conditions:
                walk(sub)

    for c in conditions:
        walk(c)
    return list(out)


@dataclass
class _StaleBurst:
    entity_id: str
    count: int = 0
    max_age_s: float | None = None


class Rule:
    __slots__ = ("ast", "defn", "disabled", "errors", "fires", "id", "last_fire", "name")

    def __init__(self, rid: int, name: str, defn: AutomationDef, ast=None) -> None:
        self.id = rid
        self.name = name
        self.defn = defn
        self.ast = ast  # compiled Starlark (Layer 2); None for typed rules
        self.errors = 0  # consecutive failures, in-memory (resets on restart)
        self.fires: list[float] = []  # monotonic ts of recent successful firings (runaway guard)
        # Separate from `fires`, which the runaway guard trims to its own window: a
        # cooldown may outlast that window and still has to be enforced.
        self.last_fire: float | None = None
        self.disabled = False  # runaway guard tripped it; halts fires until reload drops the rule


# state("entity_id", "capability") literals in a helper script — its INPUTS. The
# script IS the definition: whatever statuses it reads are what it recomputes on. A
# non-literal state() call (variable args) can't be watched, so it is documented as
# unsupported rather than silently ignored downstream.
_STATE_CALL = re.compile(r"""state\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']\s*\)""")


# A helper may read only KNOWN statuses, never another COMPUTED value — a
# `helper:`/`derived:` reference would make its result depend on the OTHER's
# evaluation order (a race) and let helpers form a cycle. The flat derivation is the
# whole guarantee, so a definition that breaks it is rejected, not evaluated.
_COMPUTED_NS = ("helper", "derived")


def _cond_inputs(c: Condition) -> set[tuple[str, str]]:
    """Every (entity_id, capability) a condition tree reads — the statuses that,
    when they change, must recompute a helper that uses this condition."""
    if c.kind == "":
        return {(c.entity_id, c.capability)} if c.entity_id else set()
    return {p for sub in c.conditions for p in _cond_inputs(sub)}


class Helper:
    """A computed helper: a pure derivation of statuses into a value, evaluated by the
    engine using the SAME Condition model + eval_condition as automations.

    A typed rule is a list of (conditions -> value) branches with a default (first
    branch whose conditions all hold wins); an advanced helper uses a Starlark
    `value = …` script instead. Either way `inputs` are the statuses it reads, so a
    change to any recomputes it. It publishes ONLY its own value, never a command (the
    reason it can't loop), and may read only RAW statuses, never another computed value
    (the reason helpers can't race or cycle)."""
    __slots__ = ("bad_ref", "branches", "capability", "default", "entity_id", "inputs", "name", "script")

    def __init__(self, entity_id: str, name: str, capability: str, definition: dict) -> None:
        self.entity_id = entity_id
        self.name = name
        self.capability = capability
        self.script = str(definition.get("script") or "").strip()
        # branches: [(conditions, value)] — validated shape from the API; a stored
        # definition that won't decode raises, surfaced as last_error by the caller.
        self.branches: list[tuple[list[Condition], object]] = [
            (msgspec.convert(br.get("conditions") or [], list[Condition]), br.get("value"))
            for br in (definition.get("branches") or [])
        ]
        self.default = definition.get("default")
        # Inputs = every status the script OR the branch conditions read.
        self.inputs: set[tuple[str, str]] = {(e, c) for e, c in _STATE_CALL.findall(self.script)}
        for conds, _ in self.branches:
            for cond in conds:
                self.inputs |= _cond_inputs(cond)
        # A reference to another computed value — rejected (defense in depth vs a
        # DB-inserted definition; the API blocks it on write).
        self.bad_ref = next((e for e, _ in self.inputs if e.split(":", 1)[0] in _COMPUTED_NS), None)

    def compute(self, snapshot: dict) -> object:
        """The helper's value for this state snapshot: the advanced script's result,
        or the first branch whose conditions all hold, else the default. PURE."""
        for conds, value in self.branches:
            if all(eval_condition(c, snapshot) for c in conds):
                return value
        return self.default


class AutomationEngine:
    def __init__(self, bus: Bus, pool: asyncpg.Pool) -> None:
        self._bus = bus
        self._pool = pool
        # Layer-2 execution runs in killable worker processes (real sandbox cap).
        self._starlark = StarlarkPool(size=STARLARK_WORKERS, timeout=STARLARK_TIMEOUT)
        self._rules: list[Rule] = []
        self._fired = 0
        self._runs_since_prune = 0  # prune the run log every N recorded runs
        # Compile cache: id -> (script_text, ast_or_None). Avoids recompiling and
        # re-writing last_error every reload; None ast = known parse failure.
        self._ast_cache: dict[int, tuple[str, object]] = {}
        # Pending `for_seconds` hold timers, keyed by (rule id, trigger index) —
        # a rule may have several held triggers, each with its own timer. A
        # qualifying report (re)starts the timer; the value moving away cancels it.
        self._pending: dict[tuple[int, int], asyncio.Task] = {}
        # Last seen value per (entity_id, capability), a mirror of current_state
        # maintained from the event firehose. Triggers fire on TRANSITIONS, not on
        # every report: without this, an adapter re-publishing an unchanged value
        # (z2m battery checkin, esphome reconnect re-announce, astro restart) would
        # re-fire `to=` rules and restart every hold timer. Seeded at boot.
        self._last: dict[tuple[str, str], object] = {}
        # Last value we PUBLISHED for each computed helper, kept separate from
        # self._last. It dedups the bus (don't re-emit an unchanged helper on every
        # input tick) WITHOUT pre-empting transition detection: self._last must be
        # advanced only by the round-trip in on_event, so a real Off→Zone 1 helper
        # change is still seen as a transition and its `to=` consumers fire. Folding
        # this into self._last (the old optimistic write) silently blinded every
        # helper consumer — the bug that left irrigation/mower rules dead.
        self._published: dict[tuple[str, str], object] = {}
        # Rules currently executing. A rule is single-flight (HA's default
        # `mode: single`): a re-trigger while it's still running is ignored, so
        # a long sequence (a gate's click-wait-click) can't overlap itself.
        self._firing: set[int] = set()
        self._stale_bursts: dict[int, _StaleBurst] = {}
        self._fleet_fires: list[float] = []  # monotonic ts of recent fires across ALL rules
        self._helper_seq: dict[str, int] = {}  # helper entity_id -> latest issued eval seq (latest-wins)
        # Last logged validation error per rule id — so a permanently-invalid def
        # logs once, not every REFRESH_INTERVAL reload (F8).
        self._invalid_logged: dict[int, str] = {}
        # Computed helpers — the PURE derivation layer below automations. Each is a
        # script that reads state() and yields a `value`; it can run NO action, so it
        # cannot loop. Loaded alongside rules; keyed by entity_id.
        self._helpers: dict[str, Helper] = {}
        # Entities whose device the engine has marked unreachable. Their last value is
        # a memory, not a reading: a presence sensor that dropped off the network keeps
        # saying "empty" forever, and a rule reading it switches the room off around the
        # people in it. So rules and helpers see these as unknown.
        self._unreachable: frozenset[str] = frozenset()
        self._manual = ManualOverrides(bus, pool)

    def _snapshot(self) -> dict[tuple[str, str], object]:
        if not self._unreachable:
            return dict(self._last)
        return {k: v for k, v in self._last.items() if k[0] not in self._unreachable}

    async def refresh_reachability(self) -> None:
        rows = await self._pool.fetch(
            "SELECT e.entity_id FROM entities e "
            "LEFT JOIN devices d ON d.device_key = COALESCE(e.device_key, e.entity_id) "
            "LEFT JOIN adapter_liveness l ON l.adapter = e.adapter "
            "WHERE d.reachable = false OR l.alive = false"
        )
        fresh = frozenset(r["entity_id"] for r in rows)
        moved = fresh ^ self._unreachable
        self._unreachable = fresh
        if moved:
            log.info("reachability: %d entit%s now unreachable", len(fresh), "y" if len(fresh) == 1 else "ies")
            for helper in self._helpers.values():
                if any(e in moved for e, _ in helper.inputs):
                    spawn(self._eval_helper(helper), log=log, name=f"helper {helper.name}")

    async def reload(self) -> None:
        rows = await self._pool.fetch(
            "SELECT id, name, definition FROM automations WHERE enabled = true ORDER BY id"
        )
        prev = {r.id: r for r in self._rules}
        rules: list[Rule] = []
        seen: set[int] = set()
        for row in rows:
            rid = int(row["id"])
            seen.add(rid)
            try:
                defn = validate_definition(row["definition"])
            except Exception as exc:
                # A stored def that fails semantic validation (e.g. a capability was
                # renamed/tightened under it) must surface in the UI, not die as a
                # log line repeated every reload (F8). Write last_error idempotently
                # (no-op once set) and log only on the first sighting of this error.
                msg = f"validation error: {exc}"
                await self._pool.execute(
                    "UPDATE automations SET last_error = $2 "
                    "WHERE id = $1 AND last_error IS DISTINCT FROM $2",
                    rid, msg,
                )
                if self._invalid_logged.get(rid) != msg:
                    self._invalid_logged[rid] = msg
                    log.warning("skipping invalid automation %s (%s): %s", rid, row["name"], exc, exc_info=True)
                continue
            self._invalid_logged.pop(rid, None)
            ast = None
            if defn.script.strip():
                ast = await self._compile(rid, defn.script)
                if ast is None:
                    continue  # parse error already recorded; skip until fixed
            rule = Rule(rid, row["name"], defn, ast)
            if rid in prev:  # carry the breaker/runaway state across a reload
                rule.errors = prev[rid].errors
                rule.fires = prev[rid].fires
                rule.last_fire = prev[rid].last_fire
            rules.append(rule)
        # Drop cache entries for automations no longer present.
        self._ast_cache = {k: v for k, v in self._ast_cache.items() if k in seen}
        # Cancel hold timers for rules that were disabled/deleted, AND for rules
        # whose definition changed mid-hold (the pending timer holds the OLD Rule
        # object and would fire the old actions).
        prev_defs = {r.id: r.defn for r in self._rules}
        new_by_id = {r.id: r for r in rules}
        for key in list(self._pending):
            rid, _tidx = key
            new = new_by_id.get(rid)
            if new is None or (rid in prev_defs and prev_defs[rid] != new.defn):
                self._pending.pop(key).cancel()
        self._rules = rules

    async def reload_helpers(self) -> None:
        """Load computed-helper definitions. Announce each as an entity (so the UI
        lists it and automations can target it), and recompute one whose script is NEW
        or CHANGED so its value exists without waiting for an input to move. A bad
        script (syntax/eval) is recorded to last_error and skipped, never fatal."""
        rows = await self._pool.fetch(
            "SELECT entity_id, name, capability, definition FROM computed_helpers WHERE enabled = true"
        )
        fresh: dict[str, Helper] = {}
        to_eval: list[Helper] = []
        for r in rows:
            eid = r["entity_id"]
            defn = r["definition"]
            if isinstance(defn, str):
                defn = json.loads(defn)
            try:
                h = Helper(eid, r["name"], r["capability"], defn or {})
            except Exception as exc:
                log.debug("helper %s invalid", eid, exc_info=True)
                await self._pool.execute(
                    "UPDATE computed_helpers SET last_error = $2 "
                    "WHERE entity_id = $1 AND last_error IS DISTINCT FROM $2",
                    eid, f"bad definition: {str(exc)[:400]}",
                )
                continue
            prev = self._helpers.get(eid)
            changed = (prev is None or prev.script != h.script or prev.capability != h.capability
                       or prev.branches != h.branches or prev.default != h.default)
            if changed:
                to_eval.append(h)  # new or changed → (re)compute once now
            fresh[eid] = h
            # Announce (idempotent-ish; publish_entity is cheap and last-write-wins).
            spawn(self._bus.publish_entity(EntityInfo(
                entity_id=eid, adapter="helper", name=h.name, capabilities=[h.capability],
                category="config",  # a derived setting, not a room device
            )), log=log, name=f"announce {eid}")
        self._helpers = fresh
        for h in to_eval:
            spawn(self._eval_helper(h), log=log, name=f"helper {h.name}")

    async def _eval_helper(self, helper: Helper) -> None:
        """Recompute a helper from the current state snapshot and publish its value if
        it changed. PURE: run_value gives the script `state()` only — no command, no
        set_state — so this can emit nothing but the helper's own value."""
        if helper.bad_ref is not None:
            await self._pool.execute(
                "UPDATE computed_helpers SET last_error = $2 "
                "WHERE entity_id = $1 AND last_error IS DISTINCT FROM $2",
                helper.entity_id,
                f"reads another computed value ({helper.bad_ref}) — a helper may read only "
                f"raw statuses (avoids a race/cycle)",
            )
            return
        if not helper.inputs:
            # Nothing to watch means nothing would ever recompute it: the helper would
            # publish one value at load and then sit frozen, looking alive. The way to
            # write that by accident is a state() call whose entity id is a variable
            # (a loop over a list of ids), which the input scan cannot see.
            await self._pool.execute(
                "UPDATE computed_helpers SET last_error = $2 "
                "WHERE entity_id = $1 AND last_error IS DISTINCT FROM $2",
                helper.entity_id,
                "reads no status through a literal state() call — nothing would ever "
                "recompute it (an id held in a variable cannot be watched)",
            )
            return
        # Latest-wins guard. A helper is re-evaluated per input event, and evaluation
        # AWAITS (the Starlark worker round-trip), so a burst starts several: #1 takes
        # the old snapshot and parks in the worker queue while #2 computes from the new
        # one and publishes. Without this, #1 then publishes ITS older value on top —
        # which sticks, and fires consumers' `to=` triggers with the regression, until
        # some later input changes again. Claim a sequence with the snapshot (both
        # synchronous, no await between) and drop the result if a newer eval started.
        seq = self._helper_seq.get(helper.entity_id, 0) + 1
        self._helper_seq[helper.entity_id] = seq
        snapshot = self._snapshot()
        try:
            # Advanced Starlark expression, else the typed condition→value branches.
            raw = await self._starlark.run_value(helper.script, snapshot) if helper.script \
                else helper.compute(snapshot)
            value = validate_state(helper.capability, raw)
        except Exception as exc:
            # Broad on purpose: a helper is the boundary, and ANY failure (script
            # error, sandbox timeout, a worker death now normalized to StarlarkError,
            # a bad value) must land as a visible last_error rather than escaping to
            # the spawn() wrapper — which would log generically and re-churn on the
            # next input tick with nothing surfaced in the UI.
            await self._pool.execute(
                "UPDATE computed_helpers SET last_error = $2 "
                "WHERE entity_id = $1 AND last_error IS DISTINCT FROM $2",
                helper.entity_id, str(exc)[:500],
            )
            log.warning("computed helper %s failed: %s", helper.entity_id, exc, exc_info=True)
            return
        if self._helper_seq.get(helper.entity_id) != seq:
            return  # a newer evaluation started while we were in the sandbox — it wins
        await self._pool.execute(
            "UPDATE computed_helpers SET last_error = NULL WHERE entity_id = $1 AND last_error IS NOT NULL",
            helper.entity_id,
        )
        key = (helper.entity_id, helper.capability)
        if self._published.get(key) == value:
            return  # already published this value — a re-emit on every input tick would
            # only spam the bus; the engine re-emits an event only on a real change anyway.
        # Record what we're about to publish, NOT into self._last: transition detection
        # for consumers must advance only when the value round-trips back through
        # on_event, otherwise their `to=` triggers never see the change (see __init__).
        self._published[key] = value
        await self._bus.publish_state(StateUpdate(
            entity_id=helper.entity_id, capability=helper.capability, value=value,
            adapter="helper", ts_ns=time.time_ns(), name=helper.name,
        ))

    async def seed_last_values(self) -> None:
        """Prime the last-value cache from current_state so the first events after
        boot are compared against reality — otherwise every adapter's reconnect
        re-announce would read as a transition and fire `to=` rules spuriously."""
        rows = await self._pool.fetch("SELECT entity_id, capability, value FROM current_state")
        self._last = {(r["entity_id"], r["capability"]): r["value"] for r in rows}
        # Seed the publish-dedup cache from the same snapshot: a helper whose recompute
        # matches its persisted value must not re-publish (and so re-fire consumers) at
        # boot. It only ever holds helper keys, but priming from all rows is harmless.
        self._published = dict(self._last)
        log.info("seeded %d last-value entries from current_state", len(self._last))

    async def reconstruct_schedule_triggers(self) -> None:
        """Fire a SCHEDULED trigger whose moment passed while the service was down.

        Time and sun are not a clock in here — they arrive as ordinary transitions
        from the astro/calendar adapters, which means a scheduled rule is subject to
        the same stale-event gate as a motion sensor: a restart spanning 18:00 sees
        the sunset transition replayed from JetStream, older than STALE_EVENT_NS, and
        silently doesn't fire. For motion that gate is right (acting on a movement
        from ten minutes ago is a ghost action); for "lights at sunset" it means the
        thing simply didn't happen that evening, with nothing in the log to say so.

        So: at boot, for a schedule-class trigger (astro:/calendar:) whose entity is
        ALREADY sitting at the qualifying value, fire it if the transition happened
        inside the recovery window and the rule hasn't already fired for it. The
        window is what keeps this honest — a deploy fires the step it interrupted,
        never a whole day's worth of missed schedule."""
        for rule in self._rules:
            for tidx, trig in enumerate(rule.defn.triggers):
                if trig.for_seconds or trig.to is None:
                    continue
                if not trig.entity_id.startswith(SCHEDULE_NAMESPACES):
                    continue
                row = await self._pool.fetchrow(
                    "SELECT value, changed_at, EXTRACT(EPOCH FROM (now() - changed_at)) AS age "
                    "FROM current_state WHERE entity_id = $1 AND capability = $2",
                    trig.entity_id, trig.capability,
                )
                if row is None or row["value"] != trig.to:
                    continue
                if float(row["age"] or 0) > SCHEDULE_RECOVER_S:
                    continue  # too long ago to still be "the step we interrupted"
                last_fired = await self._pool.fetchval(
                    "SELECT last_triggered_at FROM automations WHERE id = $1", rule.id
                )
                if last_fired is not None and row["changed_at"] is not None \
                        and last_fired >= row["changed_at"]:
                    continue  # already ran for this transition
                update = StateUpdate(
                    entity_id=trig.entity_id, capability=trig.capability,
                    value=trig.to, adapter="reconstruct", ts_ns=time.time_ns(),
                )
                log.info("recovering missed schedule trigger for %r (id=%s t=%d): %.0fs late",
                         rule.name, rule.id, tidx, float(row["age"] or 0))
                spawn(self._fire(rule, trig, update), log=log, name=f"recover {rule.name}")

    async def reconstruct_holds(self) -> None:
        """Re-arm `for_seconds` holds that were pending when the service stopped
        (A3). For each enabled held rule whose value ALREADY equals `to`, schedule
        the REMAINING hold from current_state.changed_at — the transition into `to`,
        NOT the last report (updated_at bumps on unchanged re-reports, which would
        reset a running countdown to nearly full on every deploy). So a deploy
        mid-hold (motion ended, 300 s countdown running) doesn't leave lights on all
        night. Skips a hold that ALREADY fired for this transition (its rule's
        last_triggered_at is at or after the value settled), so a non-idempotent
        action (toggle/notify/gate pulse) doesn't re-fire on every deploy while the
        trigger entity simply rests in its qualifying state. Called once at boot,
        after reload()+seed."""
        for rule in self._rules:
            for tidx, trig in enumerate(rule.defn.triggers):
                if not trig.for_seconds or trig.to is None:
                    continue
                row = await self._pool.fetchrow(
                    "SELECT value, changed_at, EXTRACT(EPOCH FROM (now() - changed_at)) AS age "
                    "FROM current_state WHERE entity_id = $1 AND capability = $2",
                    trig.entity_id, trig.capability,
                )
                if row is None or row["value"] != trig.to:
                    continue
                last_fired = await self._pool.fetchval(
                    "SELECT last_triggered_at FROM automations WHERE id = $1", rule.id
                )
                if last_fired is not None and row["changed_at"] is not None \
                        and last_fired >= row["changed_at"]:
                    continue  # the hold already ran for this transition — don't re-fire
                remaining = max(0.0, trig.for_seconds - float(row["age"]))
                update = StateUpdate(
                    entity_id=trig.entity_id, capability=trig.capability,
                    value=trig.to, adapter="reconstruct", ts_ns=time.time_ns(),
                )
                self._pending[(rule.id, tidx)] = spawn(
                    self._fire_after_hold(rule, tidx, trig, update, delay=remaining),
                    log=log, name=f"hold {rule.name}",
                )
                log.info("reconstructed hold for %r (id=%s t=%d): %.0fs remaining", rule.name, rule.id, tidx, remaining)

    async def _compile(self, rid: int, script: str):
        """Compile a Starlark script, cached by text. On a syntax error, record
        it as the rule's last_error (once) and return None so it's skipped."""
        cached = self._ast_cache.get(rid)
        if cached and cached[0] == script:
            return cached[1]
        try:
            ast = compile_script(script)
        except StarlarkError as exc:
            self._ast_cache[rid] = (script, None)
            await self._pool.execute(
                "UPDATE automations SET last_error = $2 WHERE id = $1",
                rid, f"starlark parse error: {exc}",
            )
            log.warning("automation %s starlark parse error: %s", rid, exc)
            return None
        had_error = cached is not None and cached[1] is None
        self._ast_cache[rid] = (script, ast)
        if had_error:
            # The previous text failed to parse and wrote last_error — the fix
            # must clear it now, not at the next actual firing.
            await self._pool.execute(
                "UPDATE automations SET last_error = NULL WHERE id = $1", rid
            )
        return ast

    async def on_event(self, update: StateUpdate) -> None:
        key = (update.entity_id, update.capability)
        # A transition = the value differs from what we last saw. Computed ONCE
        # per event, before any per-rule work, so rules sharing a trigger all see
        # the same answer; self._last is updated only after the loop.
        prev = self._last.get(key, _MISSING)
        changed = prev is _MISSING or prev != update.value
        # BUTTON is a MOMENTARY event (a remote keypress), not a settled state.
        # zigbee2mqtt publishes `action` only on an actual press — never re-sent on
        # battery/link reports (verified) — so every BUTTON event is a fresh press.
        # Fire on each, even a repeat of the same button (no transition), mirroring
        # HA's ZHA event trigger (fire-per-press) rather than a state change.
        is_event = update.capability == "button"
        # Replayed-from-JetStream events catch the cache up but must not act:
        # firing a rule for something that happened while we were down is exactly
        # the "ghost action" class this gate exists to prevent.
        age_ns = None if update.received_ns is None else time.time_ns() - update.received_ns
        fresh = age_ns is not None and age_ns <= STALE_EVENT_NS
        if changed:
            try:
                await self._manual.on_state(update.entity_id, update.capability, prev, update.value, fresh)
            except Exception:
                log.exception("manual hold: could not follow %s", update.entity_id)
        for rule in self._rules:
            for tidx, trig in enumerate(rule.defn.triggers):
                if trig.entity_id != update.entity_id or trig.capability != update.capability:
                    continue
                if trig.for_seconds:
                    # Held trigger: (re)start on a fresh transition, cancel when it
                    # moves away (stale may cancel too — safe side), never fire now.
                    self._handle_hold(rule, tidx, trig, update, changed, fresh)
                elif (changed or is_event) and trigger_matches(trig, update.entity_id, update.capability, update.value):
                    if not fresh:
                        self._skip_stale(rule, update, age_ns)
                        continue
                    # Fire on the transition INTO the qualifying value, concurrently so
                    # a slow rule doesn't hold up the handler or the other rules. The
                    # firing trigger is passed so Starlark can branch on its id.
                    spawn(self._fire(rule, trig, update), log=log, name=f"fire {rule.name}")
        self._last[key] = update.value
        # Computed helpers: the pure derivation layer. Recompute any whose script reads
        # this status — with self._last already updated so the helper sees the new value.
        # Replayed (stale) events still recompute: a helper is a pure function of state,
        # so re-deriving it from a caught-up cache is correct and idempotent (unlike an
        # ACTION, which must not re-run for the past). A helper never reads its OWN
        # entity, so publishing its value can't re-enter here for the same helper.
        for helper in self._helpers.values():
            if key in helper.inputs:
                spawn(self._eval_helper(helper), log=log, name=f"helper {helper.name}")

    def _handle_hold(self, rule: Rule, tidx: int, trig, update: StateUpdate, changed: bool, fresh: bool) -> None:
        """A `for_seconds` trigger reported. Start the hold when the value
        TRANSITIONS into the qualifying state; cancel when it moves away; ignore an
        unchanged re-report while a hold is already pending (so a periodic re-report
        of the same value can't push the fire out indefinitely). Mirrors HA's `for:`
        — the timer measures how long the value has actually held, uninterrupted.

        Stale (replayed) events are cache-only for the hold state: they neither ARM
        nor CANCEL. A stale event is by definition OLDER than current_state, and
        reconstruct_holds() armed the hold from that newer current_state — so a stale
        replayed move-away must NOT cancel it (it would be dropped and, being stale,
        never re-armed → the action silently lost, H). The hold's own expiry
        re-checks current_state before firing, so keeping it is safe."""
        key = (rule.id, tidx)
        pending = self._pending.get(key)
        qualifies = trig.to is None or trig.to == update.value
        if not qualifies:
            # value left the trigger condition — a FRESH move-away cancels the hold
            # (don't restart); a stale one is ignored (see the docstring — H).
            if fresh and pending is not None:
                pending.cancel()
                self._pending.pop(key, None)
            return
        if not fresh:
            return  # stale qualifying event: cache-only, never arm/restart a hold
        if not changed:
            # Unchanged re-report of the qualifying value (z2m checkin, poll
            # republish). A hold arms ONLY on the transition INTO the value: while
            # one is pending we must not restart it, and — critically — once it has
            # fired and cleared `pending`, a steady re-report must NOT re-arm and
            # re-fire (F2). The next arm needs a real transition away and back.
            return
        if pending is not None:
            pending.cancel()  # a genuine re-transition restarts the timer (HA `for:`)
        self._pending[key] = spawn(
            self._fire_after_hold(rule, tidx, trig, update),
            log=log, name=f"hold {rule.name}",
        )

    async def _fire_after_hold(self, rule: Rule, tidx: int, trig, update: StateUpdate, delay: float | None = None) -> None:
        """Wait the hold, re-confirm the value still holds, then fire. `delay`
        overrides the full duration — used to re-arm the REMAINING time when a hold
        is reconstructed after a restart (A3)."""
        key = (rule.id, tidx)
        try:
            await asyncio.sleep(trig.for_seconds if delay is None else delay)
            if trig.entity_id in self._unreachable:
                return
            if trig.to is not None:
                cur = await self._pool.fetchval(
                    "SELECT value FROM current_state WHERE entity_id = $1 AND capability = $2",
                    trig.entity_id, trig.capability,
                )
                if cur != trig.to:
                    return  # changed during the window (and we missed the cancel) — don't fire
            # COMMITTED to fire past here: un-register from `_pending` FIRST so a
            # move-away arriving mid-fire (which cancels the pending task) can no
            # longer abort a side-effecting sequence partway — leaving a gate
            # half-actuated / a multi-command pulse partly sent. Only the sleep +
            # re-confirm above is cancellable; the fire itself is not.
            if self._pending.get(key) is asyncio.current_task():
                del self._pending[key]
            await self._fire(rule, trig, update)
        except asyncio.CancelledError:
            raise  # orderly cancel (reload/away/re-transition) — propagate
        except Exception as exc:
            # A DB blip during the re-confirm (or bookkeeping) must not vanish the
            # hold silently — lights would stay on with zero trace (F3). Route it
            # through the breaker like any firing error.
            log.debug("rule %s hold failed", rule.id, exc_info=True)
            await self._on_error(rule, exc)
        finally:
            if self._pending.get(key) is asyncio.current_task():
                del self._pending[key]

    async def _fire(self, rule: Rule, trig, update: StateUpdate) -> None:
        if rule.disabled:
            return  # runaway guard already tripped — halt until the reload drops it (≤REFRESH_INTERVAL)
        if rule.id in self._firing:
            # mode=single: still running from a prior trigger. For a hold-expiry
            # this consumes the fire — log it so a "lights stayed on" has a trace.
            log.warning("automation %r (id=%s): fire suppressed — previous run still in flight",
                        rule.name, rule.id)
            return
        now = time.monotonic()
        cd = rule.defn.cooldown_seconds
        if cd and rule.last_fire is not None and now - rule.last_fire < cd:
            # Deliberately silent and NOT counted as a fire: the rule matched
            # correctly, the operator just asked not to hear it again yet.
            return
        rule.fires = [t for t in rule.fires if t > now - FIRE_RATE_WINDOW]
        if len(rule.fires) >= FIRE_RATE_LIMIT:
            rule.disabled = True  # stop the loop NOW; DB disable + reload finish the job
            await self._on_runaway(rule)
            return
        # …and the same check across ALL rules. The per-rule cap misses the loop that
        # spans two of them: A triggers B triggers A splits the rate, so each stays
        # under its own limit while the pair hammers a device forever. The fleet-wide
        # ceiling is deliberately high — a busy house genuinely fires a lot at once
        # (a scene, a presence change) — so it only trips on a self-sustaining loop.
        self._fleet_fires = [t for t in self._fleet_fires if t > now - FIRE_RATE_WINDOW]
        if len(self._fleet_fires) >= FLEET_RATE_LIMIT:
            rule.disabled = True
            await self._on_runaway(rule, fleet=True)
            return
        # A typed rule with action delays (a gate pulse sequence) needs a longer
        # cap than the default — allow for the total delay plus the base budget.
        timeout = ACTION_TIMEOUT
        if rule.ast is None:
            timeout += sum(a.delay_ms for a in rule.defn.actions) / 1000.0
        self._firing.add(rule.id)
        try:
            ran = await asyncio.wait_for(self._run(rule, trig, update), timeout=timeout)
        except Exception as exc:
            log.debug("rule %s failed", rule.id, exc_info=True)
            await self._on_error(rule, exc)
        else:
            rule.errors = 0
            if ran:
                # Record only actual firings — a condition-blocked match (or a
                # Starlark script that emitted nothing) isn't one.
                rule.fires.append(now)  # feed the runaway-rate guard
                rule.last_fire = now
                self._fleet_fires.append(now)  # …and the cross-rule one
                self._fired += 1
                await self._pool.execute(
                    "UPDATE automations SET last_triggered_at = now(), last_error = NULL WHERE id = $1",
                    rule.id,
                )
                await self._record_run(rule, "fired")
                # The device timeline's answer to "why did this come on": the
                # rule is recorded against the entity that TRIGGERED it, which
                # automation_runs (per-rule, no entity) can't say.
                await emit_journal(
                    self._bus, "automation_fired", entity_id=update.entity_id,
                    source="automation", severity="info", message=rule.name,
                    data={"automation_id": rule.id,
                          "capability": update.capability, "value": update.value},
                )
        finally:
            self._firing.discard(rule.id)

    async def _run(self, rule: Rule, trig, update: StateUpdate) -> bool:
        """Execute the rule. Returns True if commands were actually published.
        A typed rule runs the same actions whichever trigger fired (uniform); a
        Starlark rule gets the firing trigger so it can branch on its id."""
        # A button is a person pressing it, so what its rule does is done by hand.
        by_hand = update.capability == "button"
        if rule.ast is not None:
            return await self._run_starlark(rule, trig, update, by_hand=by_hand)
        return await self._run_typed(rule, by_hand=by_hand)

    async def _run_typed(self, rule: Rule, *, by_hand: bool = False) -> bool:
        # Conditions (leaf or and/or/not groups) are evaluated against a snapshot
        # of the entities they reference; the top-level list is implicitly AND-ed.
        if rule.defn.conditions:
            entities = _condition_entities(rule.defn.conditions)
            rows = await self._pool.fetch(
                "SELECT entity_id, capability, value FROM current_state WHERE entity_id = ANY($1)",
                entities,
            )
            snapshot = {(r["entity_id"], r["capability"]): r["value"] for r in rows
                        if r["entity_id"] not in self._unreachable}
            for cond in rule.defn.conditions:
                if not eval_condition(cond, snapshot):
                    return False  # a guard failed — not an error, just don't run actions
        return await self._run_actions(rule, by_hand=by_hand)

    async def _run_actions(self, rule: Rule, *, by_hand: bool = False) -> bool:
        # Actions run IN ORDER, honouring each action's delay — that's how a
        # timed sequence (gate click-wait-click, a blink) is executed.
        sent = 0
        for act in rule.defn.actions:
            if act.delay_ms:
                await asyncio.sleep(act.delay_ms / 1000.0)
            sent += await self._publish(rule, await prepare_command(
                self._pool, act.entity_id, act.capability, act.command, act.args,
                source=f"automation:{rule.id}:{rule.name}"), by_hand=by_hand)
        if sent:
            log.info("fired %r (id=%s)", rule.name, rule.id)
        return bool(sent)

    async def _publish(self, rule: Rule, cmd: Command, *, by_hand: bool) -> bool:
        if not by_hand and self._manual.holds(cmd, self._last):
            log.info("automation %r: %s held on by hand, not turned off", rule.name, cmd.entity_id)
            await self._record_run(rule, "held", cmd.entity_id)
            return False
        self._manual.note_command(cmd, by_hand=by_hand)
        await self._bus.publish_command(cmd)
        return True

    async def run_now(self, rule: Rule) -> None:
        """Force-run a rule on demand (skip trigger + conditions) — like HA's
        'Run'. Single-flight, so a manual run can't overlap a live firing or
        another manual run. Starlark runs against a synthetic empty event."""
        if rule.id in self._firing:
            return
        timeout = ACTION_TIMEOUT
        if rule.ast is None:
            timeout += sum(a.delay_ms for a in rule.defn.actions) / 1000.0
        self._firing.add(rule.id)
        try:
            if rule.ast is not None:
                synthetic = StateUpdate(
                    entity_id="", capability="", value=None, adapter="manual", ts_ns=time.time_ns(),
                )
                ran = await asyncio.wait_for(self._run_starlark(rule, None, synthetic, by_hand=True), timeout=timeout)
            else:
                ran = await asyncio.wait_for(self._run_actions(rule, by_hand=True), timeout=timeout)
            if ran:
                # Same rule as _fire: only an actual firing updates the counters —
                # a script that emitted nothing didn't "trigger just now".
                self._fired += 1
                await self._pool.execute(
                    "UPDATE automations SET last_triggered_at = now(), last_error = NULL WHERE id = $1",
                    rule.id,
                )
                await self._record_run(rule, "fired", "▶ ručno")
                await emit_journal(
                    self._bus, "automation_fired", source="automation", severity="info",
                    message=rule.name, data={"automation_id": rule.id, "manual": True},
                )
        except Exception as exc:
            log.debug("rule %s run failed", rule.id, exc_info=True)
            await self._on_error(rule, exc)
        finally:
            self._firing.discard(rule.id)

    async def _run_starlark(self, rule: Rule, trig, update: StateUpdate, *, by_hand: bool = False) -> bool:
        # Snapshot current state so the hermetic script can read it via state().
        # Use the in-memory last-value cache — a faithful mirror of current_state
        # (seeded from the identical SELECT at boot, updated on every event before
        # any fire runs, since _fire is spawned and self._last is set synchronously
        # first) — instead of a per-fire full-table SELECT on the hot firing path.
        snapshot = self._snapshot()
        event = {
            "entity_id": update.entity_id,
            "capability": update.capability,
            "value": update.value,
            # Which trigger fired — lets a multi-trigger script branch (ON/OFF in
            # one rule). Empty for a manual run or a trigger with no id set.
            "trigger_id": trig.id if trig is not None else "",
        }
        # Execute in a killable worker PROCESS: a runaway bounded loop overruns the
        # sandbox wall-clock cap and is SIGKILLed, so it can't freeze the service
        # (a thread couldn't be killed — starlark.eval holds the GIL). A timeout or
        # error raises → the firing trips the breaker like any other failure.
        emitted, states = await self._starlark.run(rule.defn.script, event, snapshot)
        # Validate everything the script emitted BEFORE publishing any of it, so a
        # bad command/state trips the breaker without half-applying the rule.
        commands = [await prepare_command(self._pool, entity_id, capability, command, args,
                                          source=f"automation:{rule.id}:{rule.name}")
                    for entity_id, capability, command, args in emitted]
        for _entity_id, capability, value in states:
            # set_state is for DERIVED "template sensors" only — reject a write to any
            # other namespace so a script can't forge a real device's reading into
            # current_state (e.g. set_state("mqtt:kitchen", …) faking a physical light).
            ns = _entity_id.split(":", 1)[0] if ":" in _entity_id else ""
            if ns != "derived":
                raise ValueError(
                    f"set_state may only write 'derived:' entities, not {_entity_id!r}"
                )
            validate_state(capability, value)
        sent = 0
        for cmd in commands:
            sent += await self._publish(rule, cmd, by_hand=by_hand)
        # Derived state: publish as a StateUpdate (a "template sensor"). adapter is
        # the entity's namespace prefix so it groups sensibly in the UI.
        for entity_id, capability, value in states:
            ns = entity_id.split(":", 1)[0] if ":" in entity_id else "derived"
            await self._bus.publish_state(
                StateUpdate(
                    entity_id=entity_id,
                    capability=capability,
                    value=value,
                    adapter=ns,
                    ts_ns=time.time_ns(),
                )
            )
        n = sent + len(states)
        if n:
            log.info("fired (starlark) %r (id=%s) — %d cmd(s), %d state(s)",
                     rule.name, rule.id, sent, len(states))
        return bool(n)

    def _skip_stale(self, rule: Rule, update: StateUpdate, age_ns: int | None) -> None:
        burst = self._stale_bursts.get(rule.id)
        if burst is None:
            burst = self._stale_bursts[rule.id] = _StaleBurst(update.entity_id)
            spawn(self._note_stale(rule), log=log, name=f"stale note {rule.name}")
        burst.count += 1
        if age_ns is not None:
            burst.max_age_s = max(burst.max_age_s or 0.0, age_ns / 1e9)

    async def _note_stale(self, rule: Rule) -> None:
        await asyncio.sleep(STALE_NOTE_DELAY_S)
        burst = self._stale_bursts.pop(rule.id)
        log.warning("automation %r (id=%s): %d trigger(s) skipped, older than %d s (oldest %s s)",
                    rule.name, rule.id, burst.count, STALE_EVENT_NS // 1_000_000_000,
                    "unknown" if burst.max_age_s is None else f"{burst.max_age_s:.0f}")
        await self._record_run(rule, "stale", str(burst.count))
        await emit_journal(
            self._bus, "automation_stale", entity_id=burst.entity_id,
            source="automation", severity="warning", message=rule.name,
            data={"automation_id": rule.id, "count": burst.count,
                  "max_age_s": None if burst.max_age_s is None else round(burst.max_age_s)},
        )

    async def _record_run(self, rule: Rule, outcome: str, detail: str = "") -> None:
        """Append a row to the run log — best-effort, never breaks a firing. Prunes
        rows older than 30 days every so often so the table stays bounded."""
        try:
            await self._pool.execute(
                "INSERT INTO automation_runs (automation_id, name, outcome, detail) VALUES ($1, $2, $3, $4)",
                rule.id, rule.name, outcome, (detail[:500] or None),
            )
            self._runs_since_prune += 1
            if self._runs_since_prune >= 500:
                self._runs_since_prune = 0
                await self._pool.execute(
                    "DELETE FROM automation_runs WHERE fired_at < now() - interval '30 days'"
                )
        except Exception as exc:
            log.warning("automation %r: could not record run: %s", rule.name, exc, exc_info=True)

    async def _on_error(self, rule: Rule, exc: Exception) -> None:
        rule.errors += 1
        await self._record_run(rule, "error", str(exc))
        log.warning("automation %r (id=%s) error %d/%d: %s",
                    rule.name, rule.id, rule.errors, BREAKER_THRESHOLD, exc)
        await emit_journal(
            self._bus, "automation_error", source="automation", severity="warning",
            message=f"{rule.name}: {exc}",
            data={"automation_id": rule.id, "errors": rule.errors, "threshold": BREAKER_THRESHOLD},
        )
        # The in-memory breaker state above is the source of truth; the DB writes
        # below are best-effort — if the firing failed BECAUSE Postgres is down,
        # these fail too and must not escape into the fire task (the breaker
        # would otherwise never trip).
        try:
            if rule.errors >= BREAKER_THRESHOLD:
                await self._pool.execute(
                    "UPDATE automations SET enabled = false, last_error = $2 WHERE id = $1",
                    rule.id,
                    f"auto-disabled after {rule.errors} consecutive errors: {exc}",
                )
                log.error("automation %r (id=%s) auto-disabled by circuit breaker", rule.name, rule.id)
                await emit_journal(
                    self._bus, "breaker_trip", source="automation", severity="error",
                    message=f"{rule.name}: auto-disabled after {rule.errors} consecutive errors",
                    data={"automation_id": rule.id, "error": str(exc)},
                )
            else:
                await self._pool.execute(
                    "UPDATE automations SET last_error = $2 WHERE id = $1", rule.id, str(exc)
                )
        except Exception as db_exc:
            log.warning("automation %r: could not record error in DB: %s", rule.name, db_exc, exc_info=True)

    async def _on_runaway(self, rule: Rule, *, fleet: bool = False) -> None:
        """Trip the runaway-firing guard: a rule firing far faster than any real
        home automation is a feedback loop. Auto-disable it, surfaced like the
        error breaker (last_error + disabled), so it stops instead of hammering
        the bus forever. `fleet` means the CROSS-RULE ceiling tripped — the loop
        spans several rules, and this one was simply the next to fire, so the
        message says so rather than blaming it alone."""
        msg = (f"auto-disabled: the whole rule set fired >{FLEET_RATE_LIMIT} times in "
               f"{FIRE_RATE_WINDOW:.0f}s — a feedback loop across rules; this one was next to fire"
               if fleet else
               f"auto-disabled: runaway firing (>{FIRE_RATE_LIMIT} fires in {FIRE_RATE_WINDOW:.0f}s — likely a feedback loop)")
        log.error("automation %r (id=%s) %s", rule.name, rule.id, msg)
        await self._record_run(rule, "error", msg)
        await emit_journal(
            self._bus, "breaker_trip", source="automation", severity="error",
            message=f"{rule.name}: {msg}", data={"automation_id": rule.id, "fleet": fleet},
        )
        try:
            await self._pool.execute(
                "UPDATE automations SET enabled = false, last_error = $2 WHERE id = $1", rule.id, msg
            )
        except Exception as db_exc:
            log.warning("automation %r: could not persist runaway disable: %s", rule.name, db_exc, exc_info=True)


_PROBE_VALUES = (True, False, 0, 1)


async def _dry_run(engine: AutomationEngine, script: str, trig: dict) -> tuple[list, str | None]:
    """Every command the script would emit across the probe values, or the error."""
    rows = await engine._pool.fetch("SELECT entity_id, capability, value FROM current_state")
    snapshot = {(r["entity_id"], r["capability"]): r["value"] for r in rows
                if r["entity_id"] not in engine._unreachable}
    # Dry-run across bool AND numeric event values, so a NUMERIC-triggered
    # script (person_count, brightness…) actually exercises its branches — a
    # bool-only probe would skip them and miss a bad command. Tolerate per-value
    # type errors (an int fed to a string-script); only a script that errors on
    # EVERY input is genuinely broken.
    emitted: list = []
    run_errs: list[str] = []
    # `trigger_id` must be present in the probe event: the live runtime always
    # injects it, and the multi-trigger branching pattern (`event["trigger_id"]`)
    # KeyErrors without it — which would make every such script fail Check as
    # "genuinely broken" (F4).
    trigger_id = str(trig.get("id", ""))
    for val in _PROBE_VALUES:
        ev = {
            "entity_id": trig.get("entity_id", ""),
            "capability": trig.get("capability", ""),
            "value": val,
            "trigger_id": trigger_id,
        }
        try:
            cmds, _states = await engine._starlark.run(script, ev, snapshot)
            emitted.extend(cmds)
        except StarlarkTimeout:
            # A runaway script — no point probing the other three values (each
            # would burn the full cap and blow the caller's request timeout).
            return [], "Skripta prekoračuje vremenski limit (moguća beskonačna petlja)."
        except Exception as exc:
            log.debug("script check failed", exc_info=True)
            run_errs.append(f"event={val}: {exc}")
    if len(run_errs) == len(_PROBE_VALUES):  # errored on every input → genuinely broken
        return [], "Izvršavanje:\n" + "\n".join(run_errs[:2])
    # both dry-runs (event True/False) usually emit the same command — dedupe once
    unique = {(t[0], t[1], t[2], json.dumps(t[3], sort_keys=True)): t for t in emitted}
    return list(unique.values()), None


async def _invalid_commands(engine: AutomationEngine, emitted: list) -> list[str]:
    """The entity must exist AND expose the capability, then the command/args must
    be legal for it — so a typo'd entity_id (the #1 Starlark mistake) is caught,
    not just a bad command."""
    crows = await engine._pool.fetch("SELECT entity_id, capabilities FROM entities")
    caps_by_ent = {r["entity_id"]: set(r["capabilities"] or []) for r in crows}
    bad: list[str] = []
    for (eid, cap, cmd, args) in emitted:
        ecaps = caps_by_ent.get(eid)
        if ecaps is None:
            bad.append(f"{eid}: unknown entity")
        elif cap not in ecaps:
            bad.append(f"{eid}: no capability '{cap}'")
        else:
            try:
                await prepare_command(engine._pool, eid, cap, cmd, args, source="")
            except CapabilityError as exc:
                bad.append(f"{eid} · {cmd}: {exc}")
    return bad


async def _check_script(engine: AutomationEngine, data: bytes) -> dict:
    """Compile + dry-run a Starlark script and return the verdict — the editor's
    "Check" button and AI-Starlark drafting call this. run() COLLECTS the commands
    the script would emit without publishing them, so no device is driven; each is
    then validated against the capability model, catching a missing entity / bad
    command before the rule ever fires."""
    try:
        req = json.loads(data)
        script = str(req["script"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid request"}
    # 1) compile — syntax only (in-process parse is safe; it never executes).
    #    The sandboxed dry-run below recompiles + RUNS the script in a worker.
    try:
        await asyncio.to_thread(compile_script, script)
    except StarlarkError as exc:
        return {"ok": False, "error": f"Sintaksa: {exc}"}
    # 2) dry-run against the live snapshot (run() never publishes)
    emitted, error = await _dry_run(engine, script, req.get("trigger") or {})
    if error:
        return {"ok": False, "error": error}
    # 3) validate every would-be command
    bad = await _invalid_commands(engine, emitted)
    if bad:
        return {"ok": False, "error": "Nevažeće komande:\n" + "\n".join(bad)}
    return {"ok": True, "commands": list(dict.fromkeys(f"{eid} · {cmd}" for (eid, _c, cmd, _a) in emitted))}


async def _on_check(engine: AutomationEngine, msg) -> None:
    verdict = await _check_script(engine, msg.data)
    if msg.reply:
        await msg.respond(json.dumps(verdict).encode())


async def _on_run(engine: AutomationEngine, msg) -> None:
    try:
        rid = int(json.loads(msg.data)["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return
    rule = next((r for r in engine._rules if r.id == rid), None)
    if rule is None:
        log.warning("run-now: rule id %s not loaded/enabled", rid)
        return
    log.info("run-now: %r (id=%s)", rule.name, rule.id)
    spawn(engine.run_now(rule), log=log, name=f"run-now {rule.name}")


async def _boot(engine: AutomationEngine) -> None:
    await engine._starlark.start()  # warm the sandbox worker pool before consuming
    await engine.reload()
    # Seed last-values BEFORE consuming so boot-time re-announces aren't seen as
    # transitions, then re-arm any holds that were mid-countdown at shutdown.
    await engine.seed_last_values()
    await engine.refresh_reachability()
    await engine._manual.refresh()
    await engine._manual.load_held()
    await engine.reconstruct_holds()
    await engine.reconstruct_schedule_triggers()
    # Computed helpers AFTER the snapshot is seeded, so their first eval sees real
    # state. Their published values also prime the cache before consuming the stream.
    await engine.reload_helpers()


async def _refresh_rules(engine: AutomationEngine) -> None:
    try:
        await engine.reload()
        await engine.reload_helpers()
        await engine.refresh_reachability()
        await engine._manual.refresh()
    except Exception:
        log.exception("rule reload failed; keeping previous set")


async def _heating_pass(heating: HeatingController) -> None:
    try:
        await heating.reload()
        await heating.tick()
    except Exception:
        log.exception("heating pass failed; boiler and valves keep their state")


async def _drain(engine: AutomationEngine) -> None:
    # Cancel pending holds, give in-flight firings a moment to finish — closing
    # the bus/pool under them just turns shutdown into a stack-trace dump.
    for task in engine._pending.values():
        task.cancel()
    for _ in range(20):  # up to ~2 s
        if not engine._firing:
            break
        await asyncio.sleep(0.1)
    await engine._starlark.close()


async def main() -> None:
    bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-automation", user=CORE)
    pool = await pg_pool(max_size=8, init=jsonb_init)
    await apply_migrations(pool)  # ensure schema before reload() reads it
    await bus.connect()
    attach_log_bus(bus, "automation")
    engine = AutomationEngine(bus, pool)
    await _boot(engine)
    # Durable JetStream consumer: events missed while down are replayed instead of
    # silently skipped. Note the two different guarantees this buys: the last-value
    # CACHE converges at-least-once (a replay always updates it), but ACTIONS are
    # at-most-once — on_event acks the event and spawns _fire fire-and-forget, so a
    # crash between the ack and publish_command drops that one firing (acceptable:
    # re-running a physical action late is worse than skipping it). The staleness
    # gate in on_event caps this — a trigger older than 60s replays into the cache
    # only, never fires — so a deploy can't unleash a burst of stale ghost actions.
    await bus.ensure_streams()
    consumer = await bus.consume_events_stream(engine.on_event, durable="automation")
    await bus.nc.subscribe(RUN_SUBJECT, cb=functools.partial(_on_run, engine))
    await bus.nc.subscribe(CHECK_SUBJECT, cb=functools.partial(_on_check, engine))

    # The heating controller shares this process (state cache, bus, tick) but not its
    # fate: every pass is guarded, so a heating fault can never stall rule firing.
    heating = HeatingController(bus, pool, lambda: engine._unreachable)
    await heating.reload()
    log.info("automation up — %d rule(s) loaded, consuming dida.events", len(engine._rules))

    health = HealthMarker("dida", "automation")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    last_reload = loop.time()
    last_heating = 0.0
    while not stop.is_set():
        health.touch()
        now = loop.time()
        if now - last_reload >= REFRESH_INTERVAL:
            await _refresh_rules(engine)
            last_reload = now
        try:
            await engine._manual.sweep(engine._snapshot())
        except Exception:
            log.exception("manual hold sweep failed; holds stay as they are")
        if now - last_heating >= HEATING_INTERVAL:
            await _heating_pass(heating)
            last_heating = now
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)

    log.info("shutting down")
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await _drain(engine)
    await bus.close()
    await pool.close()


if __name__ == "__main__":
    run_service(main())
