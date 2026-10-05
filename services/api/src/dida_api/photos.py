"""The wall panel's screensaver photographs, from OPUS.

The family's photographs live in OPUS, and the house token opens only the wall's
door there: a random handful of the household's photographs and each one's
preview. The token must never reach a browser, so the panel asks DIDA with its
DIDA session and DIDA asks the player (same pattern as the camera proxy).
"""

from __future__ import annotations

import io
import logging
import re

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

from dida_api import opus
from dida_api.auth import WALLPANEL_USERNAME, AuthUser, current_user

log = logging.getLogger("dida.api.photos")

router = APIRouter(tags=["photos"])

_CHECKSUM_RE = re.compile(r"^[0-9a-f]{40}$")

# The wall panel runs on a Cast device (Nest Hub) whose GPU caps textures at
# 2048px — an image at or past that can't be composited and renders BLANK
# (while text draws fine), and OPUS keeps its previews at 2048px AVIF, which the
# Hub's Chromium may not decode at all. So every photo leaves here as a JPEG
# under the cap; two full images live at once during the crossfade, on a device
# with little RAM.
_MAX_EDGE = 1920


async def photo_user(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role != "admin" and user.username != WALLPANEL_USERNAME:
        raise HTTPException(403, "household photographs require wall panel or administrator access")
    return user


def _for_panel(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


@router.get("/photos/random")
async def photos_random(
    request: Request, response: Response, count: int = Query(30, ge=1, le=100),
    user: AuthUser = Depends(photo_user),
) -> dict:
    """A batch of random household photographs (id, when, place and country
    code). The panel fetches this list, then pixels via /photos/{id}/preview —
    two steps so it can preload the next photo."""
    pool = request.app.state.pool
    response.headers["cache-control"] = "private, no-store"
    if not await opus.configured(pool):
        raise HTTPException(404, "photos_not_configured")
    try:
        drawn = await opus.get(pool, "/photos/wall", count=count)
    except opus.OpusUnavailable as exc:
        log.warning("photos: %s", exc)
        raise HTTPException(502, "opus unavailable") from None
    return {"photos": [
        {"id": p["id"], "taken_at": p.get("taken_at"),
         "place": p.get("place") or None, "country": p.get("country") or None}
        for p in drawn.get("photos") or []
        if isinstance(p.get("id"), str) and _CHECKSUM_RE.match(p["id"])
    ]}


@router.get("/photos/{checksum}/preview")
async def photo_preview(
    checksum: str, request: Request, user: AuthUser = Depends(photo_user)
) -> Response:
    """One photograph as a panel-sized JPEG, streamed through DIDA's origin."""
    if not _CHECKSUM_RE.match(checksum):
        raise HTTPException(400, "invalid photo id")
    try:
        data = await opus.fetch(request.app.state.pool, f"/photos/{checksum}/preview")
    except opus.OpusUnavailable as exc:
        log.warning("photos: %s", exc)
        raise HTTPException(502, "opus unavailable") from None
    try:
        jpeg = await anyio.to_thread.run_sync(_for_panel, data)
    except OSError as exc:
        log.warning("photos: %s could not be decoded: %s", checksum, exc)
        raise HTTPException(502, "photo not decodable") from None
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"cache-control": "private, no-store"},
    )
