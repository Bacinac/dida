"""Virtual-entity (helper) routes — list; admin create/delete. Extracted from
app.py. Helpers are user-defined boolean/enum/number entities the `virtual`
adapter owns; the API just CRUDs the virtual_entities table (+ cleans up the
live state/registry rows on delete).
"""

from __future__ import annotations

import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin

router = APIRouter(tags=["virtual"])

# `on_off` is a switch an automation acts on and puts back: what a voice assistant
# is handed as a sentence it may say, since Matter carries on and off and nothing else
VIRTUAL_CAPS = {"boolean", "on_off", "enum", "number", "time"}
VIRTUAL_CATEGORIES = {"control", "config"}


class VirtualIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    capability: str  # boolean | on_off | enum | number | time
    options: list[str] | None = None  # required (>=2) for enum
    # a `number` helper's slider range (stored in the options column as {min,max,step}
    # and published as number_options so the UI renders a slider). Defaults 0..100 step 1.
    min: float | None = None
    max: float | None = None
    step: float | None = None
    # 'control' = a switch/value you operate (shows on device + floor-plan views);
    # 'config'  = a household setting (quiet-hours boundary, threshold) that the
    # device views hide. Default keeps every existing caller on 'control'.
    category: str = "control"


@router.get("/virtual")
async def list_virtual(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT entity_id, name, capability, options, category, created_at "
        "FROM virtual_entities ORDER BY category, entity_id"
    )
    return [dict(r) for r in rows]


@router.post("/virtual", status_code=201)
async def create_virtual(body: VirtualIn, request: Request,
                         _admin: AuthUser = Depends(require_admin)) -> dict:
    cap = body.capability
    if cap not in VIRTUAL_CAPS:
        raise HTTPException(400, f"capability must be one of {sorted(VIRTUAL_CAPS)}")
    if body.category not in VIRTUAL_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(VIRTUAL_CATEGORIES)}")
    opts: list[str] | dict | None = [o.strip() for o in (body.options or []) if o.strip()]
    if cap == "enum" and len(opts) < 2:
        raise HTTPException(400, "an enum helper needs at least 2 options")
    if cap == "number":
        # carry the slider range in the options column: {min, max, step}
        lo = body.min if body.min is not None else 0
        hi = body.max if body.max is not None else 100
        if hi <= lo:
            raise HTTPException(400, "a number helper needs max greater than min")
        opts = {"min": lo, "max": hi, "step": body.step if body.step is not None else 1}
    elif cap != "enum":
        opts = None
    slug = re.sub(r"[^a-z0-9_]+", "_", body.name.strip().lower()).strip("_") or "helper"
    entity_id = f"virtual:{slug}"
    try:
        row = await request.app.state.pool.fetchrow(
            "INSERT INTO virtual_entities (entity_id, name, capability, options, category) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING entity_id, name, capability, options, category, created_at",
            entity_id, body.name.strip(), cap, opts, body.category,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, f"a helper named {entity_id!r} already exists") from exc
    return dict(row)


@router.delete("/virtual/{entity_id:path}", status_code=204)
async def delete_virtual(entity_id: str, request: Request,
                         _admin: AuthUser = Depends(require_admin)) -> None:
    if not entity_id.startswith("virtual:"):
        raise HTTPException(400, "not a virtual entity")
    pool = request.app.state.pool
    result = await pool.execute("DELETE FROM virtual_entities WHERE entity_id = $1", entity_id)
    if result.endswith("0"):
        raise HTTPException(404, "helper not found")
    # Remove its live state + registry row too (it's no longer owned by anyone).
    await pool.execute("DELETE FROM current_state WHERE entity_id = $1", entity_id)
    await pool.execute("DELETE FROM entities WHERE entity_id = $1", entity_id)
