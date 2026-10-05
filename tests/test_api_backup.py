"""Integration test — the backup/restore subsystem (dida_api.backup)
end to end against a real Postgres, exercising the paths that need no external
ClickHouse:

* the require_admin boundary on every route (non-admin 403, no cookie 401);
* the scheduled-backup schedule store: GET defaults, PUT round-trip, JobIn
  validation (422), and that editing preserves last_run;
* the on-disk backup file API (list / download / delete) with the anchored
  filename regex rejecting traversal, against a patched _BACKUP_DIR;
* a REAL config round-trip — pg_dump `GET /system/backup` produces a genuine
  PGDMP custom archive, tamper a marker, `POST /system/restore` puts it back
  (pg_dump/pg_restore 18 ship in the api image);
* restore validation branches (empty body, non-archive) failing loud;
* the ClickHouse history-restore *validation* branches (bad tar, missing/unknown
  manifest, missing native member) which all reject before any ClickHouse call;
* the scheduler execution helpers (_maybe_run / _run_kind / _prune_kind) driving a
  real config pg_dump, and the pure schedule helpers (_load_schedule migration,
  _due_slot, _job).

The ClickHouse *success* paths (history_info, history download/restore inserts,
_ch_tar_to) genuinely need a live ClickHouse (:8123) that the api image can't
supply on its own, so only their auth boundary + tar-validation are covered here.

Runs in the api image; the runner supplies the ephemeral Postgres (see
tests/run.sh). Bypasses the app lifespan by wiring app.state directly. The
login rate-limiter is reset per-test by tests/conftest.py.
"""
import io
import json
import os
import tarfile
import tempfile
from datetime import UTC, datetime

import dida_api.app as appmod
import dida_api.backup as backup
from dida_api.common import get_setting
from dida_core import apply_migrations, jsonb_init, pg_pool, set_app_setting
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _setup() -> object:
    """Fresh pool + migrations + a known admin and a known non-admin, app.state wired."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('bkpadmin', 'bkpuser')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('bkpadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('bkpuser', $1, 'user')",
        await hash_password("userpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "backup-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    return pool


async def _login(c: AsyncClient, username: str, password: str) -> None:
    r = await c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username}"


async def _drop_users(pool) -> None:
    await pool.execute("DELETE FROM users WHERE username IN ('bkpadmin', 'bkpuser')")


def _make_targz(members: dict[str, bytes]) -> bytes:
    """A gzip tar carrying exactly `members` (name -> bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── require_admin boundary on every route ────────────────────────────────────

async def test_backup_admin_boundary():
    pool = await _setup()
    try:
        valid_sched = {
            "config": {"enabled": False, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 7},
            "history": {"enabled": False, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 7},
        }
        fname = "dida-config-20260101-0300.dump"
        # A non-admin is refused on every endpoint (403).
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpuser", "userpw12")
            checks = [
                ("get", "/system/backup", None),
                ("post", "/system/restore", b""),
                ("get", "/system/history/info", None),
                ("get", "/system/backup/history", None),
                ("post", "/system/restore/history", b""),
                ("post", "/system/restore/history/recover", b""),
                ("get", "/system/backup/schedule", None),
                ("get", "/system/backup/list", None),
                ("post", "/system/backup/run", None),
                ("get", f"/system/backup/file/{fname}", None),
                ("delete", f"/system/backup/file/{fname}", None),
            ]
            for method, path, content in checks:
                if content is not None:
                    r = await c.request(method.upper(), path, content=content)
                else:
                    r = await c.request(method.upper(), path)
                assert r.status_code == 403, f"non-admin {method} {path} → 403 (got {r.status_code})"
            r = await c.put("/system/backup/schedule", json=valid_sched)
            assert r.status_code == 403, "non-admin PUT schedule → 403"

        # No cookie at all → 401.
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            r = await c.get("/system/backup/list")
            assert r.status_code == 401, "anonymous → 401"
            r = await c.post("/system/restore", content=b"x")
            assert r.status_code == 401, "anonymous restore → 401"
    finally:
        await _drop_users(pool)
        await pool.close()


# ── schedule store: GET defaults, PUT round-trip, validation, last_run keep ──

async def test_backup_schedule_get_put_roundtrip():
    pool = await _setup()
    await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpadmin", "adminpw12")

            # GET with no stored schedule → per-kind defaults, next_run None while disabled.
            r = await c.get("/system/backup/schedule")
            assert r.status_code == 200
            body = r.json()
            assert body["config"]["enabled"] is False and body["config"]["keep"] == 14
            assert body["history"]["enabled"] is False and body["history"]["keep"] == 3
            assert body["config"]["next_run"] is None

            # PUT a valid schedule (config enabled) round-trips through GET.
            payload = {
                "config": {"enabled": True, "freq": "daily", "time": "03:30", "weekday": 0, "keep": 10},
                "history": {"enabled": False, "freq": "weekly", "time": "04:00", "weekday": 2, "keep": 5},
            }
            r = await c.put("/system/backup/schedule", json=payload)
            assert r.status_code == 200, "valid schedule accepted"
            assert r.json()["config"]["keep"] == 10
            r = await c.get("/system/backup/schedule")
            body = r.json()
            assert body["config"]["enabled"] is True and body["config"]["time"] == "03:30"
            assert body["config"]["keep"] == 10
            assert isinstance(body["config"]["next_run"], str), "enabled job exposes a next_run instant"
            assert body["history"]["keep"] == 5

            # Invalid JobIn fields are rejected loud (422), stored value untouched.
            for bad in ({"time": "25:00"}, {"keep": 0}, {"freq": "hourly"}, {"weekday": 9}):
                job = {"enabled": True, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 7, **bad}
                r = await c.put(
                    "/system/backup/schedule",
                    json={"config": job, "history": payload["history"]},
                )
                assert r.status_code == 422, f"invalid job {bad} → 422"
            r = await c.get("/system/backup/schedule")
            assert r.json()["config"]["keep"] == 10, "rejected write left the prior schedule intact"

            # Editing the schedule preserves an existing last_run (doesn't reset the clock).
            stored = await get_setting(pool, "backup_schedule")
            sched = json.loads(stored)
            sched["config"]["last_run"] = "2026-01-01T00:00:00+00:00"
            await set_app_setting(pool, "backup_schedule", json.dumps(sched))
            r = await c.put("/system/backup/schedule", json=payload)
            assert r.status_code == 200
            after = json.loads(await get_setting(pool, "backup_schedule"))
            assert after["config"]["last_run"] == "2026-01-01T00:00:00+00:00", "last_run preserved across edit"
    finally:
        await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
        await _drop_users(pool)
        await pool.close()


# ── on-disk backup file API (list / download / delete) ───────────────────────

async def test_backup_file_list_download_delete():
    pool = await _setup()
    orig_dir = backup._BACKUP_DIR
    tmpdir = tempfile.mkdtemp(prefix="bkp-files-")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpadmin", "adminpw12")

            # A missing backup dir lists as empty (OSError swallowed), not a 500.
            backup._BACKUP_DIR = os.path.join(tmpdir, "does-not-exist")
            r = await c.get("/system/backup/list")
            assert r.status_code == 200 and r.json() == [], "missing backup dir → []"

            # Now a real dir with two valid backups and one bogus file (filtered out).
            backup._BACKUP_DIR = tmpdir
            cfg_name = "dida-config-20260101-0300.dump"
            hist_name = "dida-history-20260102-0300.tar.gz"
            cfg_bytes = b"PGDMP-fake-config-archive-bytes"
            with open(os.path.join(tmpdir, cfg_name), "wb") as f:
                f.write(cfg_bytes)
            with open(os.path.join(tmpdir, hist_name), "wb") as f:
                f.write(b"\x1f\x8bfake-history")
            with open(os.path.join(tmpdir, "not-a-backup.txt"), "wb") as f:
                f.write(b"junk")

            r = await c.get("/system/backup/list")
            assert r.status_code == 200
            listing = r.json()
            names = {e["name"] for e in listing}
            assert names == {cfg_name, hist_name}, "regex filters the stray file out"
            by_name = {e["name"]: e for e in listing}
            assert by_name[cfg_name]["kind"] == "config"
            assert by_name[hist_name]["kind"] == "history"
            assert by_name[cfg_name]["bytes"] == len(cfg_bytes), "reported size matches on disk"
            assert [e["name"] for e in listing] == sorted(names, reverse=True), "newest-name-first order"

            # Download: a name that fails the anchored regex → 404 (no traversal).
            for bad in ("evil.dump", "dida-config-bad.dump", "dida-config-20260101-0300.exe"):
                r = await c.get(f"/system/backup/file/{bad}")
                assert r.status_code == 404, f"bad name {bad} rejected"
            # A well-formed name that doesn't exist on disk → 404.
            r = await c.get("/system/backup/file/dida-config-20991231-2359.dump")
            assert r.status_code == 404, "absent-but-valid name → 404"
            # A real file downloads with the right bytes + media type.
            r = await c.get(f"/system/backup/file/{cfg_name}")
            assert r.status_code == 200 and r.content == cfg_bytes, "config file streams verbatim"
            assert r.headers["content-type"] == "application/octet-stream"
            r = await c.get(f"/system/backup/file/{hist_name}")
            assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"

            # Delete: bad name → 404, missing → 404, real → 204 then gone.
            r = await c.delete("/system/backup/file/evil.dump")
            assert r.status_code == 404
            r = await c.delete("/system/backup/file/dida-config-20991231-2359.dump")
            assert r.status_code == 404
            r = await c.delete(f"/system/backup/file/{cfg_name}")
            assert r.status_code == 204, "delete real file → 204"
            r = await c.get(f"/system/backup/file/{cfg_name}")
            assert r.status_code == 404, "file is gone after delete"
    finally:
        backup._BACKUP_DIR = orig_dir
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
        await _drop_users(pool)
        await pool.close()


# ── config backup: REAL pg_dump download + restore round-trip ────────────────

async def test_config_backup_download_and_restore_roundtrip():
    pool = await _setup()
    await pool.execute("DELETE FROM app_settings WHERE key = 'announce_lang'")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpadmin", "adminpw12")

            # Seed a marker so it lands INSIDE the dump, then export.
            await set_app_setting(pool, "announce_lang", "original-value")
            r = await c.get("/system/backup")
            assert r.status_code == 200, "admin downloads a backup"
            archive = r.content
            assert archive[:5] == b"PGDMP", "a genuine pg_dump custom-format archive"
            assert "attachment" in r.headers.get("content-disposition", ""), "served as a file download"
            assert r.headers["content-type"] == "application/octet-stream"

            # Tamper the live value, then restore the archive over the DB.
            await set_app_setting(pool, "announce_lang", "TAMPERED")
            assert await get_setting(pool, "announce_lang") == "TAMPERED"
            r = await c.post("/system/restore", content=archive)
            assert r.status_code == 200, f"restore succeeds (got {r.status_code}: {r.text[:200]})"
            body = r.json()
            assert body["ok"] is True and body["bytes"] == len(archive)
    finally:
        await pool.close()

    # Restore terminated the pool's sessions; verify + clean up on a fresh pool.
    vpool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    try:
        assert await get_setting(vpool, "announce_lang") == "original-value", "restore put the marker back"
    finally:
        await vpool.execute("DELETE FROM app_settings WHERE key = 'announce_lang'")
        await vpool.execute("DELETE FROM users WHERE username IN ('bkpadmin', 'bkpuser')")
        await vpool.close()


# ── config restore validation (fail loud, DB untouched) ──────────────────────

async def test_config_restore_validation():
    pool = await _setup()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpadmin", "adminpw12")

            r = await c.post("/system/restore", content=b"")
            assert r.status_code == 400 and "empty" in r.text, "empty upload rejected"

            r = await c.post("/system/restore", content=b"this is definitely not a pg_dump archive")
            assert r.status_code == 400, "a non-archive upload is rejected before touching the DB"
            assert "valid DIDA backup" in r.text
    finally:
        await _drop_users(pool)
        await pool.close()


# ── history restore validation (all reject before any ClickHouse call) ───────

async def test_history_restore_validation():
    pool = await _setup()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "bkpadmin", "adminpw12")

            # empty upload
            r = await c.post("/system/restore/history", content=b"")
            assert r.status_code == 400 and "empty" in r.text

            # not a gzip tar at all
            r = await c.post("/system/restore/history", content=b"not a tar.gz at all, just bytes")
            assert r.status_code == 400 and "tar.gz" in r.text, "unparseable archive rejected"

            # valid tar.gz but no manifest
            r = await c.post("/system/restore/history", content=_make_targz({"foo.native": b"x"}))
            assert r.status_code == 400 and "DIDA history backup" in r.text

            # manifest present but not JSON
            r = await c.post(
                "/system/restore/history",
                content=_make_targz({"manifest.json": b"this is not json"}),
            )
            assert r.status_code == 400 and "manifest" in r.text

            # manifest lists only unknown tables
            r = await c.post(
                "/system/restore/history",
                content=_make_targz({"manifest.json": json.dumps({"tables": ["bogus"]}).encode()}),
            )
            assert r.status_code == 400 and "no known history tables" in r.text

            # manifest lists a known table but its .native member is missing
            r = await c.post(
                "/system/restore/history",
                content=_make_targz(
                    {"manifest.json": json.dumps({"tables": ["state_history"]}).encode()}
                ),
            )
            assert r.status_code == 400 and "missing state_history" in r.text
    finally:
        await _drop_users(pool)
        await pool.close()


# ── scheduler execution helpers (real config pg_dump) ────────────────────────

async def test_scheduler_maybe_run_and_run_kind():
    pool = await _setup()
    orig_dir = backup._BACKUP_DIR
    tmpdir = tempfile.mkdtemp(prefix="bkp-sched-")
    await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
    try:
        backup._BACKUP_DIR = tmpdir

        class _App:
            class state:  # a stand-in for app.state
                pass

        app = _App()
        app.state.pool = pool

        # config enabled + due (no last_run); history enabled but ClickHouse absent →
        # its failure is caught per-kind so the tick still finishes and config wins.
        sched = {
            "config": {"enabled": True, "freq": "daily", "time": "00:00", "weekday": 0, "keep": 14},
            "history": {"enabled": True, "freq": "daily", "time": "00:00", "weekday": 0, "keep": 3},
        }
        await set_app_setting(pool, "backup_schedule", json.dumps(sched))

        await backup._maybe_run(app)

        cfg_files = [n for n in os.listdir(tmpdir) if n.startswith("dida-config-")]
        assert len(cfg_files) == 1, "a scheduled config backup was written"
        with open(os.path.join(tmpdir, cfg_files[0]), "rb") as f:
            assert f.read(5) == b"PGDMP", "the scheduled file is a genuine pg_dump archive"
        persisted = json.loads(await get_setting(pool, "backup_schedule"))
        assert persisted["config"].get("last_run"), "config last_run persisted after the run"
        assert not persisted["history"].get("last_run"), "the failed history kind did not record a run"

        # A second immediate tick is a no-op: last_run is now >= the due slot.
        first_last_run = persisted["config"]["last_run"]
        await backup._maybe_run(app)
        assert json.loads(await get_setting(pool, "backup_schedule"))["config"]["last_run"] == first_last_run

        # _run_kind + _prune_kind: seed two stale config files, keep=1 keeps only the newest.
        for stamp in ("20200101-0000", "20200102-0000"):
            with open(os.path.join(tmpdir, f"dida-config-{stamp}.dump"), "wb") as f:
                f.write(b"old")
        newest = await backup._run_kind("config", 1)
        remaining = sorted(n for n in os.listdir(tmpdir) if n.startswith("dida-config-"))
        assert remaining == [newest], "prune kept exactly the newest config backup"

        # keep<=0 is a no-op guard, and a missing dir is swallowed (OSError), not raised.
        backup._prune_kind("config", 0)
        assert os.path.exists(os.path.join(tmpdir, newest)), "keep<=0 prunes nothing"
        backup._BACKUP_DIR = os.path.join(tmpdir, "gone")
        backup._prune_kind("config", 1)  # os.listdir raises → swallowed
        backup._BACKUP_DIR = tmpdir

        # backup_scheduler: both kinds disabled (each hits the skip), and a sleep shim
        # lets one loop iteration run before cancellation propagates out of the loop.
        import asyncio as _aio

        await set_app_setting(
            pool,
            "backup_schedule",
            json.dumps({
                "config": {"enabled": False, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 14},
                "history": {"enabled": False, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 3},
            }),
        )

        class _SleepShim:
            CancelledError = _aio.CancelledError

            def __init__(self):
                self.n = 0

            async def sleep(self, _secs):
                self.n += 1
                if self.n >= 2:  # after the initial settle-sleep + one loop sleep
                    raise _aio.CancelledError

        real_asyncio = backup.asyncio
        backup.asyncio = _SleepShim()
        try:
            cancelled = False
            try:
                await backup.backup_scheduler(app)
            except _aio.CancelledError:
                cancelled = True
            assert cancelled, "scheduler ran a tick then honoured cancellation"
        finally:
            backup.asyncio = real_asyncio
    finally:
        backup._BACKUP_DIR = orig_dir
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
        await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
        await _drop_users(pool)
        await pool.close()


# ── pure schedule helpers (_load_schedule migration, _due_slot, _job) ────────

async def test_schedule_pure_helpers():
    pool = await _setup()
    await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
    try:
        # _load_schedule migrates the OLD flat single-schedule format into two jobs.
        flat = {
            "enabled": True, "freq": "weekly", "time": "04:30", "weekday": 3,
            "keep_config": 5, "keep_history": 2, "config": True, "history": False,
        }
        await set_app_setting(pool, "backup_schedule", json.dumps(flat))
        loaded = await backup._load_schedule(pool)
        assert loaded["config"]["enabled"] is True and loaded["config"]["keep"] == 5
        assert loaded["config"]["freq"] == "weekly" and loaded["config"]["time"] == "04:30"
        assert loaded["history"]["enabled"] is False and loaded["history"]["keep"] == 2

        # No stored schedule → defaults.
        await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
        defaults = await backup._load_schedule(pool)
        assert defaults["config"]["keep"] == 14 and defaults["history"]["keep"] == 3

        # _job: a non-dict raw yields the kind's defaults; a partial dict overlays.
        assert backup._job(None, "config")["keep"] == 14
        overlaid = backup._job({"enabled": True, "keep": 9, "freq": "weekly", "junk": 1}, "history")
        assert overlaid["enabled"] is True and overlaid["keep"] == 9 and overlaid["freq"] == "weekly"
        assert "junk" not in overlaid

        # _due_slot: daily (past + future time), weekly, and a malformed time fallback.
        now = datetime(2026, 7, 11, 10, 30, tzinfo=UTC)
        s = backup._due_slot({"freq": "daily", "time": "03:00"}, now)
        assert s.hour == 3 and s.minute == 0 and s <= now
        s = backup._due_slot({"freq": "daily", "time": "23:00"}, now)
        assert s.hour == 23 and s < now, "a not-yet-reached daily slot rolls back a day"
        s = backup._due_slot({"freq": "weekly", "time": "03:00", "weekday": 0}, now)
        assert s.weekday() == 0 and s.hour == 3 and s <= now
        # A weekly slot that lands later today than `now` rolls back a whole week.
        early = datetime(2026, 7, 11, 2, 0, tzinfo=UTC)
        s = backup._due_slot({"freq": "weekly", "time": "03:00", "weekday": early.weekday()}, early)
        assert s.weekday() == early.weekday() and s.hour == 3 and s < early, "future weekly slot → last week"
        s = backup._due_slot({"freq": "daily", "time": "not-a-time"}, now)
        assert s.hour == 3 and s.minute == 0, "an unparseable time falls back to 03:00"
    finally:
        await pool.execute("DELETE FROM app_settings WHERE key = 'backup_schedule'")
        await _drop_users(pool)
        await pool.close()
