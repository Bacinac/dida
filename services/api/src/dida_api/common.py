"""Small cross-cutting helpers shared by app.py and the extracted routers.

Consolidated from copies that used to live in app.py / services.py (the audit's
tracked duplications). The DB-touching ones take the pool explicitly so a router
can pass request.app.state.pool without importing the app module.
"""

from __future__ import annotations

import json
import logging
import os

from anthropic import AsyncAnthropic
from dida_core import DEVICE_TYPES as DEVICE_TYPES  # re-export (single source of truth)
from dida_core import ConfigDecryptError, decrypt_secret

log = logging.getLogger("dida.api")

# DEVICE_TYPES — the canonical device-type vocabulary the UI may set on an entity
# (PATCH /entities) or a whole device (PATCH /devices). It lives ONCE, in the core
# capability model, and is re-exported here so the routers keep importing it from
# common. The type is DIDA's own truth: the adapter only SEEDS it, never overrides.


async def resolve_assistant_client(state) -> AsyncAnthropic | None:
    """Build (and cache on app.state) the Claude client from the stored key,
    falling back to the env var. Rebuilds when the key changes so UI edits take
    effect live. `state` is request.app.state (or app.state)."""
    key = await stored_api_key(state.pool, "anthropic_api_key") or None
    if not key:
        return None
    if state.anthropic_client is None or state.anthropic_key != key:
        state.anthropic_client = AsyncAnthropic(api_key=key)
        state.anthropic_key = key
    return state.anthropic_client


def as_list(v: object) -> list:
    """jsonb column → list, whether the pool decodes it (list) or returns text."""
    if isinstance(v, list):
        return v
    if isinstance(v, (str, bytes, bytearray)):
        try:
            return json.loads(v) or []
        except (ValueError, TypeError):
            return []
    return []


def key_hint(value: str | None) -> str | None:
    """Last 4 chars only, so the admin can recognise which key is set without
    the secret ever leaving the server."""
    if not value:
        return None
    return value[-4:] if len(value) >= 4 else value


async def get_setting(pool, key: str) -> str | None:
    return await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", key)


async def stored_api_key(pool, name: str) -> str | None:
    """Read a stored LLM API key, decrypting it. None if unset.

    A value that will not decrypt (rotated DIDA_SECRET_KEY, corrupt row) is NOT
    handed back raw: that ciphertext would leave here as an 'API key' and come
    back as an opaque 401 from the provider. Log it and answer None, which every
    caller already reads as 'no key configured'."""
    enc = await get_setting(pool, name)
    if not enc:
        return None
    try:
        return decrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), enc, raise_on_error=True)
    except ConfigDecryptError:
        log.error("%s is undecryptable (key rotated or value corrupt) — re-enter it in Settings", name)
        return None


async def resolved_key(pool, provider: str) -> str | None:
    """The provider's key from the database. An `<PROVIDER>_API_KEY` in the
    environment is seeded into that row at first boot and never consulted again."""
    return await stored_api_key(pool, f"{provider}_api_key") or None


async def vlan_networks(pool):
    """(vid, network) for each VLAN DIDA currently has an address on — derived from
    the address netmgr obtained (`vlan_status`). Best-effort (skips malformed).
    Shared by the settings write path (reject a routed subnet a VLAN already
    covers) and the discovery-subnet scan."""
    import ipaddress

    out = []
    try:
        for vid, s in json.loads(await get_setting(pool, "vlan_status") or "{}").items():
            addr = s.get("address") or ""
            if "/" in addr:
                out.append((str(vid), ipaddress.ip_interface(addr).network))
    except (AttributeError, ValueError):
        pass
    return out
