"""Per-user visibility — the Phase 2 view boundary.

A user's `kind='view'` rules hide entities from them. Enforcement is at the data
source (every read path + the WS firehose), so the client simply never receives
a hidden entity and the whole UI inherits the hiding — no per-component work.

`hidden_entity_ids` resolves the full hidden set (for bulk filtering /state,
/entities and per-connection WS filtering); `is_hidden` answers the single-entity
question (/command, /history). Admins see everything.

Match semantics mirror control rules: an entity is hidden when a view rule names
it directly (scope=entity), names its area (scope=area), or names any capability
it exposes (scope=capability — e.g. hide every `camera` entity from a user).
"""

from __future__ import annotations

import json

import asyncpg
from dida_core.heating import RoomEntities, derive_rooms

from dida_api.auth import AuthUser

# Pages whose whole point is the house at large: they legitimately consume the
# full entity firehose. A user holding ANY of them gets the general set (minus
# their view-hides). Only a genuinely narrow page (ulaz) scopes the entity set —
# so a limited-trust login can't enumerate the house through /state, /entities or
# the WS feed. Keep in sync with users.PAGE_ORDER (the narrow one is 'entry').
_BROAD_PAGES = frozenset({"floorplan", "devices", "media", "history", "cameras", "assistant", "adapters"})


async def _entry_entity_ids(pool: asyncpg.Pool) -> set[str]:
    """The entities the /entry page exposes: each configured access-point slot and
    its optional live-status source. Read straight from app_settings.entry_controls
    (the same JSON entry.py manages) so the page and its scope never drift."""
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'entry_controls'")
    if not raw:
        return set()
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(cfg, dict):
        return set()
    slots = ("car", "pedestrian", "door",
             "state_car", "state_pedestrian", "state_door", "state_door_fallback")
    return {cfg[s].strip() for s in slots if isinstance(cfg.get(s), str) and cfg[s].strip()}


async def page_allowed_ids(pool: asyncpg.Pool, user: AuthUser) -> set[str] | None:
    """Entity ids a page-scoped user may see, or None = no page-based restriction.
    None for admins, unrestricted users (allowed_pages NULL), or anyone holding a
    broad page. A narrow-only user (e.g. ulaz-only) is limited to exactly the
    entities their pages expose."""
    if user.role == "admin" or user.allowed_pages is None:
        return None
    pages = set(user.allowed_pages)
    if pages & _BROAD_PAGES:
        return None
    allowed: set[str] = set()
    if "entry" in pages:
        allowed |= await _entry_entity_ids(pool)
    if "heating" in pages:
        allowed |= await _heating_entity_ids(pool)
    return allowed


async def _heating_entity_ids(pool: asyncpg.Pool) -> set[str]:
    rooms, orphans = await derive_rooms(pool)
    allowed = set(orphans) | {"heating:system"}
    for row in await pool.fetch("SELECT id, heating_config FROM areas"):
        here = rooms.get(row["id"], RoomEntities([], [], []))
        config = row["heating_config"] or {}
        if not here.valves and not config:
            continue
        allowed.add(f"heating:room:{row['id']}")
        allowed.update(here.valves + here.sensors + here.contacts)
        allowed.update(config.get("valves") or [])
        if config.get("sensor"):
            allowed.add(config["sensor"])
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'heating'")
    settings = json.loads(raw) if raw else {}
    allowed.update(settings[key] for key in ("boiler", "outdoor", "away_helper") if settings.get(key))
    return allowed


async def hidden_for(pool: asyncpg.Pool, user: AuthUser) -> set[str]:
    """Effective hidden set for the read surface: the user's view-rule hides PLUS,
    for a page-scoped (narrow) user, every entity OUTSIDE their pages' allowed set.
    Folds allowed_pages into the same exclude-set that /state, /entities and the WS
    hub already apply, so page-scoping needs no per-endpoint logic. For unrestricted
    users this is exactly hidden_entity_ids (no extra query)."""
    hidden = await hidden_entity_ids(pool, user)
    allowed = await page_allowed_ids(pool, user)
    if allowed is None:
        return hidden
    all_ids = {r["entity_id"] for r in await pool.fetch("SELECT entity_id FROM entities")}
    return hidden | (all_ids - allowed)


async def can_view_entity(pool: asyncpg.Pool, user: AuthUser, entity_id: str) -> bool:
    """Single-entity read gate (for /history and other by-id reads): not view-hidden
    AND within the user's page scope. Admins/unrestricted always pass."""
    if await is_hidden(pool, user, entity_id):
        return False
    allowed = await page_allowed_ids(pool, user)
    return allowed is None or entity_id in allowed


async def hidden_entity_ids(pool: asyncpg.Pool, user: AuthUser) -> set[str]:
    """The set of entity_ids `user` may not see. Empty for admins / unrestricted."""
    if user.role == "admin":
        return set()
    rows = await pool.fetch(
        """
        SELECT e.entity_id FROM entities e
        WHERE EXISTS (
            SELECT 1 FROM user_access_rules h
            WHERE h.user_id = $1 AND h.kind = 'view' AND (
                (h.scope = 'entity'     AND h.ref = e.entity_id) OR
                (h.scope = 'area'       AND h.ref = e.area_id::text) OR
                (h.scope = 'capability' AND e.capabilities ? h.ref)
            )
        )
        """,
        user.id,
    )
    return {r["entity_id"] for r in rows}


async def is_hidden(pool: asyncpg.Pool, user: AuthUser, entity_id: str) -> bool:
    """Whether this one entity is hidden from `user`."""
    if user.role == "admin":
        return False
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM entities e
                JOIN user_access_rules h ON h.user_id = $1 AND h.kind = 'view'
                WHERE e.entity_id = $2 AND (
                    (h.scope = 'entity'     AND h.ref = e.entity_id) OR
                    (h.scope = 'area'       AND h.ref = e.area_id::text) OR
                    (h.scope = 'capability' AND e.capabilities ? h.ref)
                )
            )
            """,
            user.id, entity_id,
        )
    )
