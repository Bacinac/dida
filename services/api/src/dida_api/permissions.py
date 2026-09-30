"""Per-user device control permissions — the Phase 2 access boundary.

Enforced at the /command dispatch: a non-admin user's baseline (`can_control`)
plus a set of scope "flip" rules decide whether a given command is allowed.
See migration 0014 for the model rationale. Kept in its own module so the rule
resolution is unit-testable and reusable (a future per-entity view filter can
share `flip_rules`).
"""

from __future__ import annotations

import asyncpg
from fastapi import HTTPException

from dida_api.auth import AuthUser


async def can_control_entity(
    pool: asyncpg.Pool, user: AuthUser, entity_id: str, capability: str
) -> bool:
    """Resolve whether `user` may issue `capability` commands to `entity_id`.

    Admins always may. Otherwise: `matched` is true when any of the user's rules
    covers this entity, its area, or this capability; the outcome flips the
    `can_control` baseline. One DB round-trip (entity area + the user's rules)."""
    if user.role == "admin":
        return True
    row = await pool.fetchrow(
        """
        SELECT (SELECT area_id FROM entities WHERE entity_id = $2) AS area_id,
               ARRAY(SELECT scope || ':' || ref FROM user_access_rules
                     WHERE user_id = $1 AND kind = 'control') AS rules
        """,
        user.id, entity_id,
    )
    rules: set[str] = set(row["rules"] or ())
    area_id = row["area_id"]
    matched = (
        f"entity:{entity_id}" in rules
        or (area_id is not None and f"area:{area_id}" in rules)
        or f"capability:{capability}" in rules
    )
    return user.can_control != matched


async def require_control(
    pool: asyncpg.Pool, user: AuthUser, entity_id: str, capability: str
) -> None:
    """Raise 403 if the user may not control this entity/capability."""
    if not await can_control_entity(pool, user, entity_id, capability):
        raise HTTPException(403, "you do not have permission to control this device")
