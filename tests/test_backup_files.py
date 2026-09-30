"""The backup store: the two places a filename becomes a path, and the clock.

`/system/backup/file/{name}` reads and deletes files by a name taken straight
from the URL. Admin-only, but "admin" here includes an admin session riding a
crafted link — and `os.path.join("/backups", "../../etc/shadow")` is just
`/etc/shadow`, so the name pattern is the whole of the defence. It is tested here
against the names the writer actually produces, so tightening one without the
other shows up as a failure rather than as backups that can no longer be
downloaded.

The scheduler half is about a quieter failure. `_due_slot` runs before anything
is written, on every tick, for both kinds — so one malformed field in the stored
schedule doesn't degrade backups, it stops them, and the only trace is a log line
in a service nobody reads until they need the backup that isn't there.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from dida_api import backup as mod

GOOD = "dida-config-20260808-0300.dump"
GOOD_HISTORY = "dida-history-20260808-0300.tar.gz"


# --- the name is the whole defence --------------------------------------------


@pytest.mark.parametrize("name", [
    "../../../etc/passwd",
    "../.env",
    "/etc/shadow",
    "dida-config-20260808-0300.dump/../../../etc/shadow",
    "dida-config-20260808-0300.dump\n",
    "dida-config-20260808-0300.dump.bak",
    "dida-secrets-20260808-0300.dump",
    "dida-config-2026-08-08-0300.dump",
    "",
    ".",
])
def test_a_name_that_is_not_exactly_a_backup_is_refused(name):
    assert not mod._NAME_RE.match(name), f"{name!r} would be joined onto the backup dir"


@pytest.mark.parametrize("name", [GOOD, GOOD_HISTORY])
def test_the_names_the_writer_produces_are_accepted(name):
    """A guard nobody can get past is the same as no download feature."""
    assert mod._NAME_RE.match(name)


async def test_what_is_written_is_what_can_be_fetched(monkeypatch, tmp_path):
    """Round-trip: the writer and the reader must agree on the name shape, or the
    backups exist and cannot be downloaded."""
    monkeypatch.setattr(mod, "_BACKUP_DIR", str(tmp_path))

    async def _write(path):
        with open(path, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(mod, "_pg_dump_to", _write)
    monkeypatch.setattr(mod, "_ch_tar_to", _write)
    for kind in mod._KINDS:
        name = await mod._run_kind(kind, keep=10)
        assert mod._NAME_RE.match(name), f"{kind}: wrote {name!r}, which the reader refuses"


# --- pruning ------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_BACKUP_DIR", str(tmp_path))
    return tmp_path


def _fill(store, kind, stamps):
    for s in stamps:
        (store / f"dida-{kind}-{s}{mod._SUFFIX[kind]}").write_bytes(b"x")


def test_pruning_keeps_the_newest_and_removes_the_rest(store):
    _fill(store, "config", ["20260801-0300", "20260802-0300", "20260803-0300"])
    mod._prune_kind("config", keep=2)
    assert sorted(p.name for p in store.iterdir()) == [
        "dida-config-20260802-0300.dump", "dida-config-20260803-0300.dump"]


def test_pruning_one_kind_leaves_the_other_alone(store):
    """History runs weekly and keeps 3; config runs daily and keeps 14. A prune
    that counted across both would delete config dumps to satisfy the history cap."""
    _fill(store, "config", ["20260801-0300", "20260802-0300", "20260803-0300"])
    _fill(store, "history", ["20260801-0300", "20260802-0300"])
    mod._prune_kind("history", keep=1)
    assert len([p for p in store.iterdir() if p.name.startswith("dida-config-")]) == 3


@pytest.mark.parametrize("keep", [0, -1, -3])
def test_a_non_positive_keep_deletes_nothing(store, keep):
    """`keep` reaches here from a stored setting via `int(job.get("keep", 7) or 7)`.
    Zero is harmless by accident — `files[:-0]` is `files[:0]`, an empty slice — but
    a NEGATIVE value inverts the slice: `files[:-(-1)]` is `files[:1]`, which deletes
    the OLDEST backup on every run and leaves the retention count looking correct.
    The explicit guard is what makes both cases a no-op rather than one of them a
    slow rotation of the archive."""
    _fill(store, "config", ["20260801-0300", "20260802-0300"])
    mod._prune_kind("config", keep=keep)
    assert len(list(store.iterdir())) == 2


def test_pruning_never_touches_a_file_it_did_not_write(store):
    """`_BACKUP_DIR` is a mount an operator can repoint (`DIDA_BACKUP_HOST`) — and
    on this installation it points at an NFS share."""
    _fill(store, "config", ["20260801-0300", "20260802-0300"])
    (store / "notes.txt").write_bytes(b"x")
    (store / "dida-config-manual.dump").write_bytes(b"x")
    mod._prune_kind("config", keep=1)
    assert (store / "notes.txt").exists()
    assert (store / "dida-config-manual.dump").exists()


def test_pruning_an_absent_directory_is_not_an_error(monkeypatch, tmp_path):
    """The mount can be missing on a fresh host; the scheduler tick must survive."""
    monkeypatch.setattr(mod, "_BACKUP_DIR", str(tmp_path / "gone"))
    mod._prune_kind("config", keep=1)


def test_only_well_formed_backups_are_listed(store):
    _fill(store, "config", ["20260801-0300"])
    (store / "notes.txt").write_bytes(b"x")
    (store / "dida-config-nonsense.dump").write_bytes(b"x")
    assert [f["name"] for f in mod._list_files()] == ["dida-config-20260801-0300.dump"]


def test_the_listing_is_newest_first(store):
    _fill(store, "config", ["20260801-0300", "20260803-0300", "20260802-0300"])
    names = [f["name"] for f in mod._list_files()]
    assert names == sorted(names, reverse=True)


# --- the clock ----------------------------------------------------------------


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_a_daily_slot_before_the_hour_points_at_yesterday():
    """Otherwise every tick before 03:00 sees a future slot, `last_run` is always
    older, and the backup runs on every tick all night."""
    slot = mod._due_slot({"freq": "daily", "time": "03:00"}, _at(2026, 8, 8, 2, 0))
    assert slot == _at(2026, 8, 7, 3, 0)


def test_a_daily_slot_after_the_hour_points_at_today():
    slot = mod._due_slot({"freq": "daily", "time": "03:00"}, _at(2026, 8, 8, 4, 0))
    assert slot == _at(2026, 8, 8, 3, 0)


def test_a_weekly_slot_is_the_most_recent_matching_day_never_a_future_one():
    # 2026-08-08 is a Saturday (weekday 5).
    now = _at(2026, 8, 8, 4, 0)
    assert mod._due_slot({"freq": "weekly", "weekday": 5, "time": "03:00"}, now) \
        == _at(2026, 8, 8, 3, 0)
    assert mod._due_slot({"freq": "weekly", "weekday": 0, "time": "03:00"}, now) \
        == _at(2026, 8, 3, 3, 0)
    assert mod._due_slot({"freq": "weekly", "weekday": 6, "time": "03:00"}, now) \
        == _at(2026, 8, 2, 3, 0)


def test_a_weekly_slot_later_today_falls_back_a_full_week():
    """Same weekday, but the hour hasn't come round yet."""
    now = _at(2026, 8, 8, 1, 0)
    assert mod._due_slot({"freq": "weekly", "weekday": 5, "time": "03:00"}, now) \
        == _at(2026, 8, 1, 3, 0)


@pytest.mark.parametrize("job", [
    {"time": "not-a-time"},
    {"time": None},
    {"time": "25:00"},
    {"time": "03:99"},
    {"time": "-1:00"},
    {"freq": "weekly", "weekday": "friday"},
    {"freq": "weekly", "weekday": None},
])
def test_a_malformed_schedule_falls_back_instead_of_stopping_every_backup(job):
    """This runs before anything is written, for BOTH kinds, on every tick. An
    exception here doesn't degrade backups — it ends them, and the only evidence is
    a log line in a service nobody reads until the backup is needed."""
    slot = mod._due_slot(job, _at(2026, 8, 8, 4, 0))
    assert isinstance(slot, datetime)
    assert slot <= _at(2026, 8, 8, 4, 0)


def test_the_fallback_is_the_documented_default_hour():
    assert mod._due_slot({"time": "garbage"}, _at(2026, 8, 8, 4, 0)) == _at(2026, 8, 8, 3, 0)


# --- the mount ----------------------------------------------------------------


def test_the_backup_directory_is_not_inside_the_state_volume():
    """`/backups` is repointed at off-box NFS on this installation precisely so a
    lost host does not take the backups with it. Nested under /state it would."""
    assert not mod._BACKUP_DIR.startswith("/state")
    assert os.path.isabs(mod._BACKUP_DIR)
