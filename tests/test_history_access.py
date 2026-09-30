"""History and logs: what a narrow login may look back at.

Current state is gated per entity, which would be worth little if the past were
not. Someone fenced off from the bedroom's motion sensor today can otherwise ask
what it read yesterday and reconstruct the same picture a day late. So `/history`
applies the same gate as `/state`, and answers 404 rather than 403 — a refusal
that distinguishes "not yours" from "not there" confirms the sensor exists.

The batch routes make the opposite choice on purpose. A page rendering forty
device cards asks for forty sparklines in one round trip; refusing the batch
because one entity is fenced off would blank every chart on the page. They drop
the hidden ones and answer for the rest. Both behaviours are deliberate and both
are pinned here, because the natural instinct is to make them consistent.

Logs are admin-only and it is not a close call: lines quote hostnames, file
paths, failure messages from token checks, and whatever an adapter chose to
print. So is the command trail, which names which person told which device to do
what.
"""

from __future__ import annotations

import inspect

import pytest
from dida_api import history_routes as mod
from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.history import MAX_HOURS
from fastapi import HTTPException

ADMIN = AuthUser(id=1, username="marko", role="admin")
GOST = AuthUser(id=2, username="gost", role="user")


class _CH:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.queries: list[tuple[str, dict]] = []

    async def query(self, sql, parameters=None):
        self.queries.append((sql, parameters or {}))
        return type("R", (), {"result_rows": self.rows})()


class _Request:
    def __init__(self, ch=None) -> None:
        self.ch = _CH() if ch is None else ch
        self.app = type("A", (), {"state": type("S", (), {
            "pool": object(), "ch": self.ch})()})()


@pytest.fixture(autouse=True)
def boundary(monkeypatch):
    """`secret:*` is fenced off from a non-admin."""
    async def _can_view(pool, user, eid):
        return not (eid.startswith("secret:") and user.role != "admin")

    async def _hidden_for(pool, user):
        return {"secret:vault"} if user.role != "admin" else set()
    monkeypatch.setattr(mod, "can_view_entity", _can_view)
    monkeypatch.setattr(mod, "hidden_for", _hidden_for)

    async def _series(ch, eid, cap, hours, tz):
        return {"capability": cap, "points": []}

    async def _sparks(ch, pairs, hours):
        return {f"{e}|{c}": [] for e, c in pairs}

    async def _replay(ch, ids, frm, to):
        return {"entities": list(ids)}

    async def _last(ch, eid, cap):
        return {"value": None}
    monkeypatch.setattr(mod, "query_series", _series)
    monkeypatch.setattr(mod, "query_sparklines", _sparks)
    monkeypatch.setattr(mod, "build_replay", _replay)
    monkeypatch.setattr(mod, "last_nonempty", _last)


# --- one entity: refuse, and refuse the same way as /state ----------------------


async def test_the_past_of_a_fenced_off_entity_is_not_readable():
    """Gating the present and not the past leaks the same picture a day late."""
    with pytest.raises(HTTPException) as e:
        await mod.history(_Request(), entity_id="secret:vault",
                          capability="on_off", user=GOST)
    assert e.value.status_code == 404


async def test_the_refusal_does_not_confirm_the_entity_exists():
    """404, not 403 — the distinction between "not yours" and "not there" is
    itself the disclosure."""
    with pytest.raises(HTTPException) as e:
        await mod.history(_Request(), entity_id="secret:vault",
                          capability="on_off", user=GOST)
    assert e.value.status_code != 403


async def test_an_admin_reads_the_same_history():
    out = await mod.history(_Request(), entity_id="secret:vault",
                            capability="on_off", user=ADMIN)
    assert out["entity_id"] == "secret:vault"


async def test_last_nonempty_carries_the_same_gate():
    """It answers "what did this hold, and when did it stop" — the memory of a
    reading is the reading."""
    with pytest.raises(HTTPException) as e:
        await mod.history_last_nonempty(_Request(), entity_id="secret:vault",
                                        capability="occupancy", user=GOST)
    assert e.value.status_code == 404


async def test_a_history_request_naming_no_capability_is_refused():
    """Otherwise it is a request for every series the entity ever had."""
    with pytest.raises(HTTPException) as e:
        await mod.history(_Request(), entity_id="light:kitchen", user=GOST)
    assert e.value.status_code == 400


async def test_capabilities_are_deduplicated():
    """`capabilities=on_off,on_off&capability=on_off` would otherwise run the same
    ClickHouse query three times and return it three times."""
    out = await mod.history(_Request(), entity_id="light:kitchen",
                            capabilities="on_off,on_off", capability="on_off",
                            user=GOST)
    assert len(out["series"]) == 1


@pytest.mark.parametrize("hours,expect", [(0, 1), (-5, 1), (10**9, MAX_HOURS), (48, 48)])
async def test_the_window_is_clamped(hours, expect):
    """The caller picks this. Unclamped it is a full scan of the history firehose,
    requested from a browser."""
    out = await mod.history(_Request(), entity_id="light:kitchen",
                            capability="on_off", hours=hours, user=GOST)
    assert out["hours"] == expect


async def test_a_missing_history_store_is_reported_not_answered_empty():
    """An empty series says "nothing happened"; the honest answer is that the
    store is down, which is a different thing to go and fix."""
    req = _Request()
    req.app.state.ch = None
    with pytest.raises(HTTPException) as e:
        await mod.history(req, entity_id="light:kitchen", capability="on_off", user=GOST)
    assert e.value.status_code == 503


# --- the batch routes drop instead of refusing ----------------------------------


async def test_a_fenced_off_entity_is_dropped_from_a_sparkline_batch():
    """Deliberately NOT a 404: the caller is a page rendering what it can see, and
    one fenced-off device must not blank every chart on it."""
    out = await mod.history_sparklines(
        _Request(), mod.SparklineIn(pairs=[("light:kitchen", "on_off"),
                                           ("secret:vault", "on_off")]),
        user=GOST)
    assert "light:kitchen|on_off" in out
    assert not any(k.startswith("secret:") for k in out)


async def test_the_sparkline_batch_is_bounded():
    """A page has tens of cards. Anything larger is a fan-out the caller sized."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        mod.SparklineIn(pairs=[(f"e{i}", "on_off") for i in range(201)])


@pytest.mark.parametrize("hours", [0, 24 * 8])
def test_the_sparkline_window_is_bounded_by_the_model(hours):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        mod.SparklineIn(pairs=[], hours=hours)


async def test_replay_drops_fenced_off_entities_but_serves_the_rest():
    """The floor plan replays what it renders. A narrow-scoped login still gets a
    replay of its own surface."""
    out = await mod.history_replay(
        _Request(), mod.ReplayIn(frm=0, to=1, entities=["light:kitchen", "secret:vault"]),
        user=GOST)
    assert out["entities"] == ["light:kitchen"]


async def test_a_replay_of_nothing_visible_is_a_404_not_an_empty_bundle():
    """An empty bundle renders as a plan where nothing ever happened."""
    with pytest.raises(HTTPException) as e:
        await mod.history_replay(_Request(), mod.ReplayIn(frm=0, to=1, entities=["secret:vault"]),
                                 user=GOST)
    assert e.value.status_code == 404


async def test_replay_deduplicates_before_it_bounds():
    """Otherwise the cap is spent on repeats and the tail of a real plan is cut."""
    out = await mod.history_replay(
        _Request(), mod.ReplayIn(frm=0, to=1,
                                 entities=["light:kitchen", "light:kitchen"]),
        user=GOST)
    assert out["entities"] == ["light:kitchen"]


# --- logs and the command trail are admin-only ----------------------------------


@pytest.mark.parametrize("fn", ["history_commands", "app_logs", "app_log_services"])
def test_the_operator_surfaces_are_admin_only(fn):
    """Log lines quote hostnames, paths and failure messages; the command trail
    names which person told which device to do what."""
    param = inspect.signature(getattr(mod, fn)).parameters["_admin"]
    assert param.default.dependency is require_admin, f"{fn} is not admin-gated"


@pytest.mark.parametrize("fn", ["history", "history_sparklines", "history_replay",
                                "history_last_nonempty"])
def test_the_history_surfaces_are_signed_in_and_per_entity_gated(fn):
    params = inspect.signature(getattr(mod, fn)).parameters
    assert "user" in params, f"{fn} does not resolve a user, so it cannot gate"
    assert params["user"].default.dependency is current_user


async def test_a_log_level_filter_takes_everything_at_or_above_it():
    """Asking for WARNING and not being shown the ERROR beside it is never what
    was meant — and at 03:14 it reads as "nothing was wrong"."""
    ch = _CH()
    await mod.app_logs(_Request(ch), level="warning", _admin=ADMIN)
    assert ch.queries[0][1]["levels"] == ["WARNING", "ERROR", "CRITICAL"]


async def test_an_unknown_log_level_is_ignored_rather_than_returning_nothing():
    """A typo'd level that filtered to the empty set would read as a quiet system."""
    ch = _CH()
    await mod.app_logs(_Request(ch), level="LOUD", _admin=ADMIN)
    assert "levels" not in ch.queries[0][1]


@pytest.mark.parametrize("limit,expect", [(0, 1), (-1, 1), (10**6, 2000), (50, 50)])
async def test_the_log_limit_is_clamped(limit, expect):
    ch = _CH()
    await mod.app_logs(_Request(ch), limit=limit, _admin=ADMIN)
    assert ch.queries[0][1]["limit"] == expect


async def test_log_filters_are_bound_parameters_not_interpolated():
    """`q` is a free-text substring typed by a person. Interpolated into the SQL it
    would be a ClickHouse injection on an admin session."""
    ch = _CH()
    await mod.app_logs(_Request(ch), q="'; DROP TABLE app_logs --", _admin=ADMIN)
    sql, params = ch.queries[0]
    assert "DROP TABLE" not in sql
    assert params["q"] == "'; DROP TABLE app_logs --"


async def test_the_command_trail_filters_are_bound_too():
    ch = _CH()
    await mod.history_commands(_Request(ch), entity_id="'; DROP --",
                               source="' OR 1=1 --", _admin=ADMIN)
    sql, params = ch.queries[0]
    assert "DROP" not in sql and "1=1" not in sql
    assert params["eid"] == "'; DROP --"


@pytest.mark.parametrize("limit,expect", [(0, 1), (10**6, 2000)])
async def test_the_command_trail_limit_is_clamped(limit, expect):
    ch = _CH()
    await mod.history_commands(_Request(ch), limit=limit, _admin=ADMIN)
    assert ch.queries[0][1]["limit"] == expect


async def test_a_missing_log_store_is_reported():
    req = _Request()
    req.app.state.ch = None
    with pytest.raises(HTTPException) as e:
        await mod.app_logs(req, _admin=ADMIN)
    assert e.value.status_code == 503
