"""The automation service's pure guards — each one stands between a rule and a
failure that reports success.

`automation/__main__.py` is 554 statements at 40%, and what is missing includes
two decisions that are pure functions of their input, need no bus and no
database, and both fail the same way when wrong: the rule says it fired.

  * a condition tree's entity list drives the state snapshot the conditions are
    then evaluated against, so an entity missed inside an and/or/not group is
    evaluated against no value at all;
  * a computed helper picks the first branch whose conditions hold. Get the order
    or the fallthrough wrong and the house runs on a value nobody chose.

The fourth is a guard rather than a decision: a helper that reads another computed
value would be a loop, and while the API refuses it on write, a definition inserted
straight into the database must not get through either.
"""

from __future__ import annotations

from dida_automation.__main__ import (
    Helper,
    _condition_entities,
)
from dida_core.automations import Condition

# --- which entities a condition tree reads ------------------------------------


def _leaf(eid, cap="on_off", op="==", value=True):
    return Condition(entity_id=eid, capability=cap, op=op, value=value)


def test_a_flat_condition_list_names_its_entities():
    got = _condition_entities([_leaf("mqtt:a"), _leaf("mqtt:b")])
    assert set(got) == {"mqtt:a", "mqtt:b"}


def test_entities_inside_an_OR_group_are_found():
    """The snapshot is fetched from this list. Miss one and the condition is
    evaluated against a value that was never read — silently false."""
    tree = [Condition(kind="or", conditions=[_leaf("mqtt:a"), _leaf("mqtt:b")])]
    assert set(_condition_entities(tree)) == {"mqtt:a", "mqtt:b"}


def test_entities_nested_TWO_groups_deep_are_found():
    tree = [Condition(kind="and", conditions=[
        Condition(kind="or", conditions=[_leaf("mqtt:a"), _leaf("mqtt:b")]),
        Condition(kind="not", conditions=[_leaf("mqtt:c")]),
    ])]
    assert set(_condition_entities(tree)) == {"mqtt:a", "mqtt:b", "mqtt:c"}


def test_the_same_entity_twice_is_listed_once():
    """The list becomes an IN clause; duplicates are wasted work, not a bug — but a
    changed shape here would show up as one."""
    got = _condition_entities([_leaf("mqtt:a", "on_off"), _leaf("mqtt:a", "brightness")])
    assert got == ["mqtt:a"]


def test_a_group_with_no_entity_contributes_nothing():
    assert _condition_entities([Condition(kind="and", conditions=[])]) == []


# --- computed helpers ---------------------------------------------------------


# The operator is the SYMBOL, not a word: `compare` handles "=="/"!=" and the
# ordering ops, and anything else falls through to False. A first draft wrote "eq",
# which made every branch silently unmet — the tests failed immediately, which is
# the difference between a wrong fixture and a wrong fixture that still passes.
def _helper(branches, default=None, entity_id="helper:test"):
    return Helper(entity_id, "Test", "boolean",
                  {"branches": branches, "default": default})


def test_the_FIRST_matching_branch_wins():
    """Order is the author's intent — evaluating them all and taking the last would
    invert every helper written as most-specific-first."""
    h = _helper([
        {"conditions": [{"entity_id": "mqtt:a", "capability": "on_off",
                         "op": "==", "value": True}], "value": "first"},
        {"conditions": [], "value": "catch-all"},
    ], default="fallback")
    assert h.compute({("mqtt:a", "on_off"): True}) == "first"


def test_a_branch_whose_conditions_fail_is_skipped():
    h = _helper([
        {"conditions": [{"entity_id": "mqtt:a", "capability": "on_off",
                         "op": "==", "value": True}], "value": "on"},
        {"conditions": [], "value": "catch-all"},
    ], default="fallback")
    assert h.compute({("mqtt:a", "on_off"): False}) == "catch-all"


def test_with_no_branch_matching_the_DEFAULT_is_used():
    """Not None: a helper that goes null mid-evaluation takes every rule reading it
    down with it."""
    h = _helper([
        {"conditions": [{"entity_id": "mqtt:a", "capability": "on_off",
                         "op": "==", "value": True}], "value": "on"},
    ], default="fallback")
    assert h.compute({("mqtt:a", "on_off"): False}) == "fallback"


def test_a_branch_needs_ALL_its_conditions(rig=None):
    h = _helper([
        {"conditions": [
            {"entity_id": "mqtt:a", "capability": "on_off", "op": "==", "value": True},
            {"entity_id": "mqtt:b", "capability": "on_off", "op": "==", "value": True},
        ], "value": "both"},
    ], default="no")
    assert h.compute({("mqtt:a", "on_off"): True, ("mqtt:b", "on_off"): False}) == "no"
    assert h.compute({("mqtt:a", "on_off"): True, ("mqtt:b", "on_off"): True}) == "both"


def test_the_helpers_INPUTS_cover_every_status_it_reads():
    """Inputs decide what the engine subscribes to. A status missing from this set
    means the helper never recomputes when it changes — it holds a stale value
    forever, and nothing reports a fault."""
    h = _helper([
        {"conditions": [
            {"kind": "or", "conditions": [
                {"entity_id": "mqtt:a", "capability": "on_off", "op": "==", "value": True},
                {"entity_id": "mqtt:b", "capability": "occupancy", "op": "==", "value": True},
            ]},
        ], "value": "yes"},
    ])
    assert ("mqtt:a", "on_off") in h.inputs
    assert ("mqtt:b", "occupancy") in h.inputs


def test_a_helper_that_reads_ANOTHER_computed_value_is_flagged():
    """Defense in depth: the API refuses this on write, but a definition inserted
    straight into the database would otherwise build a computation loop."""
    h = Helper("helper:loop", "Loop", "boolean",
               {"script": 'state("helper:other", "boolean")'})
    assert h.bad_ref == "helper:other"


def test_an_ordinary_reference_is_not_flagged():
    h = Helper("helper:ok", "Ok", "boolean",
               {"script": 'state("mqtt:kitchen", "occupancy")'})
    assert h.bad_ref is None
