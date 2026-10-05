"""Floor routes — the home's levels and their scaffold plan image.

A floor's KEY is what areas/entities reference in fp_floor. Its raster is the
SETUP-TIME scaffold the vision service turns into rooms; the consumer view then
renders pure vector (areas.fp_poly), so the image is only ever reference while
editing. Images live under /state/floorplan (the api's own state mount) and are
served back via GET /floorplan/{key}; upload is a raw PUT (no multipart dep).
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, can_see_page, current_user, require_admin

router = APIRouter(tags=["floors"])

_PLANVISION = os.environ.get("DIDA_PLANVISION_URL", "http://planvision:8094")
_COLS ="id, key, name, sort_order, img_path, img_w, img_h, borders, switch_x, switch_y, marker_scale"
_DIR = Path("/state/floorplan")            # api mounts DIDA_STATE_HOST/api → /state
_MAX_BYTES = 25 * 1024 * 1024              # 25 MB scaffold cap
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_MEDIA = {v: k for k, v in _EXT.items()}
_SLUG = re.compile(r"[^a-z0-9]+")


def _store_image(fname: str, data: bytes, previous: str | None) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    (_DIR / fname).write_bytes(data)
    # Drop a stale image of a different type so we don't leave an orphan.
    if previous and previous != fname:
        (_DIR / previous).unlink(missing_ok=True)


def _slugify(name: str) -> str:
    s = _SLUG.sub("-", name.strip().lower()).strip("-")
    return s or "floor"


def _planvision_error(e: httpx.HTTPError) -> HTTPException:
    """A crashed pipeline and a missing container are different faults.

    Both used to surface as "planvision unreachable", which sent every
    investigation to the network and the container list while the real cause — an
    image OpenCV choked on — sat in planvision's own log, unread."""
    if isinstance(e, httpx.HTTPStatusError):
        return HTTPException(502, f"planvision failed ({e.response.status_code}): "
                                  f"{e.response.text[:200]}")
    return HTTPException(502, f"planvision unreachable: {e}")


class FloorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=48)


class FloorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    sort_order: int | None = Field(default=None, ge=0, le=999)
    # Floor-switch marker position on the plan (% coords; null → default corner).
    switch_x: float | None = Field(default=None, ge=0, le=100)
    switch_y: float | None = Field(default=None, ge=0, le=100)
    # One multiplier for every marker on this floor (composes with per-device scale).
    marker_scale: float | None = Field(default=None, ge=0.3, le=3)


@router.get("/floors")
async def list_floors(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    if not can_see_page(_user, "floorplan"):
        return []
    rows = await request.app.state.pool.fetch(
        f"SELECT {_COLS} FROM floors ORDER BY sort_order, id"  # noqa: S608
    )
    return [dict(r) for r in rows]


@router.post("/floors", status_code=201)
async def create_floor(body: FloorIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Add a level. Its key is a unique slug of the name (areas/entities pin to it)."""
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        base = _slugify(body.name)
        taken = {r["key"] for r in await conn.fetch(
            "SELECT key FROM floors WHERE key = $1 OR key LIKE $2", base, f"{base}-%")}
        key = base
        i = 2
        while key in taken:
            key = f"{base}-{i}"
            i += 1
        nxt = await conn.fetchval("SELECT COALESCE(MAX(sort_order) + 1, 0) FROM floors")
        row = await conn.fetchrow(
            f"INSERT INTO floors (key, name, sort_order) VALUES ($1, $2, $3) RETURNING {_COLS}",  # noqa: S608
            key, body.name.strip(), nxt,
        )
    return dict(row)


@router.patch("/floors/{floor_id}")
async def patch_floor(floor_id: int, body: FloorPatch, request: Request,
                      _admin: AuthUser = Depends(require_admin)) -> dict:
    """Rename or reorder a floor. The key is immutable (placements reference it)."""
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
    if not fields:
        raise HTTPException(400, "nothing to update")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await request.app.state.pool.fetchrow(
        f"UPDATE floors SET {sets} WHERE id = $1 RETURNING {_COLS}", floor_id, *fields.values())  # noqa: S608
    if row is None:
        raise HTTPException(404, "floor not found")
    return dict(row)


@router.delete("/floors/{floor_id}", status_code=204)
async def delete_floor(floor_id: int, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> None:
    """Remove a floor + its scaffold image. Any area/entity still on it keeps its
    (now-orphaned) fp_floor key — placements are cleared from the floorplan UI."""
    row = await request.app.state.pool.fetchrow(
        "DELETE FROM floors WHERE id = $1 RETURNING img_path", floor_id)
    if row and row["img_path"]:
        (_DIR / row["img_path"]).unlink(missing_ok=True)


@router.put("/floors/{floor_id}/image")
async def upload_image(floor_id: int, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Upload/replace a floor's scaffold. Body = raw image bytes; ?w=&h= carry the
    natural pixel dims (the browser has them from decoding for preview)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    ext = _EXT.get(ctype)
    if ext is None:
        raise HTTPException(415, "image must be PNG, JPEG or WebP")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body")
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, "image too large")
    try:
        w = int(request.query_params.get("w", "0")) or None
        h = int(request.query_params.get("h", "0")) or None
    except ValueError:
        w = h = None
    # Guard the raster canvas planvision later builds from these: an out-of-range dim
    # (must be 1..8192 px) is dropped to None (unknown) rather than OOM-ing np.zeros.
    if w is not None and not (1 <= w <= 8192):
        w = None
    if h is not None and not (1 <= h <= 8192):
        h = None

    async with request.app.state.pool.acquire() as conn, conn.transaction():
        floor = await conn.fetchrow("SELECT key, img_path FROM floors WHERE id = $1", floor_id)
        if floor is None:
            raise HTTPException(404, "floor not found")
        fname = f"{floor['key']}.{ext}"
        await asyncio.to_thread(_store_image, fname, data, floor["img_path"])
        row = await conn.fetchrow(
            f"UPDATE floors SET img_path = $2, img_w = $3, img_h = $4 WHERE id = $1 RETURNING {_COLS}",  # noqa: S608
            floor_id, fname, w, h,
        )
    return dict(row)


@router.get("/floorplan/{key}")
async def serve_image(key: str, request: Request,
                      user: AuthUser = Depends(current_user)) -> Response:
    """Serve a floor's scaffold image. Gated to users who can see the floorplan
    page — a narrow-scoped login (e.g. ulaz-only) has no business fetching the
    home's layout."""
    if not can_see_page(user, "floorplan"):
        raise HTTPException(403, "no access to floor plan")
    row = await request.app.state.pool.fetchrow(
        "SELECT img_path FROM floors WHERE key = $1", key)
    if row is None or not row["img_path"]:
        raise HTTPException(404, "no image")
    path = _DIR / row["img_path"]
    if not path.is_file():
        raise HTTPException(404, "no image")
    ext = row["img_path"].rsplit(".", 1)[-1]
    return Response(content=path.read_bytes(), media_type=_MEDIA.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": "no-cache"})


async def _scaffold_bytes(request: Request, key: str) -> tuple[bytes, str]:
    """The floor's stored scaffold bytes + media type, or a 400 if none."""
    row = await request.app.state.pool.fetchrow(
        "SELECT img_path FROM floors WHERE key = $1", key)
    if row is None or not row["img_path"]:
        raise HTTPException(400, "floor has no plan image")
    path = _DIR / row["img_path"]
    if not path.is_file():
        raise HTTPException(400, "plan image missing")
    return path.read_bytes(), _MEDIA.get(row["img_path"].rsplit(".", 1)[-1], "image/png")


@router.post("/floors/{key}/detect-borders")
async def detect_borders(key: str, request: Request,
                         _admin: AuthUser = Depends(require_admin)) -> dict:
    """Vectorize the floor's scaffold into a border skeleton (planvision) — the editable
    draft: [x, y, w, h] rectangles in % coords. Nothing is persisted here."""
    data, ctype = await _scaffold_bytes(request, key)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(f"{_PLANVISION}/borders", content=data,
                                     headers={"content-type": ctype})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _planvision_error(e) from e
    return resp.json()  # {"borders": [[x,y,w,h],…]}


class BordersIn(BaseModel):
    borders: list[list[float]] = Field(default_factory=list)


@router.put("/floors/{floor_id}/borders")
async def save_borders(floor_id: int, body: BordersIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Persist the CONFIRMED border set the user edited (drag/add/delete/merge). Rooms
    derive from these — borders in % [x, y, w, h]."""
    row = await request.app.state.pool.fetchrow(
        f"UPDATE floors SET borders = $2::jsonb WHERE id = $1 RETURNING {_COLS}",  # noqa: S608
        floor_id, body.borders)
    if row is None:
        raise HTTPException(404, "floor not found")
    return dict(row)


class RoomsFromBordersIn(BaseModel):
    borders: list[list[float]] | None = None   # preview the current edit; else the saved set


@router.post("/floors/{key}/rooms-from-borders")
async def rooms_from_borders(key: str, body: RoomsFromBordersIn, request: Request,
                             _admin: AuthUser = Depends(require_admin)) -> dict:
    """Room polygons = the regions enclosed by the floor's borders (planvision) — the
    edit passed in the body, else the saved set. For the UI to preview + assign."""
    row = await request.app.state.pool.fetchrow(
        "SELECT borders, img_w, img_h FROM floors WHERE key = $1", key)
    if row is None:
        raise HTTPException(404, "floor not found")
    borders = body.borders if body.borders is not None else (row["borders"] or [])
    payload = {"borders": borders, "w": row["img_w"] or 1000, "h": row["img_h"] or 1000}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{_PLANVISION}/rooms-from-borders", json=payload)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _planvision_error(e) from e
    return resp.json()  # {"rooms": [[[x,y],…],…]}


class EraseBorderIn(BaseModel):
    borders: list[list[float]] = Field(default_factory=list)
    polys: list[list[list[float]]] = Field(default_factory=list)   # the two adjacent areas


@router.post("/floors/{key}/erase-border")
async def erase_border(key: str, body: EraseBorderIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Merge two adjacent areas by ERASING the border between them — borders are the
    source of truth (planvision). Returns the thinned set + the erased pieces; the UI
    previews, then confirms into its draft (removed == [] → the areas aren't adjacent)."""
    if len(body.polys) != 2:
        raise HTTPException(400, "merge needs exactly two areas")
    row = await request.app.state.pool.fetchrow(
        "SELECT img_w, img_h FROM floors WHERE key = $1", key)
    if row is None:
        raise HTTPException(404, "floor not found")
    payload = {"borders": body.borders, "polys": body.polys,
               "w": row["img_w"] or 1000, "h": row["img_h"] or 1000}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{_PLANVISION}/erase-border", json=payload)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _planvision_error(e) from e
    return resp.json()  # {"borders": [[x,y,w,h],…], "removed": [[x,y,w,h],…]}
