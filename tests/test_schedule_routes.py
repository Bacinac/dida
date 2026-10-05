"""Schedules: the rules that decide when the house heats.

A schedule is a recurrence plus an optional yearly from–to window, and it becomes
one entity `calendar:<id>` that automations reference. Two things about that make
the write path worth pinning.

The dates are not merely shaped, they are parsed downstream. The calendar adapter
reads them with `date(y, m, d)` and returns None when it cannot — and None is not
an error there, it is a default: an unparseable `start_date` becomes "today" and
an unparseable `end_date` becomes "never expires". A typo'd month therefore did
not fail, it quietly changed what the rule does. The shape check alone admitted
`2026-13-45`, which is the kind of typo a person actually makes when they mean
December.

And a deleted schedule has to take its entity with it. The id is stable and the
entity is derived from it, so a row left in `entities` is an orphan pointing at a
schedule that no longer exists — the exact thing `/system/orphans` was built to
surface.
"""

from __future__ import annotations

import pytest
from dida_api import schedules as mod
from dida_api.auth import AuthUser
from fastapi import HTTPException

ADMIN = AuthUser(id=1, username="marko", role="admin")


@pytest.mark.parametrize("interval", [0, -1, True, 1.5, "2"])
def test_invalid_intervals_are_rejected_at_the_api(interval):
    with pytest.raises(HTTPException) as caught:
        mod._validate("calendar", {"recurrence_type": "daily", "start_date": "2026-10-05", "interval": interval})
    assert caught.value.status_code == 400


def test_multiple_cycle_interval_requires_a_stable_start():
    with pytest.raises(HTTPException, match="start_date"):
        mod._validate("calendar", {"recurrence_type": "daily", "interval": 2})


async def test_preview_uses_the_executed_interval_and_excludes_disabled_schedules():
    from datetime import date
    from unittest.mock import AsyncMock

    params = {"recurrence_type": "weekly", "start_date": "2026-10-05", "weekdays": [0], "interval": 2}
    pool = _Pool()
    pool.fetch = AsyncMock(return_value=[{"id": 7, "kind": "calendar", "params": params, "enabled": True},
                                        {"id": 8, "kind": "calendar", "params": params, "enabled": False}])
    body = mod.SchedulePreviewIn(days=[date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)])
    assert await mod.preview_schedules(body, _Request(pool), ADMIN) == [{"id": 7, "days": ["2026-10-05", "2026-10-19"]}]


_DEFAULT_ROW = {"id": 7, "name": "Grijanje", "kind": "calendar", "params": {},
                "enabled": True, "created_at": None}
_ABSENT = object()  # `None` IS the case under test, so it cannot double as "unset"


class _Pool:
    def __init__(self, row=_ABSENT) -> None:
        self.row = dict(_DEFAULT_ROW) if row is _ABSENT else row
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        return [self.row]

    async def fetchrow(self, sql, *a):
        self.calls.append((sql, a))
        return self.row

    async def execute(self, sql, *a):
        self.calls.append((sql, a))


class _Request:
    def __init__(self, pool=None) -> None:
        self.pool = pool or _Pool()
        self.app = type("A", (), {"state": type("S", (), {"pool": self.pool})()})()


def _params(**over):
    p = {"recurrence_type": "yearly"}
    p.update(over)
    return p


# --- a date that is not a date --------------------------------------------------


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "2026-00-10",
                                 "2026-12-32", "0000-01-01"])
@pytest.mark.parametrize("key", ["start_date", "end_date"])
def test_an_impossible_date_is_refused(key, bad):
    """The failure this exists for: the adapter's `date(y, m, d)` raises, it
    returns None, and None means "today" for a start and "never" for an end. The
    schedule then runs — just not the way anybody asked."""
    with pytest.raises(HTTPException) as e:
        mod._validate("calendar", _params(**{key: bad}))
    assert e.value.status_code == 400
    assert "date" in e.value.detail


@pytest.mark.parametrize("good", ["2026-08-08", "2026-8-8", "2024-2-29", "2024-02-29"])
def test_the_forms_the_adapter_can_actually_parse_are_accepted(good):
    """The adapter splits on "-" and ints each part, so an unpadded month is fine.
    Rejecting it would refuse a date that works — a guard that is wrong in the
    other direction."""
    mod._validate("calendar", _params(start_date=good))


def test_a_non_leap_february_29_is_refused():
    """`2026-02-29` matches the shape and is not a day."""
    with pytest.raises(HTTPException):
        mod._validate("calendar", _params(start_date="2026-02-29"))


@pytest.mark.parametrize("bad", ["08/08/2026", "8 Aug 2026", "2026-08", "tomorrow",
                                 "2026-08-08T00:00", " "])
def test_a_date_that_is_not_even_the_right_shape_is_refused(bad):
    if bad.strip():
        with pytest.raises(HTTPException):
            mod._validate("calendar", _params(start_date=bad))


def test_an_absent_date_is_allowed():
    """Both are optional: no start means "from now", no end means "open-ended".
    That is a decision the admin makes by leaving the field empty, which is not
    the same as typing something unparseable."""
    mod._validate("calendar", _params())
    mod._validate("calendar", _params(start_date="", end_date=""))


# --- the rest of the shape ------------------------------------------------------


def test_only_the_calendar_kind_exists():
    with pytest.raises(HTTPException) as e:
        mod._validate("cron", _params())
    assert e.value.status_code == 400


@pytest.mark.parametrize("rt", ["hourly", "", None, "Daily", 5])
def test_an_unknown_recurrence_is_refused(rt):
    """Stored, it produces a schedule the adapter never matches — an entity that
    exists, is enabled, and never turns on."""
    with pytest.raises(HTTPException):
        mod._validate("calendar", {"recurrence_type": rt})


@pytest.mark.parametrize("rt", sorted({"once", "daily", "weekly", "monthly", "yearly"}))
def test_every_documented_recurrence_is_accepted(rt):
    mod._validate("calendar", {"recurrence_type": rt})


@pytest.mark.parametrize("wd", [[7], [-1], ["mon"], "0,1", [0, 8], [None]])
def test_weekdays_outside_monday_to_sunday_are_refused(wd):
    with pytest.raises(HTTPException):
        mod._validate("calendar", _params(weekdays=wd))


def test_the_full_week_is_accepted():
    mod._validate("calendar", _params(weekdays=[0, 1, 2, 3, 4, 5, 6]))


@pytest.mark.parametrize("off", ["1", 1.5, None, "tomorrow"])
def test_a_day_offset_that_is_not_whole_days_is_refused(off):
    """It is subtracted from today as `timedelta(days=offset)`; a float or a string
    fails inside the adapter, where the failure is a schedule that stops matching."""
    with pytest.raises(HTTPException):
        mod._validate("calendar", _params(day_offset=off))


def test_a_negative_offset_is_allowed():
    """"Two days BEFORE" is a legitimate reminder — the offset is signed."""
    mod._validate("calendar", _params(day_offset=-2))


# --- the write path -------------------------------------------------------------


async def test_creating_a_schedule_validates_before_it_writes():
    req = _Request()
    with pytest.raises(HTTPException):
        await mod.create_schedule(
            mod.ScheduleIn(name="x", kind="calendar",
                           params=_params(start_date="2026-13-01")),
            req, _admin=ADMIN)
    assert req.pool.calls == []


async def test_the_params_go_in_as_an_object_not_as_a_string():
    """The pool carries a jsonb codec. `json.dumps` here would store a jsonb
    STRING, and every reader would then get a string where it expects an object —
    silently, because jsonb accepts both."""
    req = _Request()
    await mod.create_schedule(
        mod.ScheduleIn(name="  Grijanje  ", kind="calendar", params=_params()),
        req, _admin=ADMIN)
    _, args = req.pool.calls[0]
    assert isinstance(args[2], dict)
    assert args[0] == "Grijanje", "the name was not trimmed"


async def test_patching_validates_against_the_STORED_kind():
    """Not against one the caller supplies. Otherwise the patch body chooses which
    validator runs on its own params."""
    req = _Request(_Pool({"name": "x", "kind": "calendar", "params": {}}))
    with pytest.raises(HTTPException) as e:
        await mod.patch_schedule(7, mod.SchedulePatch(params=_params(start_date="2026-02-30")),
                                 req, _admin=ADMIN)
    assert e.value.status_code == 400


async def test_patching_a_schedule_that_is_gone_is_a_404():
    req = _Request(_Pool(None))
    with pytest.raises(HTTPException) as e:
        await mod.patch_schedule(7, mod.SchedulePatch(enabled=False), req, _admin=ADMIN)
    assert e.value.status_code == 404


async def test_an_omitted_patch_field_leaves_the_stored_value_alone():
    """The form patches one field at a time; COALESCE is what stops a toggle of
    `enabled` from blanking the recurrence."""
    req = _Request()
    await mod.patch_schedule(7, mod.SchedulePatch(enabled=False), req, _admin=ADMIN)
    sql, args = req.pool.calls[1]
    assert "COALESCE" in sql
    assert args[1] is None and args[3] is None


# --- deleting takes the entity with it ------------------------------------------


async def test_deleting_a_schedule_removes_its_entity_and_its_live_state():
    """The entity is derived from the stable id. Left behind, it is an orphan
    naming a schedule that no longer exists — and automations referencing it keep
    loading, evaluating, and never firing."""
    req = _Request()
    await mod.delete_schedule(7, req, _admin=ADMIN)
    touched = " ".join(sql for sql, _ in req.pool.calls)
    assert "DELETE FROM schedules" in touched
    assert "DELETE FROM current_state" in touched
    assert "DELETE FROM entities" in touched
    assert all(a == ("calendar:7",) for sql, a in req.pool.calls if "entity_id" in sql)


async def test_deleting_something_that_is_not_there_is_a_404_and_cleans_nothing():
    req = _Request(_Pool(None))
    with pytest.raises(HTTPException) as e:
        await mod.delete_schedule(7, req, _admin=ADMIN)
    assert e.value.status_code == 404
    assert len(req.pool.calls) == 1


# --- who may do what ------------------------------------------------------------


def test_reading_is_open_and_writing_is_admin_only():
    """Anyone signed in may see when the house heats; only an admin changes it."""
    import inspect
    assert inspect.signature(mod.list_schedules).parameters["_user"].default.dependency \
        is mod.current_user
    for fn in (mod.create_schedule, mod.patch_schedule, mod.delete_schedule):
        assert inspect.signature(fn).parameters["_admin"].default.dependency \
            is mod.require_admin, f"{fn.__name__} is not admin-gated"
