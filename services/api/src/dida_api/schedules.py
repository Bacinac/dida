"""Schedule routes — list + admin CRUD for DIDA's calendar scheduler.

A schedule (recurrence + optional yearly from–to window) becomes one entity
`calendar:<id>` — keyed by the STABLE DB id, not the name, so a rename never
breaks an automation that references it. The API CRUDs the `schedules` table;
on delete it clears that entity's live state/registry rows.
"""

from __future__ import annotations

from datetime import date

from dida_core.schedules import schedule_active, validate_pattern
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin

router = APIRouter(tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    kind: str
    params: dict = Field(default_factory=dict)
    enabled: bool = True


class SchedulePatch(BaseModel):
    enabled: bool | None = None
    params: dict | None = None
    name: str | None = Field(default=None, min_length=1, max_length=64)


class SchedulePreviewIn(BaseModel):
    days: list[date] = Field(min_length=1, max_length=84)


def _validate(kind: str, params: dict) -> None:
    if kind != "calendar":
        raise HTTPException(400, "kind must be 'calendar'")
    try:
        validate_pattern(params)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/schedules")
async def list_schedules(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, kind, params, enabled, created_at FROM schedules ORDER BY id"
    )
    return [dict(r) for r in rows]


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleIn, request: Request,
                          _admin: AuthUser = Depends(require_admin)) -> dict:
    _validate(body.kind, body.params)
    # Pass the dict directly — the pool's jsonb codec (jsonb_init) encodes it;
    # json.dumps() here would double-encode into a jsonb *string*.
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO schedules (name, kind, params, enabled) VALUES ($1, $2, $3, $4) "
        "RETURNING id, name, kind, params, enabled, created_at",
        body.name.strip(), body.kind, body.params, body.enabled,
    )
    return dict(row)


@router.post("/schedules/preview")
async def preview_schedules(body: SchedulePreviewIn, request: Request,
                            _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch("SELECT id, kind, params, enabled FROM schedules ORDER BY id")
    result = []
    for row in rows:
        if not row["enabled"]:
            continue
        _validate(row["kind"], row["params"])
        result.append({"id": row["id"], "days": [day.isoformat() for day in body.days
                                                if schedule_active(day, row["params"])]})
    return result


@router.patch("/schedules/{sid}")
async def patch_schedule(sid: int, body: SchedulePatch, request: Request,
                         _admin: AuthUser = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    cur = await pool.fetchrow("SELECT name, kind, params FROM schedules WHERE id = $1", sid)
    if cur is None:
        raise HTTPException(404, "schedule not found")
    params = body.params
    if params is not None:
        _validate(cur["kind"], params)
    new_name = body.name.strip() if body.name else None
    row = await pool.fetchrow(
        "UPDATE schedules SET "
        "  name    = COALESCE($2, name), "
        "  enabled = COALESCE($3, enabled), "
        "  params  = COALESCE($4, params) "
        "WHERE id = $1 RETURNING id, name, kind, params, enabled, created_at",
        sid, new_name, body.enabled, params,
    )
    # No rename cleanup needed: the entity id is the stable DB id (calendar:<id>),
    # not derived from the name.
    return dict(row)


@router.delete("/schedules/{sid}", status_code=204)
async def delete_schedule(sid: int, request: Request,
                          _admin: AuthUser = Depends(require_admin)) -> None:
    pool = request.app.state.pool
    row = await pool.fetchrow("DELETE FROM schedules WHERE id = $1 RETURNING id", sid)
    if row is None:
        raise HTTPException(404, "schedule not found")
    # Clear the (stable id-based) entity's live state + registry row.
    entity_id = f"calendar:{sid}"
    await pool.execute("DELETE FROM current_state WHERE entity_id = $1", entity_id)
    await pool.execute("DELETE FROM entities WHERE entity_id = $1", entity_id)
