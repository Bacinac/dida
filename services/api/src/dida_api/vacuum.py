"""The robot's own map, and where it sits on the house's floor plan.

The robot draws a map of what it has driven through, in its own frame: its own
origin, its own idea of which way is up. That picture is worth having next to
ours, but only once someone has laid one over the other — so this serves the
picture and stores the placement, and everything that needs a place on the plan
translated into the robot's millimetres works from that one arrangement.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin

router = APIRouter(prefix="/vacuum", tags=["vacuum"])

MAP_SUBJECT = "dida.vacuum.map"
LIVE_SUBJECT = "dida.vacuum.live"
PLACEMENT_KEY = "placement"


class Placement(BaseModel):
    """Where the robot's map lies on the plan: a box in plan percent, turned by an
    angle, optionally mirrored. It is the whole calibration — a picture the user has
    dragged into place says everything two anchor points were meant to say, and says
    it visibly."""

    adapter: str = Field("dreame", pattern=r"^[a-z0-9_]{1,32}$")
    map_id: str = Field("", max_length=64)     # which of the robot's maps, by its name
    floor: str = Field("", max_length=64)      # and which storey of ours it lies on
    x: float = Field(..., ge=-100, le=200)     # left edge, percent of the plan
    y: float = Field(..., ge=-100, le=200)     # top edge
    w: float = Field(..., gt=0, le=400)        # width; height follows the map's aspect
    rotation: float = Field(0, ge=-360, le=360)
    mirrored: bool = False
    opacity: float = Field(0.55, ge=0.05, le=1)


@router.get("/map")
async def vacuum_map(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """The robot's map as a PNG (base64) plus the geometry that gives it a scale."""
    try:
        reply = await request.app.state.bus.nc.request(MAP_SUBJECT, b"{}", timeout=25)
    except Exception as exc:
        raise HTTPException(503, f"Robot ne odgovara: {exc}") from exc
    data = json.loads(reply.data)
    if not data.get("ok"):
        raise HTTPException(409, data.get("error") or "Karta nije dostupna")
    async with request.app.state.pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = 'dreame' AND key = $1", PLACEMENT_KEY)
    store = json.loads(raw) if raw else {}
    # One arrangement per map: each storey lies over a different plan, and an older
    # single placement belongs to whichever map was current when it was made.
    if isinstance(store, dict) and "x" in store:
        store = {str(data.get("map_id", "")): store}
    if not isinstance(store, dict):
        store = {}
    for entry in data.get("maps") or []:
        entry["placement"] = store.get(str(entry.get("map_id")))
    data["placement"] = store.get(str(data.get("map_id", "")))
    data["placements"] = store
    return data


@router.get("/live")
async def vacuum_live(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """Where the robot is right now, the line it has driven, and the area it was sent
    to clean — all in the robot's own millimetres, which the stored placement turns
    back into places on the plan. Polled while it works rather than pushed as state:
    a moving robot would write a row of history per second forever."""
    try:
        reply = await request.app.state.bus.nc.request(LIVE_SUBJECT, b"{}", timeout=8)
    except Exception as exc:
        raise HTTPException(503, f"Robot ne odgovara: {exc}") from exc
    return json.loads(reply.data)


@router.put("/map/placement")
async def set_placement(body: Placement, request: Request,
                        _admin: AuthUser = Depends(require_admin)) -> dict:
    """Remember where the picture was dragged to. Stored beside the adapter's own
    settings because it belongs to that robot's map, not to the plan."""
    value = body.model_dump()
    async with request.app.state.pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = $1 AND key = $2",
            body.adapter, PLACEMENT_KEY)
        store = json.loads(raw) if raw else {}
        if isinstance(store, dict) and "x" in store:      # the single-map shape
            store = {str(store.get("map_id") or body.map_id): store}
        if not isinstance(store, dict):
            store = {}
        store[str(body.map_id)] = value
        await conn.execute(
            "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, $2, $3) "
            "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            body.adapter, PLACEMENT_KEY, json.dumps(store))
    return {"ok": True, "placement": value}
