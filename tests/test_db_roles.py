"""Integration test — the database roles (dida_core.db_roles) against a real Postgres.

Runs in its own database as the superuser, the keys service's seat, and walks the
upgrade a house takes: every table created and owned by the superuser, handed to the
app role by the keys service, then the new migration applied by the app role itself.
Then the boundary: the app role is no superuser, and each host service reaches only
its own rows of app_settings.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import asyncpg
import pytest
from dida_core import apply_migrations, pg_pool, set_app_setting
from dida_core.crypto import db_password
from dida_core.db import app_setting
from dida_core.db_roles import APP, LANPROBE, NETMGR, ROLES, grant, provision

SECRET = os.environ["DIDA_SECRET_KEY"]
DB = os.environ["POSTGRES_DB"]
MIGRATIONS = Path("db/migrations")
NEW = "0089_host_service_rows.sql"
SEEDED = {"managed_vlans": "20", "vlan_config": "{}", "net_parent": "eth0", "vlan_status": "{}",
          "lan_status": "{}", "lan_ip": "192.0.2.10", "opus_token": "sealed"}


def _login(role: str) -> dict:
    return {"host": os.environ["POSTGRES_HOST"], "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "user": role, "password": db_password(SECRET, role), "database": DB}


@pytest.fixture
async def su(tmp_path):
    pool = await pg_pool(min_size=1, max_size=2)
    before = tmp_path / "migrations"
    before.mkdir()
    for f in MIGRATIONS.glob("*.sql"):
        if f.name < NEW:
            shutil.copy(f, before)
    await apply_migrations(pool, before)
    async with pool.acquire() as conn:
        await provision(conn, SECRET, DB)
    yield pool
    await pool.close()


@pytest.fixture
async def app(su):
    pool = await asyncpg.create_pool(**_login(APP), min_size=1, max_size=2)
    await apply_migrations(pool, MIGRATIONS)
    for key, value in SEEDED.items():
        await set_app_setting(pool, key, value)
    yield pool
    await pool.close()


@pytest.fixture
async def netmgr(app):
    conn = await asyncpg.connect(**_login(NETMGR))
    yield conn
    conn.terminate()


@pytest.fixture
async def lanprobe(app):
    conn = await asyncpg.connect(**_login(LANPROBE))
    yield conn
    await conn.close()


async def test_the_upgrade_hands_the_house_to_the_app_role(su, app):
    assert await su.fetchval("SELECT datdba::regrole::text FROM pg_database WHERE datname = $1", DB) == APP
    foreign = await su.fetch(
        "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relowner::regrole::text <> $1", APP)
    assert [dict(r) for r in foreign] == []
    assert await su.fetchval("SELECT count(*) FROM schema_migrations WHERE version = $1", NEW) == 1, \
        "the app role applied the migration after the hand-over"
    plain = await su.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[]) AND rolcanlogin AND NOT "
        "(rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)", list(ROLES))
    assert {r["rolname"] for r in plain} == set(ROLES)
    async with su.acquire() as conn:
        await provision(conn, SECRET, DB)


@pytest.mark.parametrize("sql", [
    "CREATE ROLE zzb_escalate SUPERUSER",
    "COPY (SELECT 1) TO PROGRAM 'true'",
    "SELECT pg_read_file('/etc/passwd')",
    "ALTER SYSTEM SET work_mem = '8MB'",
])
async def test_the_app_role_is_no_superuser(app, sql):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute(sql)


async def test_netmgr_reaches_only_its_own_rows(netmgr):
    assert await app_setting(netmgr, "managed_vlans") == "20"
    assert await app_setting(netmgr, "opus_token") is None
    visible = {r["key"] for r in await netmgr.fetch("SELECT key FROM app_settings")}
    assert visible == {"managed_vlans", "vlan_config", "net_parent", "vlan_status"}
    await set_app_setting(netmgr, "vlan_status", '{"20": "up"}')
    assert await app_setting(netmgr, "vlan_status") == '{"20": "up"}'
    for key in ("lan_status", "managed_vlans", "opus_token"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await set_app_setting(netmgr, key, "x")
    for sql in ("SELECT * FROM entities", "DELETE FROM app_settings WHERE key = 'vlan_status'"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await netmgr.execute(sql)


async def test_lanprobe_reaches_only_its_own_row(lanprobe):
    await set_app_setting(lanprobe, "lan_status", '{"iface": "eth1"}')
    assert await app_setting(lanprobe, "lan_status") == '{"iface": "eth1"}'
    assert await app_setting(lanprobe, "lan_ip") == "192.0.2.10"
    assert await app_setting(lanprobe, "managed_vlans") is None
    assert await app_setting(lanprobe, "opus_token") is None
    for key in ("vlan_status", "lan_ip"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await set_app_setting(lanprobe, key, "x")


async def test_a_restore_can_boot_the_host_services_but_not_the_superuser(su, app, netmgr):
    """What the restore's session sweep relies on: pg_signal_backend reaches a host
    service's session, and a superuser's is beyond it."""
    theirs = await netmgr.fetchval("SELECT pg_backend_pid()")
    assert await app.fetchval("SELECT pg_terminate_backend($1)", theirs) is True
    async with su.acquire() as conn:
        mine = await conn.fetchval("SELECT pg_backend_pid()")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.fetchval("SELECT pg_terminate_backend($1)", mine)


async def test_the_grants_come_back_after_they_are_lost(su, app, netmgr):
    """A restore recreates app_settings without its grants; the api grants again."""
    await su.execute(f'REVOKE ALL ON app_settings FROM "{NETMGR}"')
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_setting(netmgr, "managed_vlans")
    await grant(app)
    assert await app_setting(netmgr, "managed_vlans") == "20"
