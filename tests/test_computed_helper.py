"""Computed-helper evaluation — the typed condition→value branch logic and the
input/loop-safety bookkeeping (dida_automation.__main__.Helper). Pure (no bus, no
Postgres); runs in the automation image (where dida_automation is importable).

The whole point of the refactor: a helper reuses the automation Condition model +
eval_condition instead of a hand-rolled rule language, so `compute()` is just "first
branch whose conditions hold, else default".
"""
from dida_automation.__main__ import Helper


def _h(definition, capability="text"):
    return Helper("helper:t", "T", capability, definition)


def test_compute_first_matching_branch_then_default():
    # WHEN sun < -6 → "night"; ELSE "day". Reuses eval_condition, no code.
    h = _h({
        "branches": [{"conditions": [{"entity_id": "astro:sun", "capability": "sun_elevation",
                                      "op": "<", "value": -6}], "value": "night"}],
        "default": "day",
    })
    assert h.compute({("astro:sun", "sun_elevation"): -8}) == "night", "condition holds → branch value"
    assert h.compute({("astro:sun", "sun_elevation"): 12}) == "day", "no branch holds → default"
    assert h.compute({}) == "day", "missing input → condition not met → default"


def test_compute_branch_order_and_multiple_conditions():
    # First matching branch wins; a branch's conditions are AND-ed.
    h = _h({
        "branches": [
            {"conditions": [{"entity_id": "a:x", "capability": "motion", "op": "==", "value": True},
                            {"entity_id": "b:y", "capability": "occupancy", "op": "==", "value": True}],
             "value": "both"},
            {"conditions": [{"entity_id": "a:x", "capability": "motion", "op": "==", "value": True}],
             "value": "motion-only"},
        ],
        "default": "idle",
    })
    both = {("a:x", "motion"): True, ("b:y", "occupancy"): True}
    assert h.compute(both) == "both", "both conditions hold → first branch"
    assert h.compute({("a:x", "motion"): True}) == "motion-only", "only the second branch's AND holds"
    assert h.compute({}) == "idle", "neither → default"


def test_inputs_are_extracted_from_conditions_and_script():
    # The statuses a helper reads = every entity/cap in its conditions AND its script.
    h = _h({
        "branches": [{"conditions": [{"entity_id": "astro:sun", "capability": "sun_elevation",
                                      "op": "<", "value": -6}], "value": "night"}],
        "default": "day",
    })
    assert ("astro:sun", "sun_elevation") in h.inputs, "a branch condition's status is watched"

    hs = _h({"script": 'value = state("mqtt:x", "motion") or state("virtual:g", "on_off")'})
    assert ("mqtt:x", "motion") in hs.inputs and ("virtual:g", "on_off") in hs.inputs, \
        "a script's state() reads are watched"
    assert hs.script and not hs.branches, "an advanced helper has a script, no branches"


def test_reading_another_computed_value_is_flagged():
    # Defense in depth: a helper that reads helper:/derived: is a race/cycle risk and
    # is flagged (the evaluator refuses it; the API blocks it on write).
    h = _h({"branches": [{"conditions": [{"entity_id": "helper:other", "capability": "enum",
                          "op": "==", "value": "x"}], "value": 1}], "default": 0})
    assert h.bad_ref == "helper:other", "a helper-reading branch is caught"
    hd = _h({"script": 'value = state("derived:z", "boolean")'})
    assert hd.bad_ref == "derived:z", "a derived-reading script is caught"
    ok = _h({"script": 'value = state("mqtt:x", "motion")'})
    assert ok.bad_ref is None, "reading a raw status is fine"


async def test_a_slower_older_evaluation_cannot_overwrite_a_newer_value():
    """A helper is re-evaluated per input event and evaluation AWAITS (the Starlark
    worker round-trip), so a burst starts several at once. The older one must not
    publish its stale result on top of the newer — that value would stick, and fire
    consumers' `to=` triggers with the regression, until the next input change."""
    import asyncio

    from dida_automation.__main__ import AutomationEngine

    eng = AutomationEngine.__new__(AutomationEngine)
    eng._helper_seq = {}
    eng._published = {}
    eng._last = {}
    eng._unreachable = frozenset()
    published: list = []

    class FakeBus:
        async def publish_state(self, upd):
            published.append(upd.value)

    class FakePool:
        async def execute(self, *_a, **_k):
            return None

    eng._bus, eng._pool = FakeBus(), FakePool()

    gate = asyncio.Event()

    class FakeStarlark:
        """First call (the OLD evaluation) parks until released; the second returns at once."""
        def __init__(self):
            self.n = 0

        async def run_value(self, _script, _snapshot):
            self.n += 1
            if self.n == 1:
                await gate.wait()
                return "OLD"
            return "NEW"

    eng._starlark = FakeStarlark()
    helper = _h({"script": 'value = state("x","y")'})
    helper.bad_ref = None

    slow = asyncio.create_task(eng._eval_helper(helper))   # #1: takes seq 1, parks
    await asyncio.sleep(0)
    await eng._eval_helper(helper)                          # #2: takes seq 2, publishes NEW
    gate.set()
    await slow                                              # #1 wakes with its stale result
    assert published == ["NEW"], f"the stale evaluation must be dropped, got {published}"


async def test_a_helper_that_watches_nothing_is_refused_loudly():
    """The trap that costs an afternoon: state() with the id in a LOOP VARIABLE is
    invisible to the input scan, so the helper watches nothing, never recomputes, and
    the alert built on it never fires — silently. It must land as a visible error, not
    as a frozen value that looks alive."""
    from dida_automation.__main__ import AutomationEngine

    engine = AutomationEngine.__new__(AutomationEngine)
    engine._helper_seq = {}
    engine._published = {}
    engine._last = {}
    engine._unreachable = frozenset()
    errors: list[tuple] = []

    class _Pool:
        async def execute(self, _sql, *args):
            errors.append(args)

    engine._pool = _Pool()
    looped = _h({"script": 'need = False\nfor eid in ["a:x", "b:y"]:\n    if state(eid, "motion"):\n'
                           '        need = True\nvalue = need'})
    assert looped.inputs == set(), "the scan cannot see a variable id — that is the trap"
    await engine._eval_helper(looped)
    assert errors and "recompute" in errors[0][1], f"expected a loud error, got {errors}"

    watched = _h({"script": 'value = state("a:x", "motion")'})
    assert watched.inputs, "the literal form is watched, and is not refused"
