"""The media page's content: what OPUS has, handed to the browser through the
house's own door, and a record put on a device.

The browser never meets OPUS. It signs in here, and this proxies what the shelf
shows with the household's token — reading only, because that token is
owner-level over there. Playing goes the other way round: the queue with its
ticketed addresses is fetched from the player and published to the device
through the same command boundary every other control goes through, so a user
who may not control the streamer cannot start it here either.
"""

from __future__ import annotations

import json
import logging
import re

from dida_core import prepare_command
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from dida_api import opus
from dida_api.auth import AuthUser, current_user
from dida_api.permissions import require_control
from dida_api.visibility import is_hidden

log = logging.getLogger("dida.api.opus")

router = APIRouter(prefix="/opus", tags=["opus"])

KINDS = {"release", "track", "station"}
# what the television can be asked to open, and where it can be sent — checked
# here, because the token that carries them is owner-level on the far side
OPENS = {"movie", "series", "artist", "station"}
TV = "opus:tv"
_PLACE = re.compile(r"^[a-z]{1,20}$")


def _unavailable(exc: opus.OpusUnavailable) -> HTTPException:
    return HTTPException(503, str(exc))



@router.get("/status")
async def status(request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Whether the media page has anything to show: configured, and answering."""
    pool = request.app.state.pool
    if not await opus.configured(pool):
        return {"configured": False, "ok": False, "detail": "not configured"}
    try:
        await opus.stations(pool)
    except opus.OpusUnavailable as exc:
        return {"configured": True, "ok": False, "detail": str(exc)}
    return {"configured": True, "ok": True, "detail": ""}


@router.get("/artists")
async def artists(request: Request, user: AuthUser = Depends(current_user)) -> list[dict]:
    try:
        return await opus.artists(request.app.state.pool)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/artist/{artist_id}")
async def artist(artist_id: int, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    try:
        return await opus.artist(request.app.state.pool, artist_id)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/release/{release_id}")
async def release(release_id: int, request: Request, prefer: str = "stereo",
                  user: AuthUser = Depends(current_user)) -> dict:
    try:
        return await opus.release(request.app.state.pool, release_id, prefer)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/radio/stations")
async def stations(request: Request, user: AuthUser = Depends(current_user)) -> list[dict]:
    try:
        return await opus.stations(request.app.state.pool)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/art")
async def art(request: Request, u: str = Query(..., max_length=2000),
              w: int = Query(320, ge=16, le=2000),
              user: AuthUser = Depends(current_user)) -> Response:
    try:
        upstream = await opus.art(request.app.state.pool, u, w)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, "no picture")
    return Response(upstream.content, media_type=upstream.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": upstream.headers.get("cache-control", "public, max-age=86400")})


class PlayIn(BaseModel):
    player_id: str
    kind: str
    id: int
    start: int = 0
    prefer: str = "stereo"


@router.post("/play")
async def play(body: PlayIn, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Put a record, a song or a station on a device in the house.

    The queue comes from OPUS with every row's ticketed address; the device
    fetches the bytes from the player itself. A station goes as `play_media` —
    the adapters' radio path, which names the codec for the streamer and reads
    the live song off the stream — and anything with files as `play_queue`."""
    if body.kind not in KINDS:
        raise HTTPException(400, "kind must be release, track or station")
    pool = request.app.state.pool
    if await is_hidden(pool, user, body.player_id) or not await pool.fetchval(
            "SELECT 1 FROM entities WHERE entity_id = $1", body.player_id):
        raise HTTPException(404, "unknown player")
    await require_control(pool, user, body.player_id, "media_transport")
    # The television only takes an order while the player is open on it; said
    # here, where the person pressed play, rather than lost in the adapter's log.
    if body.player_id == TV:
        try:
            if not (await opus.tv_now(pool)).get("listening"):
                raise HTTPException(409, "OPUS is not open on the television")
        except opus.OpusUnavailable as exc:
            raise _unavailable(exc) from exc
    try:
        found = await opus.queue(pool, body.kind, body.id, body.prefer)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc
    tracks = found.get("tracks") or []
    if not tracks:
        raise HTTPException(404, "nothing to play")
    source = f"user:{user.username}"
    if body.kind == "station":
        st = tracks[0]
        await request.app.state.bus.publish_command(await prepare_command(
            request.app.state.pool, body.player_id, "media_transport", "play_media",
            {"uri": st["uri"], "title": st.get("title") or "", "art": st.get("art") or ""},
            source=source))
        return {"ok": True, "title": st.get("title") or ""}
    # the library's id travels with each row: a streamer plays the address, and the
    # player's own television plays the record by its id
    items = [{"id": t["id"], "uri": t["uri"], "title": t.get("title") or "",
              "artist": t.get("artist") or "", "album": t.get("album") or "",
              "art": t.get("art") or "", "codec": t.get("codec"), "mime": "audio/flac"}
             for t in tracks]
    start = max(0, min(body.start, len(items) - 1))
    await request.app.state.bus.publish_command(await prepare_command(
        request.app.state.pool, body.player_id, "media_transport", "play_queue",
        {"queue": json.dumps(items), "start": start}, source=source))
    return {"ok": True, "title": found.get("title") or "", "count": len(items)}


@router.get("/tv/links")
async def tv_links(request: Request, user: AuthUser = Depends(current_user)) -> list[dict]:
    """The places the OPUS app on the television can be sent to."""
    try:
        return await opus.tv_links(request.app.state.pool)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get("/tv/items/{key}")
async def tv_items(key: str, request: Request, lang: str = "hr",
                   user: AuthUser = Depends(current_user)) -> list[dict]:
    """What can be opened in one of those places."""
    if not _PLACE.match(key) or lang not in ("hr", "en"):
        raise HTTPException(404, "no such place")
    try:
        return await opus.tv_items(request.app.state.pool, key, lang)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc


class TvOpenIn(BaseModel):
    link: str | None = None
    kind: str | None = None
    id: int | None = None
    lang: str = "hr"


@router.post("/tv/open")
async def tv_open(body: TvOpenIn, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Send the television somewhere, or open a thing on it. The same control
    boundary as every other command to it."""
    pool = request.app.state.pool
    if await is_hidden(pool, user, TV):
        raise HTTPException(404, "unknown player")
    await require_control(pool, user, TV, "media_transport")
    if body.link is not None:
        if not _PLACE.match(body.link):
            raise HTTPException(404, "no such place")
        order = {"link": body.link}
    elif body.kind in OPENS and body.id is not None and body.id > 0:
        order = {"kind": body.kind, "id": body.id, "lang": body.lang if body.lang in ("hr", "en") else "hr"}
    else:
        raise HTTPException(400, "nothing the television can open")
    try:
        await opus.tv_open(pool, order)
    except opus.OpusUnavailable as exc:
        raise _unavailable(exc) from exc
    except opus.OpusRefused as exc:
        if exc.status == 503:
            raise HTTPException(409, "OPUS is not open on the television") from exc
        raise HTTPException(exc.status if exc.status < 500 else 502, str(exc)) from exc
    log.info("opus tv: %s opened %s", user.username, order)
    return {"ok": True}
