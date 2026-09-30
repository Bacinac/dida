"""Backup & restore — downloadable, restorable snapshots of DIDA's data.

Two independent backups, because the two stores are
different beasts:

1. CONFIG (Postgres) — `pg_dump -Fc` of the ENTIRE database (registry,
   current_state, automations, auth/users, zones, floors, areas, adapter config +
   secrets, app_settings, schedules, scenes, translations, access rules …). Small;
   this is what brings the house back. pg_dump/pg_restore 18 ship in the api image.

2. HISTORY (ClickHouse) — the time-series firehose, optional and separate because
   it is bulk data bounded by retention TTLs. Backed up as a `.tar.gz` of a
   `FORMAT Native` dump of ALL THREE history tables (raw state_history + the 1h/1d
   rollups). All three, because the rollups deliberately OUTLIVE the raw (raw ~90 d,
   rollups up to years) — rebuilding them from raw on restore would lose the older
   long-term trends. Native, because the rollups are AggregateFunction states that
   only Native round-trips faithfully (CSV/Parquet would finalize + destroy them).
   Restore detaches the rollup materialized views, truncates + inserts all three
   directly, then re-attaches — so the aggregate states are exact, not double-fed.

Admin-only. Both restores are destructive; the UI gates them behind a typed
confirmation and the operator should restart the stack afterwards.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta

import httpx
from dida_core import set_app_setting
from dida_core.db import pg_password
from dida_core.db_roles import grant
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from dida_api.auth import AuthUser, require_admin
from dida_api.common import get_setting

log = logging.getLogger("dida.api.backup")

router = APIRouter(tags=["backup"])

_MAX_RESTORE_BYTES = 512 * 1024 * 1024        # 512 MB — a config DB is tiny; cap abuse
_MAX_HISTORY_BYTES = 4 * 1024 * 1024 * 1024   # 4 GB — history is bigger but retention-bounded

# The history tables (raw + rollups + command audit) and the materialized views
# that feed the rollups. Restore detaches the MVs so a raw re-insert doesn't
# double-populate them.
_HISTORY_TABLES = ("state_history", "state_history_1h", "state_history_1d", "command_history")
_HISTORY_MVS = ("state_history_1h_mv", "state_history_1d_mv")


def _pg() -> tuple[str, str, str, dict[str, str]]:
    """(host, user, db, env-with-PGPASSWORD) for the pg_* client tools."""
    host = os.environ.get("POSTGRES_HOST", "postgres")
    user = os.environ.get("POSTGRES_USER", "dida")
    db = os.environ.get("POSTGRES_DB", "dida")
    env = dict(os.environ)
    env["PGPASSWORD"] = pg_password()
    return host, user, db, env


async def _run(*argv: str, env: dict[str, str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def _pg_dump_to(path: str) -> None:
    """pg_dump -Fc the whole config database to `path`. Raises RuntimeError on fail.
    Shared by the download endpoint and the scheduled-backup writer."""
    host, user, db, env = _pg()
    rc, _out, err = await _run("pg_dump", "-h", host, "-U", user, "-d", db,
                               "-Fc", "--no-owner", "--no-acl", "-f", path, env=env)
    if rc != 0:
        raise RuntimeError(f"pg_dump failed (rc={rc}): {err.decode(errors='replace')[:300]}")


@router.get("/system/backup")
async def download_backup(_admin: AuthUser = Depends(require_admin)) -> StreamingResponse:
    """Stream a `pg_dump -Fc` archive of the whole database as a file download.

    Dumped to a temp file first (not piped) so a pg_dump failure is a clean 500
    instead of a truncated download, then streamed and deleted via a background task."""
    fd, path = tempfile.mkstemp(prefix="dida-backup-", suffix=".dump")
    os.close(fd)
    try:
        await _pg_dump_to(path)
    except RuntimeError as exc:
        _cleanup(path)
        log.error("%s", exc)
        raise HTTPException(500, "backup failed — see api logs") from exc

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    filename = f"dida-backup-{stamp}.dump"

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(_cleanup, path),
    )


@router.post("/system/restore")
async def restore_backup(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Restore a `pg_dump -Fc` archive over the live database. DESTRUCTIVE: every
    object is dropped and recreated from the archive. The archive is POSTed as the
    raw request body (application/octet-stream — no multipart dep). Validates it is a
    real archive first, terminates other DB sessions so the DROPs don't deadlock on a
    live reader, restores in one transaction (all-or-nothing), then asks for a restart."""
    host, user, db, env = _pg()
    fd, path = tempfile.mkstemp(prefix="dida-restore-", suffix=".dump")
    total = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_RESTORE_BYTES:
                    raise HTTPException(413, "backup file too large")
                f.write(chunk)
        if total == 0:
            raise HTTPException(400, "empty file")

        # 1) Validate it's a genuine pg_dump custom archive before touching the DB.
        rc, _out, err = await _run("pg_restore", "--list", path, env=env)
        if rc != 0:
            raise HTTPException(400, "not a valid DIDA backup archive")

        # 2) Boot every OTHER session off the database so pg_restore's DROPs (which
        #    take ACCESS EXCLUSIVE locks) can't deadlock against a live reader. This
        #    also drops our own pool's other connections; asyncpg reconnects lazily.
        #    Superuser sessions (autovacuum, an operator's psql) are beyond our role;
        #    pg_restore waits for their locks instead.
        pool = request.app.state.pool
        await pool.execute(
            "SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a "
            "JOIN pg_roles r ON r.oid = a.usesysid "
            "WHERE a.datname = $1 AND a.pid <> pg_backend_pid() AND NOT r.rolsuper", db,
        )

        # 3) Restore atomically. --clean --if-exists drops each object first (no error
        #    if absent); --single-transaction rolls the whole thing back on any error,
        #    so a failed restore never leaves a half-wiped database.
        rc, _out, err = await _run(
            "pg_restore", "-h", host, "-U", user, "-d", db,
            "--clean", "--if-exists", "--no-owner", "--no-acl", "--single-transaction",
            path, env=env,
        )
        if rc != 0:
            log.error("pg_restore failed (rc=%s): %s", rc, err.decode(errors="replace")[:1000])
            raise HTTPException(500, "restore failed — database left unchanged (rolled back)")
        await grant(pool)
    finally:
        _cleanup(path)

    log.warning("database restored from an uploaded backup by an admin — restart recommended")
    return {"ok": True, "bytes": total,
            "message": "Vraćanje uspješno. Preporučuje se ponovno pokretanje sustava."}


def _cleanup(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


# ── ClickHouse history backup / restore ──────────────────────────────────────

def _ch() -> tuple[str, dict[str, str], str]:
    """(base_url, auth-headers, db) for the ClickHouse HTTP interface (:8123)."""
    host = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
    port = os.environ.get("CLICKHOUSE_PORT", "8123")
    db = os.environ.get("CLICKHOUSE_DB", "dida")
    headers = {
        "X-ClickHouse-User": os.environ.get("CLICKHOUSE_USER", "dida"),
        "X-ClickHouse-Key": os.environ.get("CLICKHOUSE_PASSWORD", ""),
    }
    return f"http://{host}:{port}/", headers, db


async def _ch_ddl(client: httpx.AsyncClient, base: str, headers: dict, sql: str) -> None:
    r = await client.post(base, headers=headers, content=sql.encode())
    if r.status_code != 200:
        raise HTTPException(500, f"clickhouse: {r.text[:300]}")


async def _ch_export(client: httpx.AsyncClient, base: str, headers: dict, query: str, path: str) -> None:
    """Stream a `... FORMAT Native` query result into a file (no in-memory buffering)."""
    async with client.stream("POST", base, headers=headers, content=query.encode()) as r:
        if r.status_code != 200:
            raise HTTPException(500, f"clickhouse export failed: {(await r.aread()).decode(errors='replace')[:300]}")
        f = await asyncio.to_thread(open, path, "wb")
        try:
            async for chunk in r.aiter_bytes(256 * 1024):
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)


async def _file_chunks(path: str):
    f = await asyncio.to_thread(open, path, "rb")
    try:
        while chunk := await asyncio.to_thread(f.read, 1 << 20):
            yield chunk
    finally:
        await asyncio.to_thread(f.close)


async def _ch_import_native(client: httpx.AsyncClient, base: str, headers: dict, table: str, path: str) -> None:
    r = await client.post(base, headers=headers,
                          params={"query": f"INSERT INTO {table} FORMAT Native"}, content=_file_chunks(path))
    if r.status_code != 200:
        raise HTTPException(500, f"clickhouse insert {table} failed: {r.text[:300]}")


@router.get("/system/history/info")
async def history_info(_admin: AuthUser = Depends(require_admin)) -> dict:
    """Row count + on-disk size of the history, so the UI can show what a backup
    will weigh before the operator clicks download."""
    base, headers, db = _ch()
    tables_in = ", ".join(f"'{t}'" for t in _HISTORY_TABLES)
    q = (f"SELECT sum(rows), sum(bytes_on_disk) FROM system.parts "  # noqa: S608
         f"WHERE database='{db}' AND active AND table IN ({tables_in}) FORMAT TSV")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        r = await client.post(base, headers=headers, content=q.encode())
        if r.status_code != 200:
            raise HTTPException(500, "clickhouse unavailable")
        parts = r.text.strip().split("\t")
    rows = parts[0] if parts and parts[0] else "0"
    byts = parts[1] if len(parts) > 1 and parts[1] else "0"
    return {"rows": int(rows), "bytes": int(byts)}


async def _ch_tar_to(path: str) -> None:
    """Write a .tar.gz of a FORMAT Native dump of all three history tables (+ a
    manifest) to `path`. Shared by the download endpoint and the scheduler."""
    base, headers, db = _ch()
    tmpdir = tempfile.mkdtemp(prefix="dida-hist-")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            for tbl in _HISTORY_TABLES:
                await _ch_export(client, base, headers, f"SELECT * FROM {db}.{tbl} FORMAT Native",  # noqa: S608
                                 os.path.join(tmpdir, f"{tbl}.native"))
        manifest = {"created_at": datetime.now(UTC).isoformat(), "db": db,
                    "tables": list(_HISTORY_TABLES), "format": "Native"}
        await asyncio.to_thread(_pack_history, tmpdir, path, manifest)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def _pack_history(tmpdir: str, path: str, manifest: dict) -> None:
    mpath = os.path.join(tmpdir, "manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(mpath, arcname="manifest.json")
        for tbl in _HISTORY_TABLES:
            tar.add(os.path.join(tmpdir, f"{tbl}.native"), arcname=f"{tbl}.native")


def _unpack_history(archive: str, tmpdir: str) -> list[str]:
    """Extract an uploaded history archive and return the tables it carries."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            if "manifest.json" not in tar.getnames():
                raise HTTPException(400, "not a DIDA history backup")
            tar.extractall(tmpdir, filter="data")  # path-sanitised (py3.12+)
    except tarfile.TarError as exc:
        raise HTTPException(400, "not a valid .tar.gz history backup") from exc
    # The manifest says which tables this archive carries — an older backup
    # (pre command_history) restores what it has; unknown names are refused.
    try:
        with open(os.path.join(tmpdir, "manifest.json")) as f:
            manifest_tables = json.load(f).get("tables") or []
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "unreadable backup manifest") from exc
    tables = [t for t in manifest_tables if t in _HISTORY_TABLES]
    if not tables:
        raise HTTPException(400, "backup manifest lists no known history tables")
    for tbl in tables:
        if not os.path.exists(os.path.join(tmpdir, f"{tbl}.native")):
            raise HTTPException(400, f"backup is missing {tbl}")
    return tables


@router.get("/system/backup/history")
async def download_history_backup(_admin: AuthUser = Depends(require_admin)) -> StreamingResponse:
    """A `.tar.gz` of a FORMAT Native dump of all three history tables + a manifest."""
    fd, archive = tempfile.mkstemp(prefix="dida-hist-", suffix=".tar.gz")
    os.close(fd)
    try:
        await _ch_tar_to(archive)
    except Exception:
        _cleanup(archive)
        raise
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")

    def _iter():
        with open(archive, "rb") as f:
            while chunk := f.read(256 * 1024):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="dida-history-{stamp}.tar.gz"'},
        background=BackgroundTask(_cleanup, archive),
    )


@router.post("/system/restore/history")
async def restore_history_backup(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Restore a history backup over the live ClickHouse. DESTRUCTIVE: truncates all
    three tables and reloads them from the archive. Detaches the rollup MVs first so
    the raw re-insert doesn't double-populate the rollups (we load those directly),
    then re-attaches them — restored in a `finally` so a mid-restore error can't
    leave the rollups permanently unfed for live data."""
    base, headers, db = _ch()
    tmpdir = tempfile.mkdtemp(prefix="dida-hist-restore-")
    try:
        archive = os.path.join(tmpdir, "up.tar.gz")
        total = 0
        f = await asyncio.to_thread(open, archive, "wb")
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _MAX_HISTORY_BYTES:
                    raise HTTPException(413, "history backup too large")
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)
        if total == 0:
            raise HTTPException(400, "empty file")
        tables = await asyncio.to_thread(_unpack_history, archive, tmpdir)

        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            detached: list[str] = []
            try:
                for mv in _HISTORY_MVS:
                    await _ch_ddl(client, base, headers, f"DETACH TABLE {db}.{mv}")
                    detached.append(mv)
                for tbl in tables:
                    await _ch_ddl(client, base, headers, f"TRUNCATE TABLE {db}.{tbl}")
                for tbl in tables:
                    await _ch_import_native(client, base, headers, f"{db}.{tbl}",
                                            os.path.join(tmpdir, f"{tbl}.native"))
            finally:
                for mv in detached:
                    try:
                        await _ch_ddl(client, base, headers, f"ATTACH TABLE {db}.{mv}")
                    except Exception:
                        log.error("restore: %s not re-attached — its rollup gets no live data until it is",
                                  mv, exc_info=True)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)

    log.warning("clickhouse history restored from an uploaded backup by an admin")
    return {"ok": True, "bytes": total,
            "message": "Povijest vraćena iz kopije."}


# ── Scheduled (automatic) backups ────────────────────────────────────────────
# The api runs TWO INDEPENDENT scheduled jobs — config and history — each with its
# own daily/weekly rhythm, time, and keep-count (config is tiny + DR-critical, so it
# can run often and keep many; history is bulk + retention-bounded, so it can run
# rarely and keep few). Files go to /backups (a mount separate from /state — see
# docker-compose / DIDA_BACKUP_HOST).

_BACKUP_DIR = "/backups"
_SCHEDULE_KEY = "backup_schedule"
_KINDS = ("config", "history")
_SUFFIX = {"config": ".dump", "history": ".tar.gz"}
_DEFAULT_JOB = {"enabled": False, "freq": "daily", "time": "03:00", "weekday": 0, "keep": 7}
_DEFAULT_SCHEDULE = {
    "config":  {**_DEFAULT_JOB, "keep": 14},
    "history": {**_DEFAULT_JOB, "keep": 3},
}
# Filenames the scheduler writes / the file endpoints serve. Anchored + no slashes,
# so a request path can't traverse out of _BACKUP_DIR.
_NAME_RE = re.compile(r"^dida-(config|history)-\d{8}-\d{4}\.(?:dump|tar\.gz)\Z")


def _job(raw: object, kind: str) -> dict:
    job = dict(_DEFAULT_SCHEDULE[kind])
    if isinstance(raw, dict):
        for k in ("enabled", "freq", "time", "weekday", "keep", "last_run"):
            if k in raw:
                job[k] = raw[k]
    return job


async def _load_schedule(pool) -> dict:
    """Two per-kind jobs. Migrates the old flat single-schedule format on the way."""
    raw = await get_setting(pool, _SCHEDULE_KEY)
    loaded: dict = {}
    if raw:
        with contextlib.suppress(ValueError, TypeError):
            j = json.loads(raw)
            if isinstance(j, dict):
                loaded = j
    # Old flat format had booleans config/history + one freq/time/keep_* — split it
    # into two jobs, ANDing the old global `enabled` with each old target flag.
    if loaded and not isinstance(loaded.get("config"), dict):
        base = {"freq": loaded.get("freq", "daily"), "time": loaded.get("time", "03:00"),
                "weekday": loaded.get("weekday", 0), "last_run": loaded.get("last_run")}
        en = bool(loaded.get("enabled"))
        loaded = {
            "config":  {**base, "enabled": en and bool(loaded.get("config", True)),
                        "keep": int(loaded.get("keep_config", loaded.get("keep", 14)))},
            "history": {**base, "enabled": en and bool(loaded.get("history", False)),
                        "keep": int(loaded.get("keep_history", loaded.get("keep", 3)))},
        }
    return {kind: _job(loaded.get(kind), kind) for kind in _KINDS}


def _due_slot(job: dict, now: datetime) -> datetime:
    """The most recent scheduled instant <= now for a job. Due when last_run is
    before this (so a slot fires once, and a missed slot catches up on next tick)."""
    # The replace() is inside the try because an out-of-range hour raises there,
    # not at the int(): this runs before anything is written, for both kinds, on
    # every tick, so one bad stored field would stop backups rather than degrade
    # them.
    try:
        hh, mm = (int(x) for x in str(job.get("time", "03:00")).split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        slot = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if job.get("freq") == "weekly":
        try:
            wd = int(job.get("weekday", 0)) % 7
        except (ValueError, TypeError):
            wd = 0
        slot -= timedelta(days=(now.weekday() - wd) % 7)
        if slot > now:
            slot -= timedelta(days=7)
    elif slot > now:
        slot -= timedelta(days=1)
    return slot


def _prune_kind(kind: str, keep: int) -> None:
    if keep <= 0:
        return
    try:
        names = os.listdir(_BACKUP_DIR)
    except OSError:
        return
    files = sorted(n for n in names if n.startswith(f"dida-{kind}-") and n.endswith(_SUFFIX[kind]))
    for n in files[:-keep]:
        try:
            os.unlink(os.path.join(_BACKUP_DIR, n))
        except OSError:
            log.warning("backup: old %s not pruned", n, exc_info=True)


async def _run_kind(kind: str, keep: int) -> str:
    """Write one backup (config or history) to _BACKUP_DIR, prune that kind, return
    the filename."""
    await asyncio.to_thread(os.makedirs, _BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    name = f"dida-{kind}-{stamp}{_SUFFIX[kind]}"
    path = os.path.join(_BACKUP_DIR, name)
    if kind == "config":
        await _pg_dump_to(path)
    else:
        await _ch_tar_to(path)
    await asyncio.to_thread(_prune_kind, kind, keep)
    return name


def _list_files() -> list[dict]:
    out: list[dict] = []
    try:
        names = os.listdir(_BACKUP_DIR)
    except OSError:
        return out
    for n in names:
        m = _NAME_RE.match(n)
        if not m:
            continue
        with contextlib.suppress(OSError):
            st = os.stat(os.path.join(_BACKUP_DIR, n))
            out.append({"name": n, "kind": m.group(1), "bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat()})
    return sorted(out, key=lambda x: x["name"], reverse=True)


async def _maybe_run(app) -> None:
    pool = app.state.pool
    sched = await _load_schedule(pool)
    now = datetime.now(UTC)
    changed = False
    for kind in _KINDS:
        job = sched[kind]
        if not job.get("enabled"):
            continue
        last_run = None
        if job.get("last_run"):
            with contextlib.suppress(ValueError, TypeError):
                last_run = datetime.fromisoformat(job["last_run"])
        if last_run is not None and last_run >= _due_slot(job, now):
            continue
        try:
            name = await _run_kind(kind, int(job.get("keep", 7) or 7))
        except Exception:
            log.exception("scheduled %s backup failed", kind)
            continue
        job["last_run"] = now.isoformat()
        changed = True
        log.info("scheduled backup written: %s", name)
    if changed:
        await set_app_setting(pool, _SCHEDULE_KEY, json.dumps(sched))


async def backup_scheduler(app) -> None:
    """Started in the api lifespan. Every ~10 min, runs the scheduled backup if it's
    enabled and due. last_run is persisted, so a restart neither double-runs nor
    misses a slot."""
    await asyncio.sleep(60)  # let the pool + ClickHouse settle after boot
    while True:
        try:
            await _maybe_run(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup scheduler tick failed")
        await asyncio.sleep(600)


class JobIn(BaseModel):
    enabled: bool = False
    freq: str = Field("daily", pattern="^(daily|weekly)$")
    time: str = Field("03:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(0, ge=0, le=6)   # 0 = Monday
    keep: int = Field(7, ge=1, le=365)


class ScheduleIn(BaseModel):
    config: JobIn
    history: JobIn


@router.get("/system/backup/schedule")
async def get_schedule(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    sched = await _load_schedule(request.app.state.pool)
    now = datetime.now(UTC)
    out: dict = {}
    for kind in _KINDS:
        job = dict(sched[kind])
        if job.get("enabled"):
            step = timedelta(days=7 if job.get("freq") == "weekly" else 1)
            job["next_run"] = (_due_slot(job, now) + step).isoformat()
        else:
            job["next_run"] = None
        out[kind] = job
    return out


@router.put("/system/backup/schedule")
async def put_schedule(body: ScheduleIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    prev = await _load_schedule(pool)
    sched: dict = {}
    for kind in _KINDS:
        job = getattr(body, kind).model_dump()
        if prev[kind].get("last_run"):
            job["last_run"] = prev[kind]["last_run"]  # editing doesn't reset the clock
        sched[kind] = job
    await set_app_setting(pool, _SCHEDULE_KEY, json.dumps(sched))
    return sched


@router.get("/system/backup/list")
def list_backups(_admin: AuthUser = Depends(require_admin)) -> list[dict]:
    return _list_files()


@router.post("/system/backup/run")
async def run_backup_now(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Run BOTH backups NOW (a manual full snapshot), each pruned by its own keep."""
    sched = await _load_schedule(request.app.state.pool)
    created = [await _run_kind(kind, int(sched[kind].get("keep", 7) or 7)) for kind in _KINDS]
    return {"created": created}


@router.get("/system/backup/file/{name}")
def download_backup_file(name: str, _admin: AuthUser = Depends(require_admin)) -> StreamingResponse:
    if not _NAME_RE.match(name):
        raise HTTPException(404, "no such backup")
    path = os.path.join(_BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such backup")
    media = "application/gzip" if name.endswith(".tar.gz") else "application/octet-stream"

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(256 * 1024):
                yield chunk

    return StreamingResponse(_iter(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/system/backup/file/{name}", status_code=204)
def delete_backup_file(name: str, _admin: AuthUser = Depends(require_admin)) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(404, "no such backup")
    path = os.path.join(_BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such backup")
    os.unlink(path)
