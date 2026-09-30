"""Dangling entity references — finding them, and not crying wolf.

An entity_id is referenced from JSONB no foreign key protects: automation
definitions, scene states, area configs, schedule params. When the entity stops
existing, every rule that named it keeps loading, keeps evaluating, and never fires
again — no log line, no unhealthy container, just a house that quietly stopped
doing something. The scan exists to make that visible.

Two ways it can be useless, and both are tested here:

  * MISSING a reference. The Starlark scan knows a fixed list of API functions whose
    first argument is an entity. If that list drifts from what the sandbox actually
    registers, the scan silently stops covering whatever was added — worse than not
    scanning, because the report still says "clean".
  * INVENTING one. The first live run against production reported three working
    automations as broken, because `radio:tuner` is an api relay with no adapter and
    no registry row. A detector that is wrong once is a detector nobody opens again.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from dida_core import STARLARK_ENTITY_FUNCS, entity_references, starlark_references

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "services/automation/src/dida_automation/starlark_runtime.py"


def test_a_typed_automation_names_its_entities():
    definition = {
        "triggers": [{"entity_id": "mqtt:kitchen_motion", "capability": "occupancy"}],
        "conditions": [{"entity_id": "helper:quiet_hours"}],
        "actions": [{"entity_id": "denon:marantz_main", "command": "turn_on"}],
    }
    assert entity_references(definition) == {
        "mqtt:kitchen_motion", "helper:quiet_hours", "denon:marantz_main"}


def test_references_are_found_at_any_depth():
    """Area configs nest them two and three levels down."""
    body = {"rooms": [{"heating": {"entity_id": "mqtt:trv_bedroom"}},
                      {"media": {"players": [{"entity": "heos:marantz"}]}}]}
    assert entity_references(body) == {"mqtt:trv_bedroom", "heos:marantz"}


def test_a_picker_that_stores_a_LIST_is_read():
    assert entity_references({"entity_id": ["mqtt:a", "mqtt:b"]}) == {"mqtt:a", "mqtt:b"}


def test_capabilities_and_commands_are_not_mistaken_for_entities():
    """Everything in these structures sits next to an entity_id; only the value of
    the entity key is one, and only when it looks like `adapter:rest`."""
    body = {"entity_id": "mqtt:lamp", "capability": "on_off", "command": "turn_on",
            "args": {"value": "MUSIC"}}
    assert entity_references(body) == {"mqtt:lamp"}


def test_a_bare_word_under_an_entity_key_is_not_an_entity():
    """A half-written rule stores `{"entity_id": ""}` or a placeholder; reporting it
    as a missing device would be noise on a rule that is merely unfinished."""
    assert entity_references({"entity_id": ""}) == set()
    assert entity_references({"entity_id": "unset"}) == set()


def test_starlark_literals_are_found():
    script = (
        'if state("helper:quiet_hours", "boolean"):\n'
        '    turn_off("denon:marantz_main")\n'
        'else:\n'
        "    turn_on('mqtt:kitchen_light')\n"
        '    command("harmony:hub", "source", "set_source", "SHIELD TV")\n'
    )
    assert starlark_references(script) == {
        "helper:quiet_hours", "denon:marantz_main", "mqtt:kitchen_light", "harmony:hub"}


def test_an_automation_that_is_BOTH_typed_and_scripted_is_scanned_whole():
    """The common shape on this installation: layer-1 triggers with a Starlark body.
    Scanning only one half would report a rule as clean while the other half dangles."""
    body = {
        "triggers": [{"entity_id": "mqtt:remote_1"}],
        "script": 'turn_on("denon:marantz_main")',
    }
    assert entity_references(body) == {"mqtt:remote_1", "denon:marantz_main"}


def test_notify_is_not_scanned_for_an_entity():
    """Its first argument is a delivery target — a user or a topic, not a device.
    Treating it as one would report every notifying rule as broken."""
    assert starlark_references('notify("marko", "Title", "Message")') == set()


def test_the_scanned_function_list_matches_what_the_sandbox_REGISTERS():
    """The drift that would make the scan quietly incomplete.

    The sandbox registers its API with `module.add_callable(...)`. This reads those
    names out of the source and requires every one taking an entity first argument to
    be scanned — so adding a function to the sandbox without adding it here fails
    HERE, instead of silently narrowing what the report covers."""
    source = RUNTIME.read_text()
    registered = set(re.findall(r'add_callable\(\s*"([a-z_]+)"', source))
    assert registered, "could not read the sandbox API — did the registration change?"

    # `notify` is the one registered function whose first argument is not an entity.
    expected = registered - {"notify"}
    scanned = set(STARLARK_ENTITY_FUNCS)
    assert not (expected - scanned), \
        f"sandbox functions the scan would miss: {sorted(expected - scanned)}"
    assert not (scanned - registered), \
        f"scanned functions the sandbox does not have: {sorted(scanned - registered)}"


def test_the_api_relay_is_not_reported_as_missing():
    """`radio:tuner` has no adapter and no `entities` row by design — the api answers
    for it. The first live run called three working automations broken because of it."""
    from dida_api import radio_tuner
    from dida_api.orphans import API_RELAYS

    assert radio_tuner.TUNER in API_RELAYS


class _StubPool:
    """Just enough asyncpg to exercise find_orphans without a database.

    Rows come back under the names the REAL query asks for (`AS body`), not under
    the column names — a stub that answers a shape the code never requested tests
    the stub."""

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self._tables = tables

    async def fetch(self, sql: str, *args):
        for name, rows in self._tables.items():
            if f" {name}" in sql:
                return rows
        return []


@pytest.mark.asyncio
async def test_find_orphans_reports_the_rule_and_what_it_lost():
    from dida_api.orphans import find_orphans

    pool = _StubPool({
        "entities": [{"entity_id": "mqtt:kitchen_light"}],
        "computed_helpers": [{"entity_id": "helper:quiet_hours", "id": 1,
                              "name": "Quiet", "body": {}}],
        "virtual_entities": [],
        "automations": [
            {"id": 7, "name": "Evening", "body": {
                "actions": [{"entity_id": "mqtt:kitchen_light"},
                            {"entity_id": "mqtt:GONE"}]}},
            {"id": 8, "name": "Fine", "body": {
                "actions": [{"entity_id": "mqtt:kitchen_light"}]}},
        ],
        "scenes": [], "schedules": [], "areas": [],
    })
    found = await find_orphans(pool)
    assert [(o["name"], o["missing"]) for o in found] == [("Evening", ["mqtt:GONE"])], \
        "the working rule must not be reported, and the broken one must name what it lost"


# --- rewriting: the counterpart, and the ways it can quietly corrupt a rule ------


def test_a_rename_moves_every_reference():
    from dida_core import rewrite_references

    body = {
        "triggers": [{"entity_id": "mqtt:old"}],
        "actions": [{"entity_id": "denon:keep"}, {"entity_id": ["mqtt:old", "mqtt:other"]}],
        "script": 'turn_on("mqtt:old")',
    }
    out = rewrite_references(body, {"mqtt:old": "mqtt:new"})
    assert entity_references(out) == {"mqtt:new", "denon:keep", "mqtt:other"}


def test_the_rewriter_reaches_everywhere_the_finder_does():
    """The bug this prevents is a HALF rename: the report says the rule was fixed
    while one reference stayed behind and the rule stays dead. So whatever the
    finder can see, the rewriter must be able to move — asserted structurally, not
    by listing places twice."""
    from dida_core import rewrite_references

    body = {
        "triggers": [{"entity_id": "mqtt:old"}],
        "conditions": [{"entity": "mqtt:old"}],
        "actions": [{"entity_id": ["mqtt:old"]}],
        "nested": {"deep": {"entity_id": "mqtt:old"}},
        "script": 'state("mqtt:old", "on_off")\nturn_off("mqtt:old")',
    }
    assert "mqtt:old" in entity_references(body)
    out = rewrite_references(body, {"mqtt:old": "mqtt:new"})
    assert "mqtt:old" not in entity_references(out), "a reference was left behind"


def test_free_text_that_merely_MENTIONS_an_entity_is_not_rewritten():
    """A notify message routinely names the device it is about. A blind text
    substitution would edit the message, and the script would still look right."""
    from dida_core import rewrite_starlark

    script = ('turn_on("mqtt:old")\n'
              'notify("marko", "Alarm", "mqtt:old is on")\n'
              '# mqtt:old was renamed\n')
    out = rewrite_starlark(script, {"mqtt:old": "mqtt:new"})
    assert 'turn_on("mqtt:new")' in out
    assert '"mqtt:old is on"' in out, "the message text was rewritten"
    assert "# mqtt:old was renamed" in out, "a comment was rewritten"


def test_an_unmapped_reference_is_left_exactly_as_it_was():
    from dida_core import rewrite_references

    body = {"actions": [{"entity_id": "mqtt:untouched"}]}
    assert rewrite_references(body, {"mqtt:other": "mqtt:new"}) == body


def test_the_input_structure_is_not_mutated():
    """The caller holds the row it read from the database; mutating it in place
    would make a failed transaction leave a half-renamed object in memory."""
    from dida_core import rewrite_references

    body = {"actions": [{"entity_id": "mqtt:old"}]}
    rewrite_references(body, {"mqtt:old": "mqtt:new"})
    assert body["actions"][0]["entity_id"] == "mqtt:old"


def test_single_and_double_quoted_literals_are_both_moved():
    from dida_core import rewrite_starlark

    out = rewrite_starlark("turn_on('mqtt:old')\nturn_off(\"mqtt:old\")",
                           {"mqtt:old": "mqtt:new"})
    assert "'mqtt:new'" in out and '"mqtt:new"' in out
