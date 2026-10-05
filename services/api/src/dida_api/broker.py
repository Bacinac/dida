"""The broker: what adapters and the Matter bridge used to read from Postgres,
answered over the bus.

An adapter holds neither database credentials nor the root key (dida_core.identity).
It asks on `dida.cfg.<name>`, sealed with its own key; the api unseals with the key
it derives for that name, so the subject is authenticated by the seal, not by who
published it. A request that does not open under that name's key is dropped.

Every op is scoped to the asking adapter: its own config and secrets, its own
entities to forget, and only the house records it has a reason to read. The few
ops that touch people's data or credentials name their one caller.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from dida_core import house_timezone, set_app_setting
from dida_core.adapter_config import (
    SEALED_KEYS,
    ConfigDecryptError,
    decrypt_secret,
    encrypt_secret,
    fields_for,
    read_adapter_config,
)
from dida_core.broker import TTL, subject
from dida_core.crypto import adapter_key
from dida_core.identity import ADAPTERS, CLIENTS, readable
from dida_core.people import store_book
from home_core.tasks import spawn

log = logging.getLogger("dida.api.broker")

# app_settings an adapter may read. Anyone: plain facts about this host. Named
# callers: credentials, handed over decrypted to the one adapter that uses them.
_PUBLIC_SETTINGS = frozenset({"lan_ip", "app_url", "opus_url", "announce_lang", "webpush_contact"})
_SEALED_SETTINGS = {
    "opus_token": {"opus"},
    "webpush_vapid_private": {"notify"},
    "fcm_service_account": {"notify"},
}
_OWNED_SETTINGS = {  # read, written or consumed by exactly this adapter
    "panel_token": {"cast"},
    "panel_seen_at": {"cast"},
    "contacts_last_sync": {"contacts"},
    "contacts_sync_requested": {"contacts"},
    "smartthings_locations": {"smartthings"},
}


class Refused(Exception):
    """The op exists but is not this adapter's to ask."""


class Ctx:
    def __init__(self, pool, secret: str) -> None:
        self.pool = pool
        self.secret = secret


def _sealed(adapter: str, key: str) -> bool:
    return key in SEALED_KEYS or any(f.key == key and f.secret for f in fields_for(adapter))


def _may_read_setting(adapter: str, key: str) -> bool:
    return (key in _PUBLIC_SETTINGS
            or adapter in _SEALED_SETTINGS.get(key, ())
            or adapter in _OWNED_SETTINGS.get(key, ()))


def _own(adapter: str, entity_id: str) -> None:
    if not entity_id.startswith(f"{adapter}:"):
        raise Refused(f"{entity_id} is not in {adapter}'s namespace")


def _in_reach(adapter: str, where: list[str], args: list) -> None:
    """Scope a read of entities or their state to the namespaces the adapter may read."""
    args.append(sorted(readable(adapter)))
    where.append(f"split_part(entity_id, ':', 1) = ANY(${len(args)}::text[])")


# --- ops ------------------------------------------------------------------


async def _config(ctx: Ctx, adapter: str, of: str | None = None) -> dict:
    of = of or adapter
    if of not in readable(adapter):
        raise Refused(f"{of}'s config is not open to {adapter}")
    return await read_adapter_config(ctx.pool, of, ctx.secret, secrets=of == adapter)


async def _stored(ctx: Ctx, adapter: str, key: str) -> str | None:
    value = await ctx.pool.fetchval(
        "SELECT value FROM adapter_config WHERE adapter = $1 AND key = $2", adapter, key)
    if value is None or not _sealed(adapter, key):
        return value
    return decrypt_secret(ctx.secret, value, adapter=adapter, key=key, raise_on_error=True)


async def _store(ctx: Ctx, adapter: str, key: str, value: str) -> None:
    if _sealed(adapter, key):
        value = encrypt_secret(ctx.secret, value)
    await ctx.pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, $2, $3) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        adapter, key, value)


async def _store_if_current(ctx: Ctx, adapter: str, key: str, current: str, value: str) -> bool:
    if key != "_oauth":
        raise Refused("only the OAuth grant supports conditional storage")
    async with ctx.pool.acquire() as conn, conn.transaction():
        previous = await conn.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = $1 AND key = $2 FOR UPDATE", adapter, key)
        if previous is None:
            return False
        plain = decrypt_secret(ctx.secret, previous, adapter=adapter, key=key, raise_on_error=True)
        if plain != current:
            return False
        await conn.execute(
            "UPDATE adapter_config SET value = $3, updated_at = now() WHERE adapter = $1 AND key = $2",
            adapter, key, encrypt_secret(ctx.secret, value))
    return True


async def _frigate_migrate(ctx: Ctx, adapter: str) -> dict:
    from dida_api.frigate_identity import migrate_sites

    return await migrate_sites(ctx.pool, ctx.secret)


async def _entities(ctx: Ctx, adapter: str, prefix: str | None = None,
                    capability: str | None = None, own: bool = False) -> list[dict]:
    where, args = [], []
    _in_reach(adapter, where, args)
    if own:
        args.append(adapter)
        where.append(f"adapter = ${len(args)}")
    if prefix:
        args.append(prefix)
        where.append(f"starts_with(entity_id, ${len(args)})")
    if capability:
        args.append([capability])
        where.append(f"capabilities @> ${len(args)}::jsonb")
    rows = await ctx.pool.fetch(
        "SELECT entity_id, device_key, name, COALESCE(NULLIF(label, ''), name) AS friendly, "  # noqa: S608
        "exposed, (fp_floor IS NOT NULL OR area_id IS NOT NULL) AS placed FROM entities"
        f" WHERE {' AND '.join(where)} ORDER BY entity_id",
        *args)
    return [dict(r) for r in rows]


async def _forget(ctx: Ctx, adapter: str, entity_ids: list[str] | None = None,
                  device_keys: list[str] | None = None, prefix: str | None = None,
                  keep_placed: bool = False) -> dict:
    """Drop this adapter's entities and their live values. `keep_placed` keeps the
    row of one somebody put on the floor plan or in a room — the operator's work,
    which a reversible change at the source must not spend."""
    entity_ids = list(entity_ids or [])
    device_keys = list(device_keys or [])
    for e in entity_ids:
        _own(adapter, e)
    if prefix is not None:
        _own(adapter, prefix)
    unplaced = " AND fp_floor IS NULL AND area_id IS NULL" if keep_placed else ""
    async with ctx.pool.acquire() as conn, conn.transaction():
        ids = set(entity_ids)
        if device_keys:
            ids |= {r["entity_id"] for r in await conn.fetch(
                "SELECT entity_id FROM entities WHERE adapter = $1 AND device_key = ANY($2::text[])",
                adapter, device_keys)}
        if prefix is not None:
            ids |= {r["entity_id"] for r in await conn.fetch(
                "SELECT entity_id FROM entities WHERE adapter = $1 AND starts_with(entity_id, $2)",
                adapter, prefix)}
            await conn.execute("DELETE FROM current_state WHERE starts_with(entity_id, $1)", prefix)
        await conn.execute("DELETE FROM current_state WHERE entity_id = ANY($1::text[])", list(ids))
        present = await conn.fetchval(
            "SELECT count(*) FROM entities WHERE adapter = $1 AND entity_id = ANY($2::text[])",
            adapter, list(ids))
        gone = await conn.fetch(
            f"DELETE FROM entities WHERE adapter = $1 AND entity_id = ANY($2::text[]){unplaced} "  # noqa: S608
            "RETURNING entity_id", adapter, list(ids))
        if device_keys:
            await conn.execute(
                "DELETE FROM devices d WHERE d.adapter = $1 AND d.device_key = ANY($2::text[]) "
                "AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.device_key = d.device_key)",
                adapter, device_keys)
    return {"kept": int(present) - len(gone)}


async def _state(ctx: Ctx, adapter: str, entity_ids: list[str] | None = None,
                 prefix: str | None = None, capabilities: list[str] | None = None) -> list[dict]:
    where, args = [], []
    if entity_ids is not None:
        args.append(list(entity_ids))
        where.append(f"entity_id = ANY(${len(args)}::text[])")
    if prefix:
        args.append(prefix)
        where.append(f"starts_with(entity_id, ${len(args)})")
    if capabilities:
        args.append(list(capabilities))
        where.append(f"capability = ANY(${len(args)}::text[])")
    if not where:
        raise Refused("state needs a filter")
    _in_reach(adapter, where, args)
    rows = await ctx.pool.fetch(
        "SELECT entity_id, capability, value, EXTRACT(EPOCH FROM (now() - updated_at)) AS age "  # noqa: S608
        f"FROM current_state WHERE {' AND '.join(where)}", *args)
    return [{"entity_id": r["entity_id"], "capability": r["capability"],
             "value": r["value"], "age": float(r["age"])} for r in rows]


async def _setting(ctx: Ctx, adapter: str, key: str) -> str | None:
    if not _may_read_setting(adapter, key):
        raise Refused(f"setting {key} is not open to {adapter}")
    value = await ctx.pool.fetchval("SELECT value FROM app_settings WHERE key = $1", key)
    if value and key in _SEALED_SETTINGS:
        return decrypt_secret(ctx.secret, value, adapter=adapter, key=key, raise_on_error=True)
    return value


async def _setting_age(ctx: Ctx, adapter: str, key: str) -> float | None:
    if adapter not in _OWNED_SETTINGS.get(key, ()):
        raise Refused(f"setting {key} is not {adapter}'s")
    age = await ctx.pool.fetchval(
        "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) FROM app_settings WHERE key = $1", key)
    return None if age is None else float(age)


async def _set_setting(ctx: Ctx, adapter: str, key: str, value: str) -> None:
    if adapter not in _OWNED_SETTINGS.get(key, ()):
        raise Refused(f"setting {key} is not {adapter}'s")
    await set_app_setting(ctx.pool, key, value)


async def _take_setting(ctx: Ctx, adapter: str, key: str) -> str | None:
    """Read-and-clear: a request somebody else sets and this adapter consumes."""
    if adapter not in _OWNED_SETTINGS.get(key, ()):
        raise Refused(f"setting {key} is not {adapter}'s")
    return await ctx.pool.fetchval("DELETE FROM app_settings WHERE key = $1 RETURNING value", key)


async def _house(ctx: Ctx, adapter: str) -> dict:
    return {"tz": (await house_timezone(ctx.pool)).key}


async def _zones(ctx: Ctx, adapter: str) -> list[dict]:
    rows = await ctx.pool.fetch(
        "SELECT name, latitude, longitude, radius_m, is_home FROM zones ORDER BY name")
    return [dict(r) for r in rows]


async def _people(ctx: Ctx, adapter: str) -> dict:
    rows = await ctx.pool.fetch("SELECT name, born_on FROM people WHERE announce ORDER BY name")
    total = await ctx.pool.fetchval("SELECT count(*) FROM people")
    return {"announced": [dict(r) for r in rows], "total": int(total)}


async def _store_book(ctx: Ctx, adapter: str, book: list[dict], source: str, prune: bool = False) -> dict:
    for entry in book:
        if entry.get("born"):
            entry["born"] = dt.date.fromisoformat(entry["born"])
    return await store_book(ctx.pool, book, source=source, prune=prune)


async def _schedules(ctx: Ctx, adapter: str) -> list[dict]:
    rows = await ctx.pool.fetch("SELECT id, name, params, enabled FROM schedules ORDER BY id")
    return [dict(r) for r in rows]


async def _virtual_entities(ctx: Ctx, adapter: str) -> list[dict]:
    rows = await ctx.pool.fetch(
        "SELECT entity_id, name, capability, options, category, default_value FROM virtual_entities")
    return [dict(r) for r in rows]


async def _signing_key(ctx: Ctx, adapter: str, key: str) -> str | None:
    """An undecryptable key reads as absent: notify shows that channel red while
    the other one keeps delivering."""
    try:
        return await _setting(ctx, adapter, key)
    except ConfigDecryptError as exc:
        log.error("broker: %s", exc)
        return None


async def _push_routes(ctx: Ctx, adapter: str) -> dict:
    """Everything notify sends through: the signing keys (decrypted) and every
    login's web-push subscriptions and app tokens."""
    webpush = await ctx.pool.fetch(
        "SELECT u.username, ps.endpoint, ps.p256dh, ps.auth "
        "FROM push_subscriptions ps JOIN users u ON u.id = ps.user_id")
    fcm = await ctx.pool.fetch(
        "SELECT u.username, f.token, f.user_agent FROM fcm_tokens f JOIN users u ON u.id = f.user_id")
    return {
        "vapid_private": await _signing_key(ctx, adapter, "webpush_vapid_private"),
        "contact": await _setting(ctx, adapter, "webpush_contact"),
        "fcm_service_account": await _signing_key(ctx, adapter, "fcm_service_account"),
        "webpush": [dict(r) for r in webpush],
        "fcm": [dict(r) for r in fcm],
    }


async def _push_prune(ctx: Ctx, adapter: str, fcm_token: str | None = None,
                      endpoint: str | None = None) -> None:
    if fcm_token:
        await ctx.pool.execute("DELETE FROM fcm_tokens WHERE token = $1", fcm_token)
    if endpoint:
        await ctx.pool.execute("DELETE FROM push_subscriptions WHERE endpoint = $1", endpoint)


async def _push_ok(ctx: Ctx, adapter: str, endpoint: str) -> None:
    await ctx.pool.execute("UPDATE push_subscriptions SET last_ok_at = now() WHERE endpoint = $1", endpoint)


async def _media_output(ctx: Ctx, adapter: str, entity_id: str, probe_url: str,
                        dac: str | None = None, link: str | None = None) -> None:
    _own(adapter, entity_id)
    await ctx.pool.execute(
        "INSERT INTO media_renderer_output (entity_id, probe_url, dac, link) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (entity_id) DO UPDATE "
        "SET probe_url = EXCLUDED.probe_url, dac = EXCLUDED.dac, link = EXCLUDED.link",
        entity_id, probe_url, dac, link)


_VOICED = "e.diagnostic = false AND e.voice_effective"


def _has(capability: str) -> str:
    return f"EXISTS (SELECT 1 FROM current_state cs WHERE cs.entity_id = e.entity_id AND cs.capability = '{capability}')"  # noqa: S608


async def _voice(ctx: Ctx, adapter: str, activities: list[str] | None = None) -> dict:
    """What the Matter bridge exposes, with the values it starts from. Postgres
    decides what is voiced (`voice_effective`: a rule plus a per-entity override), so
    neither the bridge nor the UI restates the rule."""
    async def kind(where: str, capabilities: list[str], *args: Any, name: str = "e.name") -> dict:
        entities = await ctx.pool.fetch(
            f"SELECT e.entity_id, {name} AS name, e.device_type FROM entities e WHERE {where} ORDER BY e.entity_id",  # noqa: S608
            *args)
        ids = [r["entity_id"] for r in entities]
        states = await ctx.pool.fetch(
            "SELECT entity_id, capability, value FROM current_state "
            "WHERE entity_id = ANY($1::text[]) AND capability = ANY($2::text[])",
            ids, capabilities) if ids else []
        return {"entities": [dict(r) for r in entities], "states": [dict(r) for r in states]}

    return {
        # Gates are coverings and climates thermostats, not on/off plugs: an AC that
        # reports both would give matter.js one slug twice, and it refuses to start.
        "controllables": await kind(
            f"{_VOICED} AND e.device_type IS DISTINCT FROM 'cover' AND {_has('on_off')} "
            f"AND NOT {_has('hvac_mode')}", ["on_off", "brightness"]),
        "climates": await kind(
            f"{_VOICED} AND {_has('hvac_mode')}", ["hvac_mode", "target_temperature", "temperature"]),
        # A gate is renamed through its label: the virtual adapter re-announces its
        # own name on every reconnect and would overwrite a direct edit.
        "covers": await kind(
            f"{_VOICED} AND e.device_type = 'cover' AND {_has('on_off')}", ["on_off"],
            name="COALESCE(e.label, e.name)"),
        "activities": await kind(
            "e.entity_id = ANY($1::text[])", ["source", "source_options"], list(activities or [])),
    }


Handler = Callable[..., Awaitable[Any]]

# op → (handler, the clients it is open to; None = every adapter)
OPS: dict[str, tuple[Handler, frozenset[str] | None]] = {
    "config": (_config, None),
    "stored": (_stored, None),
    "store": (_store, None),
    "store_if_current": (_store_if_current, frozenset({"smartthings"})),
    "frigate_migrate": (_frigate_migrate, frozenset({"frigate"})),
    "entities": (_entities, None),
    "forget": (_forget, None),
    "state": (_state, None),
    "setting": (_setting, None),
    "setting_age": (_setting_age, None),
    "set_setting": (_set_setting, None),
    "take_setting": (_take_setting, None),
    "house": (_house, None),
    "media_output": (_media_output, frozenset({"volumio"})),
    "zones": (_zones, frozenset({"astro", "presence"})),
    "people": (_people, frozenset({"contacts"})),
    "store_book": (_store_book, frozenset({"contacts"})),
    "schedules": (_schedules, frozenset({"calendar"})),
    "virtual_entities": (_virtual_entities, frozenset({"virtual"})),
    "push_routes": (_push_routes, frozenset({"notify"})),
    "push_prune": (_push_prune, frozenset({"notify"})),
    "push_ok": (_push_ok, frozenset({"notify"})),
    "voice": (_voice, frozenset({"matter-bridge"})),
}


def _jsonable(v: Any) -> Any:
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    raise TypeError(f"{type(v).__name__} is not JSON serializable")


async def answer(ctx: Ctx, adapter: str, request: dict) -> dict:
    op = request.get("op")
    entry = OPS.get(op or "")
    if entry is None:
        return {"error": f"unknown op {op!r}"}
    handler, open_to = entry
    if adapter not in (ADAPTERS if open_to is None else open_to):
        return {"error": f"{op} is not open to {adapter}"}
    try:
        return {"result": await handler(ctx, adapter, **(request.get("args") or {}))}
    except Refused as exc:
        log.warning("broker: refused %s %s: %s", adapter, op, exc)
        return {"error": str(exc)}
    except Exception as exc:
        log.exception("broker: %s %s failed", adapter, op)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def serve_broker(bus, pool) -> None:
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    fernets = {a: Fernet(adapter_key(secret, a)) for a in CLIENTS}
    ctx = Ctx(pool, secret)

    async def _handle(msg) -> None:
        adapter = msg.subject.rsplit(".", 1)[-1]
        fernet = fernets.get(adapter)
        if fernet is None:
            log.warning("broker: request for unknown adapter %r dropped", adapter)
            return
        try:
            request = json.loads(fernet.decrypt(msg.data, ttl=TTL))
        except InvalidToken:
            log.warning("broker: request on %s not sealed with its key (or older than %ds) — dropped",
                        msg.subject, TTL)
            return
        t0 = time.monotonic()
        reply = await answer(ctx, adapter, request)
        await msg.respond(fernet.encrypt(json.dumps(reply, default=_jsonable).encode()))
        if (took := time.monotonic() - t0) > 2:
            log.warning("broker: %s %s took %.1fs", adapter, request.get("op"), took)

    async def _cb(msg) -> None:
        spawn(_handle(msg), log=log, name=f"broker {msg.subject}")

    await bus.nc.subscribe(subject("*"), queue="broker", cb=_cb)
    log.info("broker serving %d clients on %s", len(fernets), subject("*"))
