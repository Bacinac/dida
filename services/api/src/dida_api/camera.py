"""Camera stream proxy — DIDA's own public face for any camera's video.

Camera sources (BABA's go2rtc on the private subnet, a Frigate NVR behind a
Cloudflare tunnel, a future IP cam) are NOT reachable from a remote browser, and
we don't want to expose each one publicly. Instead DIDA reverse-proxies each
stream through its OWN API: the browser only ever talks to the DIDA origin, so
camera video rides DIDA's existing Cloudflare tunnel and is gated by DIDA login.

The upstream URL is NOT hardwired to one server — it's read from the camera
ENTITY's own `camera` capability descriptor (the JSON each adapter publishes:
`{stream, snapshot, mp4, webrtc, hls}`). So this one proxy serves every camera
source, keyed by entity_id (identity is the entity_id, never the display name).

Endpoints, all plain HTTP (tunnel-robust — unlike WebRTC's UDP):
- /camera/{entity_id}/snapshot — one JPEG, for the grid tiles (polled).
- /camera/{entity_id}/mp4      — the camera's own stream as fragmented MP4 (MSE), live.
- /camera/{entity_id}/events   — recent recorded detections (the archive's evidence).
- /camera/{entity_id}/event/{event_id}/snapshot — one recorded event's frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from dida_core import AdapterConfig, set_app_setting
from dida_core.baba_sites import BabaSite, site_of
from dida_core.baba_sites import parse_sites as parse_baba_sites
from dida_core.frigate_sites import parse_sites as parse_frigate_sites
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, can_see_page, current_user, require_admin
from dida_api.visibility import is_hidden

log = logging.getLogger("dida.api.camera")
router = APIRouter(tags=["camera"])

# Entity ids are slug-ish with a colon namespace (baba:backyard, frigate:downstairs).
# Constrain the path param so it can't smuggle a slash/host into the lookup; the
# real guard is that the id must resolve to a stored camera descriptor (below).
_ENTITY_RE = re.compile(r"^[a-zA-Z0-9_:.-]{1,128}$")
# A Frigate event id ("1783598029.368621-1tr8dm") — caller-supplied, so pin its
# shape before it ever reaches the upstream URL (no slash/host smuggling).
_EVENT_ID_RE = re.compile(r"^[0-9]+\.[0-9]+-[a-zA-Z0-9]+$")
_LABEL_RE = re.compile(r"^[a-z_]{1,32}$")  # object class filter (person, car, …)
# A BABA track thumbnail filename ("<uuid>.jpg") — bare name, no path separators,
# and no leading dot, so it cannot be a dot-segment either.
_THUMB_RE = re.compile(r"^[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,159}$")


def _within(url: str, base: str) -> bool:
    """`url` is under `base` and stays there. A dot-segment, even percent-encoded,
    is refused rather than resolved: httpx collapses `..` before sending, and the
    upstream may decode `%2e%2e`, so either would walk out of the prefix."""
    base = base.rstrip("/")
    if not base or not url.startswith(f"{base}/"):
        return False
    return not any(seg in (".", "..") for seg in unquote(urlsplit(url).path).split("/"))


async def _allowed_bases(pool, entity_id: str, desc: dict, *, refresh: bool = False) -> tuple[str, ...]:
    """Where this camera's pixels and archive may be fetched from: the bases the
    admin configured for the location its descriptor names, in the adapter that
    owns the entity. Nothing else — a descriptor is a bus message, and a bus
    message naming a host is not a reason to send that host a location's keys."""
    namespace = entity_id.partition(":")[0]
    site = str(desc.get("site") or "")
    if namespace == "baba":
        found = site_of(parse_baba_sites(await _site_config(pool, "baba", refresh=refresh)), site)
        return (found.go2rtc, found.api_url) if found else ()
    if namespace == "frigate":
        for d in parse_frigate_sites(await _site_config(pool, "frigate", refresh=refresh)):
            if d["name"] == site:
                return (d["url"], d["go2rtc"])
    return ()


async def _confine(pool, entity_id: str, desc: dict) -> dict:
    """The descriptor with every upstream URL outside its location's configured
    bases removed — and said so, since a camera that loses its stream this way is
    either a misconfiguration or someone else's message. The config is re-read
    once before refusing, so a location added a moment ago is not refused for a
    minute."""
    urls = {k: v for k, v in desc.items()
            if isinstance(v, str) and v.startswith(("http://", "https://"))}
    if not urls:
        return desc
    bases = await _allowed_bases(pool, entity_id, desc)
    if not all(any(_within(u, b) for b in bases) for u in urls.values()):
        bases = await _allowed_bases(pool, entity_id, desc, refresh=True)
    refused = [k for k, u in urls.items() if not any(_within(u, b) for b in bases)]
    if refused:
        log.warning("camera %s: %s outside its location's configured hosts — not proxied",
                    entity_id, ", ".join(sorted(refused)))
    return {k: v for k, v in desc.items() if k not in refused}


async def _load_descriptor(pool, entity_id: str) -> dict | None:
    row = await pool.fetchval(
        "SELECT value FROM current_state WHERE entity_id = $1 AND capability = 'camera'",
        entity_id,
    )
    if not row:
        return None
    try:
        desc = json.loads(row)
    except (ValueError, TypeError):
        return None
    return await _confine(pool, entity_id, desc) if isinstance(desc, dict) else None


async def _camera_descriptor(request: Request, entity_id: str) -> dict | None:
    """The camera entity's published stream descriptor, or None if it isn't a
    known camera. This is the SSRF boundary: the caller only picks WHICH camera,
    and every upstream URL the descriptor names must sit under a base the admin
    configured for that camera's location (`_confine`)."""
    return await _load_descriptor(request.app.state.pool, entity_id)


def _upstream(desc: dict, key: str) -> str | None:
    url = desc.get(key)
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return None


# ── auth for gated camera sources (e.g. Frigate's authenticated :8971) ───────
# A descriptor may carry `"auth": "<adapter>"` — a NON-secret pointer telling us
# to log in server-side using credentials from that adapter's config (Fernet-
# encrypted in adapter_config; never in the browser-visible descriptor). The JWT
# is cached per origin and refreshed on a 401, so the browser only ever sees
# DIDA's own login.
_jwt_cache: dict[str, str] = {}
_jwt_lock = asyncio.Lock()


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _site_creds(sites_raw: str | None, origin: str) -> tuple[str, str] | None:
    """The (user, password) of the Frigate location whose URL matches this origin —
    so the proxy logs in with the RIGHT site's credentials when several NVRs share
    the one `frigate` adapter."""
    for d in parse_frigate_sites(sites_raw):
        if _origin(d["url"]) == origin:
            return (d["user"], d["password"]) if d["user"] and d["password"] else None
    return None


async def _frigate_login(origin: str, pool) -> str | None:
    creds = _site_creds(await _site_config(pool, "frigate", refresh=True), origin)
    if not creds:
        return None
    user, password = creds
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.post(f"{origin}/api/login", json={"user": user, "password": password})
    except Exception:
        log.warning("camera: frigate login failed", exc_info=True)
        return None
    if r.status_code != 200:
        return None
    tok = r.cookies.get("frigate_token")
    if tok:
        _jwt_cache[origin] = tok
    return tok


# A source's location list (with its secrets), cached briefly so the polled wall
# doesn't decrypt adapter config per frame.
_site_cache: dict[str, tuple[str | None, float]] = {}
_SITES_TTL = 60.0


async def _site_config(pool, adapter: str, *, refresh: bool = False) -> str | None:
    now = asyncio.get_running_loop().time()
    hit = _site_cache.get(adapter)
    if not refresh and hit and hit[1] > now:
        return hit[0]
    cfg = AdapterConfig(adapter, pool)
    await cfg.load()
    raw = cfg.get("sites")
    _site_cache[adapter] = (raw, now + _SITES_TTL)
    return raw


async def _baba_sites(pool, *, refresh: bool = False) -> list[BabaSite]:
    return parse_baba_sites(await _site_config(pool, "baba", refresh=refresh))


_SNAPSHOT_PATH = "/api/frame.jpeg"


def _scaled(url: str, width: int | None) -> str:
    """A tile-sized variant of a snapshot URL.

    The wall polls every camera every few seconds, and a full 2K frame for a
    320-px tile is ten times the bytes for no visible detail. On a remote site
    that difference decides whether anything else gets through: a house on a
    0.8 Mbit/s uplink cannot serve two full frames every 2.5 s AND a recorded
    clip at the same time. go2rtc resizes server-side, so the bytes never leave
    the far end. A source we don't know how to ask (Frigate publishes its own
    downscaled URL) is fetched as published rather than guessed at."""
    if not width or _SNAPSHOT_PATH not in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}width={width}"


# BABA's `still` is its detector ring capped at this long edge; a wider ask
# needs the full-resolution frame.
_STILL_MAX_W = 960


def _snapshot_url(desc: dict, width: int | None) -> str | None:
    """Where one frame of this camera at this width comes from."""
    still = _upstream(desc, "still")
    if still and width and width <= _STILL_MAX_W:
        return still
    url = _upstream(desc, "snapshot")
    return _scaled(url, width) if url else None


async def _auth_headers(desc: dict, url: str, pool, *, refresh: bool = False) -> dict:
    """Auth header(s) for an upstream fetch, or {} when the source is open.
    `refresh=True` forces a fresh login/config read (used once on a 401)."""
    adapter = desc.get("auth")
    if not isinstance(adapter, str) or not adapter:
        return {}
    if adapter == "baba":
        # Two planes, two credentials (go2rtc's basic auth, BABA's peer key),
        # chosen by which configured base the URL is under.
        site = site_of(await _baba_sites(pool, refresh=refresh), str(desc.get("site") or ""))
        return site.headers_for(url) if site else {}
    origin = _origin(url)
    if refresh:
        _jwt_cache.pop(origin, None)
    tok = _jwt_cache.get(origin)
    if not tok:
        async with _jwt_lock:  # collapse a thundering herd of first-frame logins
            tok = _jwt_cache.get(origin) or await _frigate_login(origin, pool)
    return {"cookie": f"frigate_token={tok}"} if tok else {}


# ── wall layout: a shared, admin-set display order for the tiles ─────────────


class LayoutIn(BaseModel):
    order: list[str] = Field(default_factory=list)
    # Per-tile grid size: entity_id -> {"c": colSpan, "r": rowSpan}. Cameras absent
    # from the map fall back to a default span, so a new camera still appears.
    spans: dict[str, dict[str, int]] = Field(default_factory=dict)


@router.get("/camera/layout")
async def get_layout(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """The saved wall arrangement — tile order + per-tile grid spans. Any authed
    user reads it (one shared wall); only admins can change it."""
    pool = request.app.state.pool
    row = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'camera_order'")
    spans_row = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'camera_spans'")
    try:
        order = json.loads(row) if row else []
    except (ValueError, TypeError):
        order = []
    try:
        spans = json.loads(spans_row) if spans_row else {}
    except (ValueError, TypeError):
        spans = {}
    return {
        "order": order if isinstance(order, list) else [],
        "spans": spans if isinstance(spans, dict) else {},
    }


@router.put("/camera/layout", status_code=204)
async def set_layout(
    body: LayoutIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    pool = request.app.state.pool
    await set_app_setting(pool, "camera_order", json.dumps([str(x) for x in body.order[:128]]))
    # Clamp spans to a sane grid range so a bad client can't store a 999-wide tile.
    clean = {
        str(k): {"c": max(1, min(12, int(v.get("c", 2)))), "r": max(1, min(6, int(v.get("r", 2))))}
        for k, v in list(body.spans.items())[:128]
        if isinstance(v, dict)
    }
    await set_app_setting(pool, "camera_spans", json.dumps(clean))


async def _guard(request: Request, entity_id: str, user: AuthUser) -> None:
    """Validate the entity id and enforce access on BOTH axes, exactly like
    /command: the page-level scope (an /entry-only guest can't reach cameras) AND
    the per-user view boundary (a view-hide rule on this camera makes it 404, so
    the raw proxy read path can't leak a stream the UI has already hidden)."""
    if not _ENTITY_RE.match(entity_id):
        raise HTTPException(400, "invalid camera id")
    if not can_see_page(user, "cameras"):
        raise HTTPException(403, "no access to cameras")
    if await is_hidden(request.app.state.pool, user, entity_id):
        raise HTTPException(404, "no camera")  # don't confirm a hidden camera's existence


async def _fetch_upstream(desc: dict, url: str, pool, *, timeout_s: float = 8.0) -> httpx.Response:
    """One authenticated GET at an upstream, retrying once with a fresh login on a
    401/403. Shared by the proxied tile and the snapshot a notification carries."""
    headers = await _auth_headers(desc, url, pool)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        try:
            r = await client.get(url, headers=headers)
            if r.status_code in (401, 403) and desc.get("auth"):
                headers = await _auth_headers(desc, url, pool, refresh=True)
                r = await client.get(url, headers=headers)
        except Exception:
            log.warning("camera: upstream %s failed", url, exc_info=True)
            raise HTTPException(502, "camera source unavailable") from None
    return r


async def _proxy_image(desc: dict, url: str, pool, *, timeout_s: float = 8.0) -> Response:
    """Fetch an upstream JPEG through the descriptor's server-side auth, refreshing
    the login once on a 401. Shared by the live snapshot and the recorded-event
    frames — same tunnel, same login, same failure handling. The polled tile keeps a
    tight timeout (fail fast, retry next poll); a deliberate one-off (a full event
    frame, which Frigate won't downscale) gets a longer one so a slow tunnel loads."""
    r = await _fetch_upstream(desc, url, pool, timeout_s=timeout_s)
    if r.status_code >= 400:
        raise HTTPException(404, "no frame")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"cache-control": "no-store"},
    )


# ── a frame a notification can carry ─────────────────────────────────────────
# A push notification's image is fetched by the phone's browser, NOT by the page:
# it carries no DIDA session, so a login-gated URL would arrive as a broken image
# on the one surface where the picture IS the message. The frame is therefore
# parked under an unguessable name that expires — one still frame, one link, gone
# by tomorrow — instead of opening the camera proxy itself.
_SNAP_DIR = Path("/state/snapshots")
_SNAP_TTL_S = 24 * 3600
_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}\.jpg$")
# The phone downloads the frame itself inside a few seconds and holds it decoded in
# memory: a doorbell's full 1920×2560 is ~470 kB on the wire and ~20 MB as a bitmap,
# and on mobile data or a dozing phone that is where the picture got lost.
# A notification is never wider than a phone screen.
_SNAP_WIDTH = 1080


def _snap_gc(now: float) -> None:
    for f in _SNAP_DIR.glob("*.jpg"):
        try:
            if now - f.stat().st_mtime > _SNAP_TTL_S:
                f.unlink(missing_ok=True)
        except OSError:
            pass


async def mint_snapshot(app, entity_id: str) -> str | None:
    """Grab one frame from a camera and return the path a notification can show.

    Accepts anything that names the camera — the camera entity itself, or one of
    its zone/bell/person children, which is what an automation's trigger actually
    holds (`baba:<cam>:zone:<id>`). Returns None on any failure: a notification
    without its picture still has to go out."""
    eid = (entity_id or "").strip()
    if not _ENTITY_RE.match(eid):
        return None
    pool = app.state.pool
    desc = None
    for candidate in (eid, ":".join(eid.split(":")[:2])):
        desc = await _load_descriptor(pool, candidate)
        if desc:
            break
    url = _upstream(desc, "snapshot") if desc else None
    if not url:
        return None
    try:
        r = await _fetch_upstream(desc, _scaled(url, _SNAP_WIDTH), pool, timeout_s=6.0)
    except HTTPException:
        return None
    if r.status_code >= 400 or not r.content:
        return None
    try:
        name = await asyncio.to_thread(_store_snap, r.content)
    except OSError:
        log.warning("camera: snapshot not stored", exc_info=True)
        return None
    return f"/api/snap/{name}"


def _store_snap(data: bytes) -> str:
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    _snap_gc(time.time())
    name = f"{secrets.token_urlsafe(24)}.jpg"
    (_SNAP_DIR / name).write_bytes(data)
    return name


SNAP_CTL = "dida.camera.snap"


async def serve_snap_requests(app) -> None:
    """Answer `dida.camera.snap` — {entity_id} → {"url": "/api/snap/…"}.

    The notify adapter has no camera credentials and no descriptor table; the api
    already owns both (and the SSRF boundary around them), so minting stays here
    and the adapter only ever learns a path."""

    async def _cb(msg) -> None:
        url = None
        try:
            req = json.loads(msg.data or b"{}")
            url = await mint_snapshot(app, str(req.get("entity_id", "")))
        except Exception:
            log.warning("snap request failed", exc_info=True)
        if msg.reply:
            await app.state.bus.nc.publish(msg.reply, json.dumps({"url": url}).encode())

    await app.state.bus.nc.subscribe(SNAP_CTL, cb=_cb)


@router.get("/snap/{name}")
async def serve_snapshot(name: str) -> Response:
    """Serve a minted frame. Deliberately unauthenticated — see above; the name is
    the capability, and it stops working on its own."""
    if not _SNAP_NAME_RE.match(name):
        raise HTTPException(404, "no frame")
    path = _SNAP_DIR / name
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(404, "no frame") from None
    if time.time() - st.st_mtime > _SNAP_TTL_S:
        path.unlink(missing_ok=True)
        raise HTTPException(404, "no frame")
    return Response(content=path.read_bytes(), media_type="image/jpeg",
                    headers={"cache-control": "private, max-age=3600"})


@router.get("/camera/{entity_id}/snapshot")
async def camera_snapshot(
    entity_id: str, request: Request, user: AuthUser = Depends(current_user),
    w: int | None = Query(None, ge=160, le=1920),
) -> Response:
    """One JPEG frame for a grid tile. Cheap; the UI polls it every few seconds so
    the wall stays live without N simultaneous video streams hammering the source.
    `w` asks the source for that pixel width — what a tile actually renders — so a
    polled wall doesn't pull full-resolution frames it will paint at a quarter size."""
    await _guard(request, entity_id, user)
    desc = await _camera_descriptor(request, entity_id)
    url = _snapshot_url(desc, w) if desc else None
    if not url:
        raise HTTPException(404, "no camera")
    return await _proxy_image(desc, url, request.app.state.pool)


@router.get("/camera/{entity_id}/events")
async def camera_events(
    entity_id: str, request: Request, user: AuthUser = Depends(current_user),
    label: str = "", limit: int = 8,
) -> dict:
    """Recent recorded detections for a camera (newest first) — the evidence behind
    a live badge. Proxies the source's events feed (Frigate) with its server-side
    login; a camera without an `events` descriptor just returns an empty list, so
    the UI can ask uniformly. Each item carries the base64 `thumbnail` Frigate
    already ships, so a filmstrip renders with no extra round-trips."""
    await _guard(request, entity_id, user)
    desc = await _camera_descriptor(request, entity_id)
    base = _upstream(desc, "events") if desc else None
    if not base or not desc.get("stream"):
        return {"events": []}
    lim = max(1, min(int(limit), 24))
    pool = request.app.state.pool
    baba = bool(desc.get("baba_id"))
    # BABA: ask for finalized tracks (complete "visits"), not the raw zone/park
    # event log — visits are 1:1 with a detection crop (thumbnail), a class and a
    # precise recording window, which is exactly what a wall filmstrip shows.
    params: dict[str, object] = (
        {"camera_id": desc["baba_id"], "kind": "track_finalized", "limit": lim} if baba
        else {"camera": desc["stream"], "limit": lim}
    )
    if label and _LABEL_RE.match(label) and not baba:
        params["label"] = label  # BABA filters by class below (its param is `kind`)
    headers = await _auth_headers(desc, base, pool)
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
        try:
            r = await client.get(base, params=params, headers=headers)
            if r.status_code in (401, 403) and desc.get("auth"):
                headers = await _auth_headers(desc, base, pool, refresh=True)
                r = await client.get(base, params=params, headers=headers)
        except Exception:
            log.warning("camera: events of %s failed", base, exc_info=True)
            raise HTTPException(502, "camera source unavailable") from None
    if r.status_code >= 400:
        return {"events": []}
    try:
        raw = r.json()
    except ValueError:
        return {"events": []}
    if baba:
        return {"events": _map_baba_events(raw, desc, label)}
    keep = ("id", "camera", "label", "sub_label", "start_time", "end_time",
            "has_snapshot", "has_clip", "top_score")
    events = [
        {k: e[k] for k in keep if k in e}
        for e in (raw if isinstance(raw, list) else [])
        if isinstance(e, dict) and e.get("id")
    ]
    return {"events": events}


def _iso_epoch(iso: object) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


# The longest clip an event may request. Kept WELL under the /clip endpoint's own
# 600 s abuse clamp, so a mapped event can never produce an unplayable window.
_CLIP_CAP_S = 120.0


def _map_baba_events(raw: object, desc: dict, label: str) -> list[dict]:
    """BABA's event rows → the wall's event shape (the one Frigate set). Clip
    bounds ride along (epoch seconds, padded for detector warm-up) so the UI can
    request the recorded clip without knowing whose archive is behind the proxy."""
    out: list[dict] = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        track = e.get("track") or {}
        rec = e.get("recording") or {}
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        cls = str(track.get("class_name") or payload.get("class_name") or "")
        if label and _LABEL_RE.match(label) and cls != label:
            continue
        start = _iso_epoch(rec.get("start_at")) or _iso_epoch(e.get("at"))
        end = _iso_epoch(rec.get("end_at")) or start
        thumb = str(track.get("thumbnail_path") or "").rsplit("/", 1)[-1]
        out.append({
            "id": str(e["id"]),
            "camera": desc.get("stream"),
            "label": cls or str(e.get("kind") or ""),
            "sub_label": str(e.get("kind") or ""),
            "start_time": start,
            "end_time": end,
            "has_snapshot": bool(thumb),
            "thumb": thumb,
            "has_clip": bool(rec) and start is not None,
            "clip_start": (start - 3.0) if (rec and start is not None) else None,
            # Capped: a long-lived event (a car parked in view for 35 minutes)
            # would otherwise ask for its WHOLE span, trip the /clip window clamp
            # and play nothing at all. The evidence is the beginning — the moment
            # the thing arrived — so the clip is the first two minutes, not none.
            "clip_end": min(end + 2.0, start - 3.0 + _CLIP_CAP_S) if (rec and end is not None) else None,
        })
    return out


@router.get("/camera/{entity_id}/event/{event_id}/snapshot")
async def camera_event_snapshot(
    entity_id: str, event_id: str, request: Request,
    user: AuthUser = Depends(current_user), thumb: bool = False,
) -> Response:
    """A recorded event's frame — the image that tripped the detection. `thumb=1`
    proxies Frigate's tiny thumbnail (~5 KB, for the filmstrip); otherwise the full
    snapshot (Frigate ignores downscale params on this route, so it's ~0.5–1 MB —
    hence a longer timeout, and it's a one-off, not polled). The archive stays in
    Frigate/BABA."""
    await _guard(request, entity_id, user)
    if not _EVENT_ID_RE.match(event_id):
        raise HTTPException(400, "invalid event id")
    desc = await _camera_descriptor(request, entity_id)
    base = _upstream(desc, "events") if desc else None
    if not base:
        raise HTTPException(404, "no events")
    variant = "thumbnail.jpg" if thumb else "snapshot.jpg"
    return await _proxy_image(desc, f"{base}/{event_id}/{variant}", request.app.state.pool,
                              timeout_s=8.0 if thumb else 20.0)


@router.get("/camera/{entity_id}/thumb/{name}")
async def camera_thumb(
    entity_id: str, name: str, request: Request, user: AuthUser = Depends(current_user)
) -> Response:
    """A BABA track thumbnail (the crop that identified the detection) — the
    filmstrip image for archives whose events don't inline a base64 thumbnail.
    `name` comes from this proxy's own events mapping; pinned to a bare filename
    so it can't traverse or smuggle a path into the upstream URL."""
    await _guard(request, entity_id, user)
    if not _THUMB_RE.match(name):
        raise HTTPException(400, "invalid thumbnail name")
    desc = await _camera_descriptor(request, entity_id)
    base = _upstream(desc, "thumbs") if desc else None
    if not base:
        raise HTTPException(404, "no thumbnails")
    return await _proxy_image(desc, f"{base}/{name}", request.app.state.pool)


@router.get("/camera/{entity_id}/clip")
async def camera_clip(
    entity_id: str, request: Request, start: float, end: float,
    user: AuthUser = Depends(current_user),
) -> StreamingResponse:
    """A recorded clip for an absolute time window — the moving evidence behind an
    event. Proxies the archive's cut (BABA serves faststart MP4 with Range
    support) so seeking works in a plain <video> through the tunnel; the window
    comes from the event's own clip bounds, clamped so a bad caller can't ask the
    archive to assemble an hour of video."""
    await _guard(request, entity_id, user)
    desc = await _camera_descriptor(request, entity_id)
    base = _upstream(desc, "clip") if desc else None
    if not base:
        raise HTTPException(404, "no recordings")
    if not (end > start) or (end - start) > 600:
        raise HTTPException(400, "bad clip window")
    pool = request.app.state.pool
    headers = await _auth_headers(desc, base, pool)
    rng = request.headers.get("range")
    if rng:
        headers["range"] = rng
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=8.0))
    req = client.build_request("GET", base, params={"start": start, "end": end}, headers=headers)
    try:
        upstream = await client.send(req, stream=True)
        if upstream.status_code in (401, 403) and desc.get("auth"):
            await upstream.aclose()
            headers = await _auth_headers(desc, base, pool, refresh=True)
            if rng:
                headers["range"] = rng
            upstream = await client.send(
                client.build_request("GET", base, params={"start": start, "end": end}, headers=headers),
                stream=True,
            )
    except Exception:
        log.warning("camera: clip stream failed", exc_info=True)
        await client.aclose()
        raise HTTPException(502, "camera source unavailable") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(404, "no clip")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough = {
        k: v for k, v in upstream.headers.items()
        if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")
    }
    passthrough["cache-control"] = "no-store"
    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)


@router.get("/camera/{entity_id}/mp4")
async def camera_mp4(
    entity_id: str, request: Request, user: AuthUser = Depends(current_user)
) -> StreamingResponse:
    """The camera's own encoded stream as fragmented MP4, for a MediaSource
    player: plain HTTP, so it proxies cleanly through the tunnel (WebRTC's UDP
    does not). Full resolution without a transcode, which go2rtc's MJPEG cannot
    offer: with no encoder behind it, that route answers an empty body. The
    content type carries the codec string MSE needs, so it is passed through.
    Long-lived; torn down when the viewer closes and the client disconnects."""
    await _guard(request, entity_id, user)
    desc = await _camera_descriptor(request, entity_id)
    url = _upstream(desc, "mp4") if desc else None
    if not url:
        raise HTTPException(404, "no camera")
    pool = request.app.state.pool
    headers = await _auth_headers(desc, url, pool)
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=8.0))
    try:
        upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        if upstream.status_code in (401, 403) and desc.get("auth"):
            await upstream.aclose()
            headers = await _auth_headers(desc, url, pool, refresh=True)
            upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    except Exception:
        log.warning("camera: mp4 stream failed", exc_info=True)
        await client.aclose()
        raise HTTPException(502, "camera source unavailable") from None
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(404, "no stream")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers={"cache-control": "no-store"},
    )
