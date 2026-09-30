"""Retention-policy admin routes — the UI-editable history retention matrix.

Reads/writes `retention_class` / `retention_capability` / `retention_override` in
Postgres (the single source of truth). On save it re-applies the ClickHouse TTLs
live via `apply_retention()` — no migration, no rebuild. Admin-only.
"""

from __future__ import annotations

from dida_core import apply_retention
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, require_admin

router = APIRouter(tags=["retention"])

_DAY_MAX = 366 * 30  # ~30 years, a sane upper bound for a day-count field


class ClassRow(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=128)
    days_raw: int = Field(ge=0, le=_DAY_MAX)
    days_1h: int = Field(ge=0, le=_DAY_MAX)
    days_1d: int = Field(ge=0, le=_DAY_MAX)
    sort_order: int = 100


class OverrideRow(BaseModel):
    entity_id: str = Field(min_length=1, max_length=256)
    capability: str = Field(min_length=1, max_length=64)
    class_name: str = Field(min_length=1, max_length=64)


class RetentionIn(BaseModel):
    classes: list[ClassRow]
    capabilities: dict[str, str]  # capability -> class_name
    overrides: list[OverrideRow] = []


@router.get("/retention")
async def get_retention(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    classes = [
        dict(r)
        for r in await pool.fetch(
            "SELECT name, label, days_raw, days_1h, days_1d, sort_order "
            "FROM retention_class ORDER BY sort_order, name"
        )
    ]
    caps = {
        r["capability"]: r["class_name"]
        for r in await pool.fetch(
            "SELECT capability, class_name FROM retention_capability ORDER BY capability"
        )
    }
    overrides = [
        dict(r)
        for r in await pool.fetch(
            "SELECT entity_id, capability, class_name FROM retention_override "
            "ORDER BY entity_id, capability"
        )
    ]
    # Every capability currently in use, so the UI can offer real ones to assign.
    known = [
        r["capability"]
        for r in await pool.fetch(
            "SELECT DISTINCT capability FROM current_state ORDER BY capability"
        )
    ]
    return {
        "classes": classes,
        "capabilities": caps,
        "overrides": overrides,
        "known_capabilities": known,
    }


@router.put("/retention")
async def put_retention(
    body: RetentionIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    names = {c.name for c in body.classes}
    if "default" not in names:
        raise HTTPException(400, "the 'default' class is required")
    for cap, cls in body.capabilities.items():
        if cls not in names:
            raise HTTPException(400, f"capability {cap!r} → unknown class {cls!r}")
    for o in body.overrides:
        if o.class_name not in names:
            raise HTTPException(400, f"override {o.entity_id}/{o.capability} → unknown class")

    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM retention_class WHERE name <> ALL($1::text[])", list(names)
        )
        for c in body.classes:
            await conn.execute(
                "INSERT INTO retention_class "
                "(name, label, days_raw, days_1h, days_1d, sort_order, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, now()) "
                "ON CONFLICT (name) DO UPDATE SET label = EXCLUDED.label, "
                "days_raw = EXCLUDED.days_raw, days_1h = EXCLUDED.days_1h, "
                "days_1d = EXCLUDED.days_1d, sort_order = EXCLUDED.sort_order, updated_at = now()",
                c.name, c.label, c.days_raw, c.days_1h, c.days_1d, c.sort_order,
            )
        await conn.execute("DELETE FROM retention_capability")
        for cap, cls in body.capabilities.items():
            await conn.execute(
                "INSERT INTO retention_capability (capability, class_name) VALUES ($1, $2)",
                cap, cls,
            )
        await conn.execute("DELETE FROM retention_override")
        for o in body.overrides:
            await conn.execute(
                "INSERT INTO retention_override (entity_id, capability, class_name) "
                "VALUES ($1, $2, $3)",
                o.entity_id, o.capability, o.class_name,
            )

    # Push the new policy to ClickHouse immediately (best-effort — a CH blip must
    # not fail the save; the engine re-syncs on its next connect anyway).
    ch = request.app.state.ch
    if ch is not None:
        try:
            await apply_retention(ch, pool)
        except Exception as exc:
            raise HTTPException(502, f"saved, but ClickHouse TTL apply failed: {exc}") from exc
    return {"ok": True}
