"""Android TV live screen snapshots.

adapter-androidtv writes an ADB `screencap` into the shared `androidtv_snap` volume
each poll; this serves the latest frame. DRM apps (Netflix/Disney+) return a black
frame — content protection; live TV / YouTube / Plex show the real screen, which is
the honest "what's on" for apps that publish no media metadata.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from dida_api.auth import AuthUser, can_see_page, current_user
from dida_api.visibility import is_hidden

router = APIRouter(tags=["androidtv"])

_SNAP = Path("/snap")
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@router.get("/androidtv/snapshot/{entity_id:path}")
async def androidtv_snapshot(
    entity_id: str, request: Request, user: AuthUser = Depends(current_user)
) -> Response:
    """Latest screen snapshot for an androidtv entity, as image/png."""
    slug = entity_id.split(":", 1)[-1]
    # The route is a `:path` (accepts slashes) — validate the slug so a crafted
    # entity_id like "../../etc/passwd" can't traverse out of the snapshot dir.
    if not _SLUG_RE.match(slug):
        raise HTTPException(404, "no snapshot yet")
    # A live screen frame is device state — gate it exactly like every other read
    # path: page-level scope (this surfaces on the Media page) AND the per-user
    # view boundary, so a hidden TV can't be watched by URL after the tile is gone.
    if not can_see_page(user, "media"):
        raise HTTPException(403, "no access")
    if await is_hidden(request.app.state.pool, user, entity_id):
        raise HTTPException(404, "no snapshot yet")
    path = _SNAP / f"{slug}.jpg"
    if not path.exists():
        raise HTTPException(404, "no snapshot yet")
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
