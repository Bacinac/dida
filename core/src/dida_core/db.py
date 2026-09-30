"""Shared Postgres pool — one place that reads the POSTGRES_* env and builds an
asyncpg pool.

Every DB-backed adapter used to hand-roll this identical block, and they drifted:
some honoured POSTGRES_PORT (5442 on host-net adapters), some hardcoded :5432.
Reading the env here fixes that in one place. asyncpg is imported lazily so core
stays dependency-light for the read-only adapters (astro, notify…) that never
touch Postgres.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dida_core.references import SETTING_REFERENCES

if TYPE_CHECKING:
    import asyncpg


def pg_password() -> str:
    """The role's password: from POSTGRES_PASSWORD_FILE, where the keys service wrote
    it (dida_core.db_roles), else POSTGRES_PASSWORD — the superuser's, which only
    the keys service and the test gate hold."""
    path = os.environ.get("POSTGRES_PASSWORD_FILE")
    if path:
        try:
            pw = Path(path).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"database password not readable at {path} — is the keys service up?") from exc
    else:
        pw = os.environ.get("POSTGRES_PASSWORD", "")
    if not pw:
        # Fail loud: a missing password would otherwise connect with a default and
        # mask the misconfiguration. There is no safe default for a system of record.
        raise RuntimeError("no database password: set POSTGRES_PASSWORD_FILE or POSTGRES_PASSWORD")
    return pw


async def pg_pool(
    *,
    min_size: int = 1,
    max_size: int = 1,
    init: Callable[[asyncpg.Connection], Awaitable[None]] | None = None,
) -> asyncpg.Pool:
    """asyncpg pool from POSTGRES_{USER,HOST,PORT,DB} and `pg_password()` (PORT is 5442 on
    host-net adapters, default 5432). Pass ``init=jsonb_init`` for services that
    read jsonb columns as native Python.

    One connection unless asked: some forty processes share Postgres's hundred, and
    tests/test_pool_budget.py holds every ``max_size`` to that sum."""
    import asyncpg

    user = os.environ.get("POSTGRES_USER", "dida")
    pw = pg_password()
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))  # 5442 on host-net adapters
    db = os.environ.get("POSTGRES_DB", "dida")
    # Discrete kwargs, NOT a DSN string: a password containing '/', '#', '%', '?'
    # or '@' corrupts DSN netloc parsing (asyncpg would misread the port or
    # percent-decode the password), so URL-building silently breaks strong passwords.
    return await asyncpg.create_pool(
        user=user,
        password=pw,
        host=host,
        port=port,
        database=db,
        min_size=min_size,
        max_size=max_size,
        init=init,
    )


# One installation value, read from the database and NOWHERE else. The environment
# is a SEED, not a second source: `seed_app_setting` copies it in once at first boot
# (see the api's lifespan) and from then on this is the only place anything reads.
# Two live sources for one fact is how a host ends up half-moved — the DB says one
# address, an .env nobody re-read says another.
_setting_cache: dict[str, tuple[str, float]] = {}
_SETTING_TTL = 30.0


async def app_setting(db: asyncpg.Pool | asyncpg.Connection, key: str) -> str | None:
    """One `app_settings` value, or None when the row is absent."""
    return await db.fetchval("SELECT value FROM app_settings WHERE key = $1", key)


async def host_setting(db, key: str, default: str = "") -> str:
    """`app_settings[key]`, cached briefly (the media base is read per art fetch and
    per cast). `db` is a pool, or an adapter's broker. `default` is for the pre-seed
    instant only — after first boot the row exists, because seeding writes one even
    when the environment was empty."""
    now = time.monotonic()
    hit = _setting_cache.get(key)
    if hit and hit[1] > now:
        return hit[0] or default
    if db is None:
        return default
    # A failed read is NOT an answer: swallowing it here handed an adapter an empty
    # LAN address at boot (the DB comes up alongside it) and — worse — cached that
    # emptiness, so dlna bound SSDP to the default-route leg for the next half
    # minute and reported "nothing answers SSDP on <VLAN ip>". Let it raise: the
    # adapter dies loudly and the runtime restarts it a second later.
    raw = await db.call("setting", key=key) if hasattr(db, "call") else await app_setting(db, key)
    value = (raw or "").strip()
    _setting_cache[key] = (value, now + _SETTING_TTL)
    return value or default


def _declared(key: str) -> None:
    if key not in SETTING_REFERENCES:
        raise ValueError(f"app_settings key {key!r} is not declared in SETTING_REFERENCES")


async def seed_app_setting(db, key: str, value: str) -> None:
    """Write an installation value ONCE, at first boot, from whatever the operator
    put in the environment. Never overwrites: after this the UI owns the value, and
    editing .env has no effect — which is the point."""
    _declared(key)
    value = (value or "").strip()
    if not value:
        return
    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
        key, value,
    )
    _setting_cache.pop(key, None)


async def clear_app_setting(db: asyncpg.Pool | asyncpg.Connection, key: str) -> None:
    """Delete an installation value. The row is the only source, so this genuinely
    unsets it — nothing behind it takes over."""
    await db.execute("DELETE FROM app_settings WHERE key = $1", key)
    _setting_cache.pop(key, None)


async def set_app_setting(db: asyncpg.Pool | asyncpg.Connection, key: str, value: str) -> None:
    """Upsert an `app_settings` row, bumping `updated_at`. `db` is a pool or a
    transaction connection (both expose `.execute`). One writer for the ~14
    hand-rolled copies that had drifted — some bumped `updated_at`, some didn't."""
    _declared(key)
    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        key, value,
    )
    # Drop this process's cached read at once: without it a save looks like it did
    # nothing for up to the TTL, on the very page that just wrote it. Other
    # processes converge on their own copy's expiry.
    _setting_cache.pop(key, None)


async def ensure_app_setting(db: asyncpg.Pool | asyncpg.Connection, key: str, value: str) -> None:
    """Insert an `app_settings` row only if absent — NEVER overwrites. For
    generate-once secrets (the panel token, the VAPID keypair) where a re-run must
    keep the existing value."""
    _declared(key)
    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO NOTHING",
        key, value,
    )


async def jsonb_init(conn: asyncpg.Connection) -> None:
    """asyncpg pool ``init``: decode jsonb columns to native Python types. Without
    it asyncpg hands back the raw JSON *text* ("100", "true", "[]"), so a REST
    snapshot would disagree with the typed values streamed over the WebSocket."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
