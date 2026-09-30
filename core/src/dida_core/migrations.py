"""Numbered SQL migration runner — the single source of truth for the Postgres
schema. Replaces the per-service self-heal (each service used to
CREATE-TABLE-IF-NOT-EXISTS its own tables at boot, scattering the schema across
~8 sites and letting a fresh initdb-only DB be incomplete until every service
had booted once).

`db/migrations/NNNN_*.sql` is baked into the base image at /app/db/migrations.
Any DB-touching service calls `apply_migrations(pool)` at boot; it is:
  * concurrency-safe — a session advisory lock serialises the whole fleet, so
    services booting together don't race each other's CREATEs.
  * idempotent — already-applied files (tracked in `schema_migrations`) are
    skipped; and every current migration is itself IF-NOT-EXISTS, so applying
    them to a DB that predates this runner is a harmless no-op that just records
    the versions as applied.

Fresh install ≡ upgrade: both paths run the same numbered files in order.

INVARIANT — never rename or renumber an ALREADY-APPLIED migration. Tracking is by
FILENAME (`schema_migrations.version`): renaming an applied file makes the runner
see the new name as unapplied and re-execute it (harmless only because every file
is idempotent — a data migration like a DELETE would double-run), and leaves an
orphan row for the old name. Add a NEW numbered file for a change; treat committed
migrations as immutable.

That invariant used to be enforced by nothing at all. Editing the CONTENT of an
already-applied migration was silently ignored forever — and worse, it was ignored
ASYMMETRICALLY: it works on the author's host (applied before the edit), works on a
fresh install (applied after it), and is wrong only on an existing installation
somebody else is running, which is the one place nobody looks. Every file's sha256
is therefore recorded on apply and checked on every boot; a changed applied file is
a hard failure with the fix spelled out.

Rows written before checksums existed have none. The first boot after this change
backfills them from the CURRENT file, which is the honest limit of what can be
known: there is no record of what the file said when it ran. Verification is
prospective from that point.

INVARIANT — migrations carry SCHEMA, never one installation's content. A migration
may create tables/columns and migrate whatever rows happen to exist; it may not
create an entity, automation, helper, floor or area. Those belong to the
installation and are made in the UI, so a fresh install starts empty instead of
inheriting someone else's house. Product-level reference data (retention classes,
capability mappings, UI translations, default alert thresholds) IS part of DIDA and
does belong here. A run of migrations that named concrete devices was removed in
one pass on 2026-07-28; deleting an applied file is safe — it leaves an orphan
`schema_migrations` row, which this runner never reads back.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from dida_core.db_roles import grant

log = logging.getLogger("dida.migrations")


class MigrationIntegrityError(RuntimeError):
    """An applied migration's content no longer matches what was applied."""

# Arbitrary fixed key for pg_advisory_lock — shared by every service so they
# serialise on the same lock. (Any constant works; this one is "DIDA" digits.)
_LOCK_KEY = 3432_0001

DEFAULT_DIR = os.environ.get("DIDA_MIGRATIONS_DIR", "/app/db/migrations")


def _read_migrations(d: Path) -> list[tuple[str, str]]:
    return [(p.name, p.read_text()) for p in sorted(d.glob("*.sql"))]


async def apply_migrations(pool, migrations_dir: str | os.PathLike | None = None) -> None:
    """Apply every not-yet-applied `db/migrations/*.sql` in filename order.

    `pool` is an asyncpg pool. Safe to call from every service at boot."""
    d = Path(migrations_dir or DEFAULT_DIR)
    files = await asyncio.to_thread(_read_migrations, d)
    if not files:
        # Loud, not a warning. Every reason this directory can be empty — a wrong
        # DIDA_MIGRATIONS_DIR, a broken image build, a checkout without db/ — ends
        # with services running against whatever schema happens to be there, which
        # then fails much later and somewhere else. A warning in a log nobody is
        # reading at boot is indistinguishable from success.
        raise MigrationIntegrityError(
            f"no migration files in {d} — refusing to run against an unmanaged schema"
        )

    async with pool.acquire() as conn:
        # Session-level advisory lock: held for the whole run so a fleet booting
        # together applies migrations exactly once, in order, without racing.
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version TEXT PRIMARY KEY,"
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # This table is the runner's own bookkeeping, so it is created here
            # rather than by a numbered file — a migration that adds a column to
            # the table recording migrations has an ordering problem.
            await conn.execute(
                "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT"
            )
            applied = {
                r["version"]: r["checksum"]
                for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")
            }
            for version, sql in files:
                digest = hashlib.sha256(sql.encode()).hexdigest()

                if version in applied:
                    recorded = applied[version]
                    if recorded is None:
                        # Applied before checksums existed. Record what it says now;
                        # there is no way to learn what it said when it ran.
                        await conn.execute(
                            "UPDATE schema_migrations SET checksum = $2 WHERE version = $1",
                            version, digest,
                        )
                    elif recorded != digest:
                        raise MigrationIntegrityError(
                            f"{version} has changed since it was applied "
                            f"(recorded {recorded[:12]}, file {digest[:12]}). "
                            "Committed migrations are immutable — add a NEW numbered "
                            "file for the change. If the edit was intentional and this "
                            "installation already has its effect, update the recorded "
                            "checksum by hand."
                        )
                    continue

                # Each migration + its bookkeeping in one transaction: a failure
                # rolls back cleanly and the version is NOT recorded, so the next
                # boot retries it rather than skipping a half-applied file.
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                        version, digest,
                    )
                log.info("applied migration %s", version)
            await grant(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
