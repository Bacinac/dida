"""The assistant must not be a side door into the house.

Every other way of touching a device passes a permission check written in code.
This one passes a language model first. That inverts the usual assumption: the
caller is not a program with fixed behaviour, it is a text generator that will be
asked, sooner or later, to turn on a light it is not allowed to touch — sometimes
by a curious teenager, sometimes by a guest reading the tool list out of the
system prompt. A rule expressed in the prompt is a request; only a check in the
tool handler is a boundary.

So the property under test is not "the assistant behaves well". It is: for every
tool, the same boundary as the equivalent REST route, enforced in the handler.
The table below is the whole point of the suite — a NEW tool that nobody
classified fails here, which is the only version of this test that keeps working
after everyone has forgotten it exists.

Two boundaries, deliberately different:
  * A hidden entity answers "unknown device", never "you may not control that" —
    a refusal that names the device confirms the device.
  * A visible one the user may not operate says exactly that, because pretending
    it doesn't exist would make a legitimate view-only account unusable.
"""

from __future__ import annotations

import json

import pytest
from dida_api import assistant as mod
from dida_api import commands, permissions
from dida_api.auth import AuthUser

ADMIN = AuthUser(id=1, username="marko", role="admin")
GUEST = AuthUser(id=2, username="gost", role="user")


# Every tool the model can name, and what its handler owes the caller.
#   admin   — refuses a non-admin outright (the REST equivalent requires admin)
#   filter  — answers everyone, with entities the caller may not see removed
#   entity  — acts on one named entity, so it checks hidden + control
#   open    — no per-user surface at all; state this deliberately, don't default to it
TOOL_BOUNDARY = {
    "list_entities": "filter",
    "get_state": "filter",
    "command_log": "admin",
    "send_command": "entity",
    "query_history": "entity",
    "recall_scene": "entity",
    "create_automation": "admin",
    "list_automations": "admin",
    "set_automation_enabled": "admin",
    "run_automation": "admin",
    "explain_automation": "admin",
    "automation_runs": "admin",
    "list_alerts": "open",
    "list_scenes": "open",
    "explain_dida": "open",
}

ADMIN_ONLY_CALLS = {
    "command_log": {},
    "create_automation": {"name": "x", "description": "y"},
    "list_automations": {},
    "set_automation_enabled": {"automation_id": 1, "enabled": False},
    "run_automation": {"automation_id": 1},
    "explain_automation": {"automation_id": 1},
    "automation_runs": {},
}


class _Pool:
    def __init__(self, rows=None, val=None, row=None) -> None:
        self._rows, self._val, self._row = rows or [], val, row
        self.queries: list[str] = []

    async def fetch(self, sql, *a):
        self.queries.append(sql)
        return list(self._rows)

    async def fetchval(self, sql, *a):
        return self._val

    async def fetchrow(self, sql, *a):
        return self._row

    async def execute(self, sql, *a):
        return None


class _CH:
    def __init__(self, rows) -> None:
        self.rows, self.queries = rows, []

    async def query(self, sql, parameters=None):
        self.queries.append((sql, parameters or {}))
        return type("R", (), {"result_rows": self.rows})()


class _Bus:
    def __init__(self) -> None:
        self.commands: list = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)


def _ctx(user, *, pool=None, ch=None, bus=None):
    return mod.AssistantCtx(client=None, pool=pool or _Pool(), bus=bus or _Bus(),
                            ch=ch, user=user)


@pytest.fixture(autouse=True)
def boundary(monkeypatch):
    """Hidden: `secret:*`. Uncontrollable: `look:*`. Everything else is fair game."""
    async def _is_hidden(pool, user, eid):
        return eid.startswith("secret:")

    async def _hidden_ids(pool, user):
        return {"secret:vault"}

    async def _can_view(pool, user, eid):
        return not await _is_hidden(pool, user, eid)

    async def _can_control(pool, user, eid, cap):
        # A hidden entity is normally uncontrollable too, and it has to be modelled
        # that way here or the ORDER of the two checks stops mattering: with a
        # hidden-but-controllable fixture, checking control first still ends up
        # saying "unknown device", and the test passes against the leaky order.
        return not eid.startswith(("look:", "secret:"))
    monkeypatch.setattr(mod, "is_hidden", _is_hidden)
    monkeypatch.setattr(mod, "hidden_entity_ids", _hidden_ids)
    monkeypatch.setattr(commands, "can_view_entity", _can_view)
    monkeypatch.setattr(permissions, "can_control_entity", _can_control)

    async def _opts(pool):
        return {}
    monkeypatch.setattr(mod, "_fetch_options", _opts)


# --- the table is the guard ----------------------------------------------------


def test_every_tool_the_model_can_name_is_classified():
    """A tool added without a line in TOOL_BOUNDARY fails here. That is the entire
    reason this table exists rather than a list of individual tests: the next tool
    is the one nobody will remember to gate."""
    offered = {t["name"] for t in mod.TOOLS}
    assert offered == set(TOOL_BOUNDARY), (
        f"unclassified: {offered - set(TOOL_BOUNDARY)}; "
        f"stale: {set(TOOL_BOUNDARY) - offered}")


def test_every_offered_tool_is_actually_dispatched():
    """A tool in the schema with no handler is an error message the model earns
    after spending a full round of the loop on it."""
    dispatched = set(mod._TOOL_HANDLERS) | {"explain_dida"}
    for name in TOOL_BOUNDARY:
        assert name in dispatched, f"{name} is offered but never dispatched"


def test_the_admin_only_table_matches_the_boundary_table():
    assert set(ADMIN_ONLY_CALLS) == {k for k, v in TOOL_BOUNDARY.items() if v == "admin"}


# --- admin-only tools ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADMIN_ONLY_CALLS))
async def test_an_admin_only_tool_refuses_a_non_admin(name):
    out = json.loads(await mod._execute_tool(_ctx(GUEST), name, ADMIN_ONLY_CALLS[name]))
    assert "error" in out, f"{name} answered a non-admin"
    assert "administrator" in out["error"], f"{name}: {out['error']}"


async def test_the_refusal_happens_before_the_database_is_touched():
    """Refusing after the query has run leaks through timing and through the log,
    and on create_automation it would have spent an Opus call first."""
    pool = _Pool()
    await mod._execute_tool(_ctx(GUEST, pool=pool), "list_automations", {})
    assert pool.queries == []


async def test_an_admin_is_not_blocked_by_the_same_gate():
    """A guard that refuses everyone is not a boundary, it is an outage."""
    pool = _Pool(rows=[])
    out = json.loads(await mod._execute_tool(_ctx(ADMIN, pool=pool), "list_automations", {}))
    assert out == []


async def test_an_internal_caller_with_no_user_is_not_restricted():
    """The rule editor drives the same authoring functions with `user=None`. That
    path never exposes tools, so it is admin-equivalent by design — stated here so
    it stays a decision rather than becoming a hole."""
    assert mod.AssistantCtx(client=None, pool=_Pool(), bus=_Bus(), ch=None,
                            user=None)._restricted is False


# --- the one that turns things on ----------------------------------------------


async def test_a_hidden_device_is_unknown_not_forbidden():
    """"You may not control that" confirms it exists. For an entity the user is not
    allowed to see, that is the leak the hiding was for."""
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST), "send_command",
        {"entity_id": "secret:vault", "capability": "on_off", "command": "turn_on"}))
    assert "unknown device" in out["error"]
    assert "permission" not in out["error"]


async def test_a_visible_device_the_user_may_not_operate_says_so():
    """The opposite case: a view-only account must be told why, or it reads as a
    broken assistant."""
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST), "send_command",
        {"entity_id": "look:tv", "capability": "on_off", "command": "turn_on"}))
    assert "permission" in out["error"]


async def test_a_refused_command_never_reaches_the_bus():
    """The check is worth nothing if it runs after the publish."""
    bus = _Bus()
    for eid in ("secret:vault", "look:tv"):
        await mod._execute_tool(
            _ctx(GUEST, bus=bus), "send_command",
            {"entity_id": eid, "capability": "on_off", "command": "turn_on"})
    assert bus.commands == []


async def test_an_allowed_command_is_published_and_names_who_asked():
    """The audit trail has to distinguish the assistant acting for a person from a
    person pressing a button — otherwise "why did the light come on" has no answer."""
    bus = _Bus()
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST, bus=bus), "send_command",
        {"entity_id": "light:kitchen", "capability": "on_off", "command": "turn_on"}))
    assert out["ok"] is True
    assert len(bus.commands) == 1
    assert bus.commands[0].source == "assistant:gost"


async def test_an_invalid_command_is_rejected_at_the_capability_boundary():
    """The model can emit anything. The same validator every other caller passes
    through catches it, and the loop sees a tool error rather than a 500."""
    out = json.loads(await mod._execute_tool(
        _ctx(ADMIN), "send_command",
        {"entity_id": "light:kitchen", "capability": "on_off", "command": "self_destruct"}))
    assert "error" in out
    assert "rejected (capability)" in out["error"]


async def test_scene_recall_without_a_user_fails_loud():
    """`apply_scene` resolves the per-entity boundary against a real user. With no
    user there is no boundary at all — recalling anyway would apply every device in
    the scene regardless of who asked."""
    out = json.loads(await mod._execute_tool(
        _ctx(None), "recall_scene", {"scene_id": 1}))
    assert "error" in out
    assert "authenticated" in out["error"]


# --- the reads that filter rather than refuse ----------------------------------


async def test_a_hidden_entity_is_absent_from_the_listing():
    rows = [
        {"entity_id": "light:kitchen", "name": "K", "adapter": "mqtt", "capabilities": ["on_off"],
         "device_type": "light", "room": "Kuhinja", "floor": "Prizemlje"},
        {"entity_id": "secret:vault", "name": "V", "adapter": "mqtt", "capabilities": ["on_off"],
         "device_type": "lock", "room": "Kuhinja", "floor": "Prizemlje"},
    ]
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST, pool=_Pool(rows)), "list_entities", {"room": "Kuhinja"}))
    assert [e["entity_id"] for e in out] == ["light:kitchen"]


async def test_an_admin_sees_the_same_entity():
    """Proves the previous test measured the boundary and not an empty fixture."""
    rows = [{"entity_id": "secret:vault", "name": "V", "adapter": "mqtt",
             "capabilities": ["on_off"], "device_type": "lock", "room": "Kuhinja",
             "floor": "Prizemlje"}]
    out = json.loads(await mod._execute_tool(
        _ctx(ADMIN, pool=_Pool(rows)), "list_entities", {"room": "Kuhinja"}))
    assert [e["entity_id"] for e in out] == ["secret:vault"]


async def test_hidden_state_is_not_returned():
    rows = [{"entity_id": "secret:vault", "capability": "on_off", "value": True, "unit": None},
            {"entity_id": "light:kitchen", "capability": "on_off", "value": False, "unit": None}]
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST, pool=_Pool(rows)), "get_state", {"room": "Kuhinja"}))
    assert [e["entity_id"] for e in out] == ["light:kitchen"]


async def test_the_command_log_is_administrator_only():
    """It spans the whole house by default, and it names devices explicitly — the
    one read where forgetting the filter is a full inventory disclosure."""
    from datetime import UTC, datetime
    ts = datetime(2026, 8, 8, 3, 0, tzinfo=UTC)
    ch = _CH([(ts, "secret:vault", "on_off", "turn_on", "user:marko"),
              (ts, "light:kitchen", "on_off", "turn_off", "automation:noc")])
    out = json.loads(await mod._execute_tool(_ctx(GUEST, ch=ch), "command_log", {}))
    assert "administrator" in out["error"]
    assert ch.queries == []
    out = json.loads(await mod._execute_tool(_ctx(ADMIN, ch=ch), "command_log", {}))
    assert [r["entity_id"] for r in out] == ["secret:vault", "light:kitchen"]


async def test_the_history_of_a_hidden_entity_is_unknown_not_empty():
    """An empty series says "nothing happened"; the honest answer is that the
    device is not the caller's to ask about."""
    ch = _CH([])
    out = json.loads(await mod._execute_tool(
        _ctx(GUEST, ch=ch), "query_history",
        {"entity_id": "secret:vault", "capability": "on_off"}))
    assert out["error"] == "unknown device"
    assert ch.queries == [], "the query ran before the boundary was checked"


async def test_a_missing_history_store_is_reported_not_pretended_away():
    out = json.loads(await mod._execute_tool(
        _ctx(ADMIN, ch=None), "query_history",
        {"entity_id": "light:kitchen", "capability": "on_off"}))
    assert "unavailable" in out["error"]


async def test_a_stopped_alert_evaluator_says_so_rather_than_all_clear():
    """"No alerts" and "the watchdog is dead" are the same sentence to a reader and
    opposite facts."""
    out = json.loads(await mod._execute_tool(_ctx(ADMIN), "list_alerts", {}))
    assert "not running" in out["error"]


# --- what the model can put in the arguments -----------------------------------


@pytest.mark.parametrize("hours,expect", [(0, 1), (-5, 1), (10**9, 720), (48, 48)])
async def test_history_hours_are_clamped(hours, expect):
    """The model chooses these. An unclamped window is a full table scan on the
    history firehose, requested in natural language."""
    ch = _CH([])
    await mod._query_history(_ctx(ADMIN, ch=ch), "light:kitchen", "on_off", hours=hours)
    assert ch.queries[0][1]["h"] == expect


@pytest.mark.parametrize("limit,expect", [(0, 1), (10**6, 200), (40, 40)])
async def test_the_command_log_limit_is_clamped(limit, expect):
    ch = _CH([])
    await mod._command_log(_ctx(ADMIN, ch=ch), limit=limit)
    assert ch.queries[0][1]["n"] == expect


async def test_automation_run_history_is_clamped_too():
    pool = _Pool(rows=[])
    await mod._automation_runs(_ctx(ADMIN, pool=pool), limit=10**6)
    # The clamp is applied to the bound parameter, so assert on the call, not the SQL.
    assert pool.queries, "no query ran"


async def test_an_unfiltered_listing_answers_with_a_directory():
    """Measured at 433 entities: ~38k tokens, re-sent on every subsequent round of
    the loop. One careless call dominated the cost of the whole turn. Nothing is
    dropped silently — the totals are exact and the reply says how to get the rest."""
    rows = [{"entity_id": f"light:{i}", "name": str(i), "adapter": "mqtt",
             "capabilities": ["on_off"], "device_type": "light", "room": "Kuhinja",
             "floor": "Prizemlje"} for i in range(50)]
    out = json.loads(await mod._execute_tool(_ctx(ADMIN, pool=_Pool(rows)), "list_entities", {}))
    assert out["total"] == 50
    assert out["by_type"] == {"light": 50}
    assert "note" in out


async def test_the_directory_counts_only_what_the_caller_may_see():
    """A total that includes hidden devices leaks their number."""
    rows = [{"entity_id": "secret:vault", "name": "V", "adapter": "mqtt",
             "capabilities": ["on_off"], "device_type": "lock", "room": "K", "floor": "P"},
            {"entity_id": "light:kitchen", "name": "K", "adapter": "mqtt",
             "capabilities": ["on_off"], "device_type": "light", "room": "K", "floor": "P"}]
    out = json.loads(await mod._execute_tool(_ctx(GUEST, pool=_Pool(rows)), "list_entities", {}))
    assert out["total"] == 1
    assert "lock" not in out["by_type"]


# --- the loop must survive a broken tool ---------------------------------------


async def test_an_unknown_tool_name_is_an_error_not_a_crash():
    out = json.loads(await mod._execute_tool(_ctx(ADMIN), "rm_rf", {}))
    assert "unknown tool" in out["error"]


async def test_a_tool_that_raises_returns_an_error_the_loop_can_read():
    """A raise here would abort the turn mid-stream and the user would see a
    half-written sentence stop."""
    class _Boom(_Pool):
        async def fetch(self, sql, *a):
            raise RuntimeError("postgres is gone")
    out = json.loads(await mod._execute_tool(
        _ctx(ADMIN, pool=_Boom()), "list_entities", {"room": "K"}))
    assert "postgres is gone" in out["error"]


async def test_unexpected_arguments_do_not_escape_as_a_crash():
    """The model emits the arguments. A stray key becomes a TypeError on **kwargs."""
    out = json.loads(await mod._execute_tool(
        _ctx(ADMIN), "get_state", {"entity_id": "light:kitchen", "nonsense": 1}))
    assert "error" in out
