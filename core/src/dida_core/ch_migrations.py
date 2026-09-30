"""Numbered ClickHouse migration runner — the single source of truth for the
ClickHouse (history firehose) schema, mirroring the Postgres `migrations.py`.

ClickHouse DDL used to live as a lazy `CREATE TABLE IF NOT EXISTS` inline in the
engine's HistoryWriter. That does not scale past one table (rollup MVs, retention
tiers…). So the schema now lives in `ch/migrations/NNNN_*.sql`, baked into the
base image at /app/ch/migrations, applied in filename order at engine boot.

Unlike Postgres there is a single writer (the engine), so no cross-fleet advisory
lock is needed — version tracking in `ch_schema_migrations` is enough to make it
idempotent. Every statement is itself IF-NOT-EXISTS, so applying to a DB that
predates this runner just records the versions as applied. Files may hold several
`;`-separated statements (ClickHouse runs one statement per command).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger("dida.ch_migrations")

DEFAULT_DIR = os.environ.get("DIDA_CH_MIGRATIONS_DIR", "/app/ch/migrations")


def _statements(sql: str) -> list[str]:
    """Split a migration file into individual statements. Whole-line `--` comments
    are stripped FIRST (a `;` inside a comment must not split a statement), then
    the remaining code is split on `;`. Our DDL never embeds a `;` inside a statement."""
    code = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [s.strip() for s in code.split(";") if s.strip()]


def _read_migrations(d: Path) -> list[tuple[str, str]]:
    return [(p.name, p.read_text()) for p in sorted(d.glob("*.sql"))]


async def apply_ch_migrations(client, migrations_dir: str | os.PathLike | None = None) -> None:
    """Apply every not-yet-applied `ch/migrations/*.sql` in filename order.

    `client` is a clickhouse-connect AsyncClient. Safe to call repeatedly — an
    already-applied file is skipped."""
    d = Path(migrations_dir or DEFAULT_DIR)
    files = await asyncio.to_thread(_read_migrations, d)
    if not files:
        raise RuntimeError(f"no ClickHouse migration files in {d} — refusing to run against an unmanaged schema")

    await client.command(
        "CREATE TABLE IF NOT EXISTS ch_schema_migrations ("
        "  version String, applied_at DateTime DEFAULT now()"
        ") ENGINE = MergeTree ORDER BY version"
    )
    res = await client.query("SELECT version FROM ch_schema_migrations")
    applied = {row[0] for row in res.result_rows}

    for version, sql in files:
        if version in applied:
            continue
        for stmt in _statements(sql):
            await client.command(stmt)
        # ClickHouse has no multi-statement transaction; the file's statements are
        # each idempotent (IF NOT EXISTS), so a partial apply retries cleanly next
        # boot without a half-recorded version.
        await client.command(
            "INSERT INTO ch_schema_migrations (version) VALUES ({version:String})",
            parameters={"version": version},
        )
        log.info("applied ClickHouse migration %s", version)
