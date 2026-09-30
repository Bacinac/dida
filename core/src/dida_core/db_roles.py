"""Who logs in to Postgres, and as what.

The superuser stays with Postgres and the keys service. Every other service logs in
as a role that cannot reach past the database: no COPY … PROGRAM, no server files,
no other roles.

- `dida_app` owns the schema. The api, engine and automation run migrations as it.
- `dida_netmgr` and `dida_lanprobe` are host services that must work with the bus
  down, so they keep the database, but row security (migration 0089) narrows each
  to its own app_settings keys.

The keys service provisions them as the superuser on every `up`: role, password
(dida_core.crypto.db_password), ownership, grants. Idempotent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dida_core.crypto import db_password

if TYPE_CHECKING:
    import asyncpg

APP = "dida_app"
NETMGR = "dida_netmgr"
LANPROBE = "dida_lanprobe"
ROLES = (APP, NETMGR, LANPROBE)

# Tables a narrow role may read and write; migration 0089 narrows the rows.
GRANTS = {NETMGR: ("app_settings",), LANPROBE: ("app_settings",)}

_RELATION = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE"}


async def provision(conn: asyncpg.Connection, secret: str, db: str) -> None:
    for role in ROLES:
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            await conn.execute(f'CREATE ROLE "{role}"')
        password = await conn.fetchval("SELECT quote_literal($1)", db_password(secret, role))
        await conn.execute(
            f'ALTER ROLE "{role}" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            f"NOREPLICATION NOBYPASSRLS PASSWORD {password}")
    # A restore boots every other session off the database, the host services' too.
    await conn.execute(f'GRANT pg_signal_backend TO "{APP}"')
    await _hand_over(conn, db)
    await grant(conn)


async def grant(conn: asyncpg.Connection | asyncpg.Pool) -> None:
    """The narrow roles' tables. Run by whoever builds or rebuilds them: the keys
    service, the migration runner (a fresh install has no tables when keys runs) and
    a restore (which drops them with their grants)."""
    for role, tables in GRANTS.items():
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            continue
        for table in tables:
            if await conn.fetchval("SELECT to_regclass($1)", table) is not None:
                await conn.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO "{role}"')


async def _hand_over(conn: asyncpg.Connection, db: str) -> None:
    """Give `dida_app` the database and everything in it the superuser built. A table
    takes its indexes and owned sequences along, so sequences come after."""
    if await conn.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1", db) != APP:
        await conn.execute(f'ALTER DATABASE "{db}" OWNER TO "{APP}"')
    for kinds in (["r", "p", "v", "m"], ["S"]):
        rows = await conn.fetch(
            "SELECT relkind::text AS kind, oid::regclass::text AS name FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind::text = ANY($1) "
            "AND relowner <> (SELECT oid FROM pg_roles WHERE rolname = $2)",
            kinds, APP)
        for r in rows:
            await conn.execute(f'ALTER {_RELATION[r["kind"]]} {r["name"]} OWNER TO "{APP}"')
