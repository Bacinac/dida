"""Native Android companion app (android/) — provisioning + sideload distribution.

The app is a WebView shell over the normal UI plus a native background-location
layer that speaks the existing OwnTracks protocol (see `dida_api.owntracks`), so
presence needed no server changes. What DID need a server side:

- `/me/mobile-config` — session-authenticated, one-shot during onboarding: hands
  the app its endpoint-scoped location credentials (the same `owntracks_token`
  the OwnTracks app used) plus the current zone set as waypoints for native
  geofences. After this call the app never uses the web session for reporting.
- `/me/app-link` — browser→app handoff: the signed-in page wraps the user's one
  reusable login token as an intent:// URL so the freshly installed app opens
  signed in. It never mints a new token while one is live — the QR stays valid.
- `/app/dida.apk` + `/app/apk.json` — sideload distribution and the self-update
  metadata (versionCode/sha256). Unauthenticated like /version: the update check
  runs before any session exists, and the APK is not a secret. The artifacts are
  built by android/build.sh on the dev box and bind-mounted read-only (compose:
  ./android/dist → /apk); a missing artifact 404s loudly, never silently.
- `/app/crash-report` — the apps' uncaught-exception handler stores the trace on
  the phone and uploads it on the next start; the report becomes an ERROR row in
  ClickHouse app_logs (via the same dida.logs stream every service uses), so a
  phone crash is diagnosed in the same log viewer as a service crash.
- `/app/location-status` — the answer to a wake push (notify adapter): whether
  the phone armed its location engine or why it could not. Without it a phone
  that refuses to arm and a phone that never got the message look identical from
  here, which is how one stayed dark for weeks. Authenticated with the location
  token, the only credential the app holds outside a WebView session.
- `/app/push-image` — a notification whose camera frame never made it onto the
  phone. The server can see that it minted the frame and that something fetched
  it, never which phone did not; the app says so, on the same credential.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path

from dida_core import LogRecord
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from home_core.auth import encode_session_token
from pydantic import BaseModel, Field

from dida_api import owntracks
from dida_api.auth import AuthUser, current_user
from dida_api.users import ensure_login_token

log = logging.getLogger("dida.api.mobile")

router = APIRouter(tags=["mobile"])

_APK_DIR = Path(os.environ.get("DIDA_APK_DIR", "/apk"))

# The car app cannot re-run an interactive login (its UI is the car screen), so
# its bearer token lives for years; revocation is the user's token_version bump,
# identical to killing web sessions.
_CAR_TOKEN_TTL = timedelta(days=3650)


@router.get("/me/mobile-config")
async def my_mobile_config(request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Self-service provisioning for the native app: exchanges the (short-lived)
    web session for the long-lived, endpoint-scoped location credentials. The
    token can only POST to /owntracks — it cannot log into the UI — so a lost
    phone is revoked by rotating it, exactly like an OwnTracks phone was."""
    pool = request.app.state.pool
    username, token = await owntracks.ensure_location_token(pool, user.id)
    public = await owntracks.public_base_url(pool)
    if not owntracks.is_reachable(public):
        # Fail loud: credentials that can only ever POST to a loopback origin
        # would arm a reporter that goes dark the moment the phone leaves WiFi.
        raise HTTPException(
            503, "DIDA_PUBLIC_URL is not externally reachable — set it to the tunnel origin"
        )
    return {
        "username": username,
        "token": token,
        "url": f"{public}/api/owntracks",
        # Same waypoint dicts the OwnTracks reply channel pushes, so the app has
        # ONE parser for both the bootstrap and later setWaypoints syncs.
        "waypoints": await owntracks.zone_waypoints(pool),
    }


@router.post("/me/app-link")
async def my_app_link(request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """An auto-login URL for the CURRENT user — the browser→app handoff. Carries
    the user's ONE existing login token (minted only if none is live): tokens are
    reusable and there is a single token per user, so minting a fresh short-lived
    one here would silently kill the setup QR the admin is still showing. That
    was a live bug: every tap of the setup button invalidated the QR."""
    public = await owntracks.public_base_url(request.app.state.pool)
    if not owntracks.is_reachable(public):
        raise HTTPException(
            503, "the public address is not externally reachable — set it in Settings → Network"
        )
    token = await ensure_login_token(request.app.state.pool, user.id)
    return {"url": f"{public}/api/auth/link?k={token}&next=/onboard"}


class FcmTokenIn(BaseModel):
    token: str = Field(..., min_length=16, max_length=4096)


@router.post("/me/fcm-token", status_code=204)
async def register_fcm_token(
    body: FcmTokenIn, request: Request, user: AuthUser = Depends(current_user)
) -> None:
    """Register this install's FCM token for the signed-in user — native push for
    the Android app (its WebView has no Web Push). Re-registering a token under a
    different user MOVES it (a handed-over phone notifies whoever signs in). The
    notify adapter reads these rows and prunes any token FCM reports gone."""
    ua = (request.headers.get("user-agent") or "")[:200]
    await request.app.state.pool.execute(
        "INSERT INTO fcm_tokens (user_id, token, user_agent) VALUES ($1, $2, $3) "
        "ON CONFLICT (token) DO UPDATE SET user_id = EXCLUDED.user_id, "
        "user_agent = EXCLUDED.user_agent",
        user.id, body.token, ua,
    )


@router.post("/me/car-token")
async def my_car_token(request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Long-lived bearer credential for DIDA Auto (Android Auto companion).

    The same JWT the session cookie carries, just with a decade TTL and sent as
    `Authorization: Bearer` — one verification path in current_user, one
    revocation model (token_version)."""
    token = encode_session_token(
        user.id, request.app.state.secret_key, token_version=user.token_version, ttl=_CAR_TOKEN_TTL
    )
    return {"token": token}


class CrashReportIn(BaseModel):
    app: str = Field(..., max_length=100)
    version: str = Field("", max_length=50)
    thread: str = Field("", max_length=200)
    stack: str = Field(..., min_length=1, max_length=16000)
    occurred_at_ms: int = Field(..., ge=0)
    device: str = Field("", max_length=200)


# Only our own packages may file reports; the value is the app_logs `service`
# label, so phone crashes filter in the log viewer exactly like service crashes.
_CRASH_SERVICES = {
    "biz.boskovic.dida": "app-android",
    "biz.boskovic.dida.auto": "app-auto",
}

#: A report can legitimately arrive long after the crash (the phone retries on
#: app start), but an absurd client clock must not file rows outside the log
#: retention window where nobody would ever see them.
_CRASH_MAX_AGE_NS = 90 * 86400 * 1_000_000_000


class LocationStatusIn(BaseModel):
    outcome: str = Field(..., max_length=40)
    version: str = Field("", max_length=50)
    device: str = Field("", max_length=200)


#: What the app may report back. An unknown value is a client/server version
#: mismatch worth seeing, so it is logged rather than rejected.
_ARMED = "armed"


@router.post("/app/location-status", status_code=204)
async def location_status(body: LocationStatusIn, request: Request) -> None:
    """A phone's answer to a wake push. `armed` means background reporting is
    running again and a fix should follow within the minute; anything else names
    the reason it cannot, which is the part no server-side check can observe."""
    username = await owntracks.basic_user(request)
    level = "INFO" if body.outcome == _ARMED else "WARNING"
    await request.app.state.bus.publish_log(LogRecord(
        ts_ns=time.time_ns(),
        service="app-android",
        level=level,
        logger="location",
        message=(
            f"{username}: location engine {body.outcome}"
            f" ({body.version or 'unknown version'} on {body.device or 'unknown device'})"
        )[:4000],
        exc="",
    ))


class PushImageIn(BaseModel):
    failure: str = Field(..., min_length=1, max_length=300)
    version: str = Field("", max_length=50)
    device: str = Field("", max_length=200)


@router.post("/app/push-image", status_code=204)
async def push_image(body: PushImageIn, request: Request) -> None:
    """A phone showed a notification without the camera frame it was sent. The
    app shows the text regardless, so without this the loss is invisible."""
    username = await owntracks.basic_user(request)
    await request.app.state.bus.publish_log(LogRecord(
        ts_ns=time.time_ns(),
        service="app-android",
        level="WARNING",
        logger="push",
        message=(
            f"{username}: notification picture lost — {body.failure}"
            f" ({body.version or 'unknown version'} on {body.device or 'unknown device'})"
        )[:4000],
        exc="",
    ))


@router.post("/app/crash-report", status_code=204)
async def crash_report(
    body: CrashReportIn, request: Request, user: AuthUser = Depends(current_user)
) -> None:
    """A crash from one of the Android apps, uploaded on the start AFTER the one
    that died. Published as a LogRecord onto dida.logs — the journal sink lands
    it in app_logs, and the viewer's service filter picks the new label up on
    its own. Authenticated (session cookie for the family app, car-token bearer
    for Auto/Music): crashes are diagnostics, not an anonymous write path."""
    service = _CRASH_SERVICES.get(body.app)
    if service is None:
        raise HTTPException(422, f"unknown app id {body.app!r}")
    now_ns = time.time_ns()
    ts_ns = min(body.occurred_at_ms * 1_000_000, now_ns)
    if ts_ns < now_ns - _CRASH_MAX_AGE_NS:
        ts_ns = now_ns
    first_line = body.stack.strip().splitlines()[0][:300]
    bus = request.app.state.bus
    record = LogRecord(
        ts_ns=ts_ns,
        service=service,
        level="ERROR",
        logger="crash",
        message=(
            f"{body.version} crashed on {body.device} "
            f"(user {user.username}, thread {body.thread}): {first_line}"
        )[:4000],
        exc=body.stack[:8000],
    )
    # No local log line: the api's own logs ride the same stream, and mirroring
    # the report there would file every crash twice.
    await bus.publish_log(record)


def _apk_meta(meta: str) -> dict:
    try:
        return json.loads((_APK_DIR / meta).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(
            404, f"{meta} artifact missing — android/build.sh builds it (dev), deploy ships it"
        ) from None
    except ValueError:
        log.error("%s in %s is corrupt", meta, _APK_DIR)
        raise HTTPException(500, f"{meta} is corrupt — rebuild with android/build.sh") from None


@router.get("/app/apk.json")
async def apk_meta() -> dict:
    """Self-update metadata (versionCode / versionName / sha256) of the served
    APK. Generated by android/build.sh next to the APK itself."""
    return _apk_meta("apk.json")


@router.get("/app/auto.json")
async def auto_apk_meta() -> dict:
    """DIDA Auto artifact metadata — the account page gates its download card on
    this (404 = no artifact, hide the card)."""
    return _apk_meta("auto.json")


def _apk_response(stem: str, meta: str) -> FileResponse:
    """One sideload artifact, served safely: loud 404 when missing, versioned
    download name (a Downloads folder full of identical files made stale
    sideloads indistinguishable during the first rollout — the URL stays
    stable, only Content-Disposition varies), and no-store because Cloudflare
    caches .apk by default — a cached copy would serve stale bytes after a
    deploy and fail the app's sha256 check (bitten live). Clients additionally
    version the query (?v=) so any edge cache splits keys."""
    apk = _APK_DIR / f"{stem}.apk"
    if not apk.is_file():
        raise HTTPException(
            404, f"{stem}.apk artifact missing — android/build.sh builds it (dev), deploy ships it"
        )
    version = ""
    # Metadata missing/corrupt → plain name; the download itself still works.
    with contextlib.suppress(OSError, ValueError):
        version = json.loads((_APK_DIR / meta).read_text(encoding="utf-8")).get(
            "versionName", ""
        )
    # A UNIQUE filename per download: the browser never prompts "download again?"
    # (the file it already has has a different name), which was the friction that
    # made re-installing feel broken. The suffix is cosmetic — the bytes are the
    # served build regardless.
    uniq = secrets.token_hex(3)
    name = f"{stem}-{version}-{uniq}.apk" if version else f"{stem}-{uniq}.apk"
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename=name,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/app/dida.apk")
async def apk_download() -> FileResponse:
    return _apk_response("dida", "apk.json")


@router.get("/app/dida-auto.apk")
async def auto_apk_download() -> FileResponse:
    """Direct APK of DIDA Auto (the Android Auto companion). The phone half —
    pairing, car-token mint — is fully testable from this sideload; the car
    screen itself only appears once the app arrives via the Play internal track
    (templated AA apps cannot sideload), and that build is Play-signed, so
    switching to it means uninstalling this one first."""
    return _apk_response("dida-auto", "auto.json")
