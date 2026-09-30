"""Migrations are tracked by filename — so the CONTENT had nothing holding it.

Editing an already-applied migration was silently ignored forever, and ignored
asymmetrically: it works on the author's host (applied before the edit), works on a
fresh install (applied after it), and is wrong only on an existing installation
somebody else is running. That is the one place nobody looks, and the one place an
upgrade actually happens.

The last test here is the one the whole suite exists for: an OLD schema with an
older set of migrations already applied, then today's code on top. Thirty-four
suites call `apply_migrations`, all against an EMPTY database — which covers a fresh
install and nothing else.

Runs against its OWN database (`tests/run.sh` creates it on the ephemeral postgres),
because it drops and rebuilds `public` and rewrites `schema_migrations`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from dida_core import MigrationIntegrityError, apply_migrations, pg_pool

pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_HOST"), reason="needs the ephemeral postgres"
)

MIGRATIONS = Path("db/migrations")


@pytest.fixture
async def pool():
    """A pool onto a database wiped back to empty — every test starts from nothing."""
    p = await pg_pool(min_size=1, max_size=2)
    await p.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    yield p
    await p.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_text().encode()).hexdigest()


async def _recorded(pool) -> dict[str, str | None]:
    rows = await pool.fetch("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def test_a_migration_records_the_checksum_of_what_ran(pool, tmp_path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t (id int);")
    await apply_migrations(pool, tmp_path)
    assert (await _recorded(pool))["0001_a.sql"] == _digest(tmp_path / "0001_a.sql")


async def test_editing_an_applied_migration_is_a_hard_failure(pool, tmp_path):
    """The whole point: this used to be silence, forever."""
    f = tmp_path / "0002_b.sql"
    f.write_text("CREATE TABLE t2 (id int);")
    await apply_migrations(pool, tmp_path)

    f.write_text("CREATE TABLE t2 (id int, extra text);")
    with pytest.raises(MigrationIntegrityError) as e:
        await apply_migrations(pool, tmp_path)
    assert "has changed since it was applied" in str(e.value)
    assert "0002_b.sql" in str(e.value)


async def test_the_error_says_what_to_do_about_it(pool, tmp_path):
    """An integrity failure at boot stops a service; a message that only says NO
    leaves the operator guessing."""
    f = tmp_path / "0003_c.sql"
    f.write_text("CREATE TABLE t3 (id int);")
    await apply_migrations(pool, tmp_path)
    f.write_text("CREATE TABLE t3 (id bigint);")
    with pytest.raises(MigrationIntegrityError) as e:
        await apply_migrations(pool, tmp_path)
    assert "add a NEW numbered" in str(e.value)


async def test_an_unchanged_migration_re_applies_cleanly(pool, tmp_path):
    """A guard that fires on the normal path is worse than no guard — every service
    calls this at every boot."""
    (tmp_path / "0004_d.sql").write_text("CREATE TABLE t4 (id int);")
    for _ in range(3):
        await apply_migrations(pool, tmp_path)


async def test_a_row_from_before_checksums_is_backfilled_not_rejected(pool, tmp_path):
    """An existing installation upgrading INTO this feature must not be bricked by
    it: its rows predate checksums and have none."""
    f = tmp_path / "0005_e.sql"
    f.write_text("CREATE TABLE t5 (id int);")
    await apply_migrations(pool, tmp_path)
    await pool.execute("UPDATE schema_migrations SET checksum = NULL")

    await apply_migrations(pool, tmp_path)          # must not raise
    assert (await _recorded(pool))["0005_e.sql"] == _digest(f)


async def test_an_empty_migration_directory_is_loud(pool, tmp_path):
    """It used to log a warning and return, leaving services running against
    whatever schema happened to be there."""
    with pytest.raises(MigrationIntegrityError) as e:
        await apply_migrations(pool, tmp_path / "nothing-here")
    assert "unmanaged schema" in str(e.value)


async def test_the_real_upgrade_path_an_old_database_then_todays_code(pool, tmp_path):
    """The scenario every installation hits and no test covered.

    Apply the first 20 committed migrations — a database roughly two months behind —
    then the whole current set on top, which is exactly what an upgrade does."""
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert len(files) > 40, "migration set unexpectedly small — wrong working directory?"

    old = tmp_path / "old"
    old.mkdir()
    for f in files[:20]:
        shutil.copy(f, old / f.name)
    await apply_migrations(pool, old)

    before = await _recorded(pool)
    assert len(before) == 20

    await apply_migrations(pool, MIGRATIONS)

    after = await _recorded(pool)
    assert len(after) == len(files)
    assert all(v is not None for v in after.values()), "every row must carry a checksum"
    # The 20 already there are not re-run, and keep the checksum they were applied with.
    for name, digest in before.items():
        assert after[name] == digest


async def test_the_upgraded_schema_matches_a_fresh_install(pool, tmp_path):
    """Upgrade and fresh install must converge, or the two paths drift apart silently
    and only one of them is ever tested."""
    files = sorted(MIGRATIONS.glob("*.sql"))
    old = tmp_path / "old"
    old.mkdir()
    for f in files[:20]:
        shutil.copy(f, old / f.name)
    await apply_migrations(pool, old)
    await apply_migrations(pool, MIGRATIONS)
    upgraded = await _columns(pool)

    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await apply_migrations(pool, MIGRATIONS)
    fresh = await _columns(pool)

    assert upgraded == fresh, "upgraded schema differs from a fresh install"


async def _columns(pool) -> set[tuple[str, str, str]]:
    rows = await pool.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name <> 'schema_migrations'"
    )
    return {(r["table_name"], r["column_name"], r["data_type"]) for r in rows}
