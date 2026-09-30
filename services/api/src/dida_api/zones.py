"""Geofence-zone routes (list for any user; create/edit/delete admin-only) —
extracted from app.py. Zones are geofences the presence layer resolves GPS
reports against; exactly one may be flagged `is_home`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin

router = APIRouter(tags=["zones"])

_ZONE_COLS = "id, name, latitude, longitude, radius_m, is_home"


class ZoneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius_m: float = Field(default=100, gt=0, le=1_000_000)
    is_home: bool = False


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_m: float | None = Field(default=None, gt=0, le=1_000_000)
    is_home: bool | None = None


@router.get("/zones")
async def list_zones(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        f"SELECT {_ZONE_COLS} FROM zones ORDER BY is_home DESC, name"  # noqa: S608
    )
    return [dict(r) for r in rows]


@router.post("/zones", status_code=201)
async def create_zone(body: ZoneIn, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        if body.is_home:
            await conn.execute("UPDATE zones SET is_home = false WHERE is_home")
        row = await conn.fetchrow(
            f"INSERT INTO zones (name, latitude, longitude, radius_m, is_home) "  # noqa: S608
            f"VALUES ($1, $2, $3, $4, $5) RETURNING {_ZONE_COLS}",
            body.name.strip(), body.latitude, body.longitude, body.radius_m, body.is_home,
        )
    return dict(row)


@router.patch("/zones/{zone_id}")
async def patch_zone(zone_id: int, body: ZonePatch, request: Request,
                     _admin: AuthUser = Depends(require_admin)) -> dict:
    """Rename, move, resize, or (re)assign the home flag of a zone."""
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
    if not fields:
        raise HTTPException(400, "nothing to update")
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        if fields.get("is_home"):
            await conn.execute("UPDATE zones SET is_home = false WHERE is_home AND id <> $1", zone_id)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        row = await conn.fetchrow(
            f"UPDATE zones SET {sets} WHERE id = $1 RETURNING {_ZONE_COLS}",  # noqa: S608
            zone_id, *fields.values(),
        )
    if row is None:
        raise HTTPException(404, "zone not found")
    return dict(row)


@router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(zone_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> None:
    await request.app.state.pool.execute("DELETE FROM zones WHERE id = $1", zone_id)
