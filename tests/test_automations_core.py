"""Unit tests for the Layer-1 typed automation model (dida_core.automations).

PURE — validation/evaluation over in-memory msgspec Structs; no Postgres, no bus.
Runs in any DIDA image (dida_core installed); wired into run.sh's first (api-image,
--cov=dida_core) group — NO ephemeral Postgres needed.

Complements test_core_semantics.py (which already covers compare()'s bool-vs-number
strictness and a few validate_definition() dead-shape rejections) and
test_automation_transitions.py (the *engine's* transition/hold behaviour). Here we
drive the CORE pure functions directly: the remaining
compare() operators, eval_condition() leaves + and/or/not groups, trigger_matches(),
and the uncovered validate_definition() paths — to=None capability probing across
every value type, the Starlark layer, typed-rule rejections, and condition
(leaf + group) validation including the numeric-ordering guard.
"""
import pytest
from dida_core.automations import (
    AutomationDef,
    Condition,
    Trigger,
    compare,
    eval_condition,
    trigger_matches,
    validate_definition,
)
from dida_core.capabilities import CapabilityError


def test_triggerless_and_pre_migration_shapes_are_rejected():
    assert AutomationDef().triggers == [], "no trigger at all -> empty"
    with pytest.raises(CapabilityError, match="no trigger"):
        validate_definition({"actions": []})
    # The pre-migration singular key is no longer a trigger. msgspec drops the
    # unknown field, so such a rule must be REJECTED rather than stored disarmed —
    # 0064 folds those rows before this code ever sees them.
    with pytest.raises(CapabilityError, match="no trigger"):
        validate_definition({"trigger": {"entity_id": "mqtt:a", "capability": "occupancy"},
                             "actions": []})


def test_compare_ordering_and_equality_edges():
    # Operators/paths NOT exercised by test_core_semantics.
    assert compare("<=", 2, 2) is True
    assert compare("<=", 3, 2) is False
    assert compare(">", 5, 4) is True
    assert compare(">", 4, 5) is False
    assert compare("!=", 2, 3) is True, "numeric inequality holds"
    assert compare("!=", 2, 2) is False
    assert compare("==", "on", "on") is True, "string equality"
    assert compare("!=", "on", "off") is True
    assert compare("<", 1, "x") is False, "mixed number/string ordering is not-met"
    assert compare("??", 1, 2) is False, "an unknown operator is not-met"


def test_eval_condition_leaf():
    snap = {("mqtt:pir", "occupancy"): True, ("s:temp", "temperature"): 21.0}
    met = Condition(entity_id="mqtt:pir", capability="occupancy", op="==", value=True)
    assert eval_condition(met, snap) is True
    unmet = Condition(entity_id="mqtt:pir", capability="occupancy", op="==", value=False)
    assert eval_condition(unmet, snap) is False
    absent = Condition(entity_id="mqtt:gone", capability="occupancy", op="==", value=True)
    assert eval_condition(absent, snap) is False, "entity missing from snapshot -> not met"
    novalue = Condition(entity_id="mqtt:pir", capability="occupancy", op="==", value=None)
    assert eval_condition(novalue, snap) is False, "a None comparison value -> not met"
    warm = Condition(entity_id="s:temp", capability="temperature", op=">", value=20)
    assert eval_condition(warm, snap) is True, "numeric ordering leaf"


def test_eval_condition_groups():
    snap = {("a", "occupancy"): True, ("b", "contact"): False}
    la = Condition(entity_id="a", capability="occupancy", op="==", value=True)   # met
    lb = Condition(entity_id="b", capability="contact", op="==", value=True)     # unmet
    assert eval_condition(Condition(kind="and", conditions=[la, lb]), snap) is False
    assert eval_condition(Condition(kind="or", conditions=[la, lb]), snap) is True
    assert eval_condition(Condition(kind="not", conditions=[lb]), snap) is True, "NOT(false)=true"
    assert eval_condition(Condition(kind="not", conditions=[la]), snap) is False, "NOT(true)=false"
    assert eval_condition(Condition(kind="xor", conditions=[la]), snap) is False, "unknown kind -> not met"


def test_trigger_matches():
    t = Trigger(entity_id="mqtt:door", capability="contact", to=True)
    assert trigger_matches(t, "mqtt:door", "contact", True) is True
    assert trigger_matches(t, "mqtt:door", "contact", False) is False, "`to` set, value differs"
    assert trigger_matches(t, "mqtt:other", "contact", True) is False, "wrong entity"
    assert trigger_matches(t, "mqtt:door", "motion", True) is False, "wrong capability"
    any_t = Trigger(entity_id="mqtt:door", capability="contact")  # to=None -> any value fires
    assert trigger_matches(any_t, "mqtt:door", "contact", False) is True


def _rule(trigger_cap):
    """A minimal valid typed rule whose trigger has NO `to` (forces the probe path)."""
    return {
        "triggers": [{"entity_id": "x:sensor", "capability": trigger_cap}],
        "actions": [{"entity_id": "x:light", "capability": "on_off", "command": "turn_on"}],
    }


def test_validate_definition_to_none_probes_each_value_type():
    # to=None => _probe_value synthesises a VALID probe per value type, which
    # validate_state must then accept — one capability per branch:
    #   occupancy=bool, hvac_mode=choices, color_rgb=pattern, effect_options=json,
    #   person_count=numeric-with-minimum, measurement=numeric-no-minimum, location=plain-string.
    for cap in ("occupancy", "hvac_mode", "color_rgb", "effect_options",
                "person_count", "measurement", "location"):
        validate_definition(_rule(cap))  # raises if the synthesised probe fails to validate
    # an unknown capability with no `to` is still rejected (the probe's _spec lookup).
    with pytest.raises(CapabilityError):
        validate_definition(_rule("not_a_capability"))


def test_validate_definition_starlark_layer():
    ok = {
        "triggers": [{"entity_id": "x:pir", "capability": "occupancy", "to": True}],
        "script": "log('hi')",
    }
    defn = validate_definition(ok)
    assert defn.script and not defn.actions, "a script-only rule is accepted as Layer 2"
    # a Starlark rule must NOT also carry typed actions/conditions (layers stay separate).
    with pytest.raises(CapabilityError):
        validate_definition({
            **ok,
            "actions": [{"entity_id": "x:l", "capability": "on_off", "command": "turn_on"}],
        })


def test_validate_definition_typed_rejections():
    base_trig = {"entity_id": "x:pir", "capability": "occupancy", "to": True}
    good_action = {"entity_id": "x:l", "capability": "on_off", "command": "turn_on"}

    with pytest.raises(CapabilityError):  # no trigger at all
        validate_definition({"actions": [good_action]})
    with pytest.raises(CapabilityError):  # typed rule requires actions
        validate_definition({"triggers": [base_trig], "actions": []})
    with pytest.raises(CapabilityError):  # finite but negative hold
        validate_definition({"triggers": [{**base_trig, "for_seconds": -1.0}], "actions": [good_action]})
    with pytest.raises(CapabilityError):  # non-finite action delay
        validate_definition({"triggers": [base_trig],
                             "actions": [{**good_action, "delay_ms": float("inf")}]})
    with pytest.raises(CapabilityError):  # unknown command for the capability
        validate_definition({"triggers": [base_trig],
                             "actions": [{**good_action, "command": "explode"}]})
    with pytest.raises(CapabilityError):  # malformed shape (entity_id must be str) -> ValidationError
        validate_definition({"triggers": [{"entity_id": 123, "capability": "occupancy", "to": True}],
                             "actions": [good_action]})


def _typed(conditions):
    return {
        "triggers": [{"entity_id": "x:pir", "capability": "occupancy", "to": True}],
        "conditions": conditions,
        "actions": [{"entity_id": "x:l", "capability": "on_off", "command": "turn_on"}],
    }


def test_validate_definition_conditions_valid():
    leaf = {"entity_id": "x:pir", "capability": "occupancy", "op": "==", "value": True}
    numeric = {"entity_id": "x:dim", "capability": "brightness", "op": ">", "value": 50}
    group = {"kind": "or", "conditions": [leaf, {"kind": "not", "conditions": [numeric]}]}
    # A set_ action too, to also exercise the command-args value validation branch.
    raw = _typed([leaf, numeric, group])
    raw["actions"].append({"entity_id": "x:dim", "capability": "brightness",
                           "command": "set_brightness", "args": {"value": 40}})
    defn = validate_definition(raw)
    assert len(defn.conditions) == 3, "leaf + numeric-ordering + or/not group all validate"


def test_validate_definition_conditions_rejections():
    def bad_cond(c):
        with pytest.raises(CapabilityError):
            validate_definition(_typed([c]))

    bad_cond({"entity_id": "", "capability": "occupancy", "op": "==", "value": True})   # empty leaf id
    bad_cond({"entity_id": "x", "capability": "occupancy", "op": "@@", "value": True})  # bad operator
    bad_cond({"entity_id": "x", "capability": "occupancy", "op": "==", "value": None})  # no value
    bad_cond({"entity_id": "x", "capability": "occupancy", "op": "==", "value": True,
              "conditions": [{"entity_id": "y", "capability": "contact", "op": "==", "value": True}]})  # leaf nests
    bad_cond({"entity_id": "x", "capability": "on_off", "op": ">", "value": True})      # ordering on non-numeric
    bad_cond({"kind": "xor", "conditions": [
        {"entity_id": "x", "capability": "occupancy", "op": "==", "value": True}]})     # bad group kind
    bad_cond({"kind": "and", "conditions": []})                                         # empty group


def test_cooldown_seconds_validates_and_defaults_off():
    assert validate_definition(_typed([])).cooldown_seconds is None, "absent = no cooldown"
    assert validate_definition({**_typed([]), "cooldown_seconds": 300}).cooldown_seconds == 300
    assert validate_definition({**_typed([]), "cooldown_seconds": 0}).cooldown_seconds == 0
    for bad in (-1, float("nan"), float("inf")):
        with pytest.raises(CapabilityError, match="cooldown_seconds"):
            validate_definition({**_typed([]), "cooldown_seconds": bad})
