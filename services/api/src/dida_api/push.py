"""Web Push (PWA obavijesti) — subscription management + VAPID keypair.

The DIDA UI is an installable PWA; its service worker receives Web Push
messages, so an automation's `notify` command lands on the family's phones
with no third-party app. Delivery itself lives in the notify adapter
(adapters/notify) — ONE delivery path for ntfy and Web Push alike. This module
owns only what must sit at the authenticated HTTP boundary:

- the VAPID application keypair: generated once at boot into app_settings
  (public plain — browsers subscribe against it; private Fernet-encrypted with
  the shared DIDA_SECRET_KEY so the notify adapter can sign sends),
- per-user browser subscriptions (push_subscriptions),
- a test send, published as a normal `notify` command on the bus so a passing
  test proves the exact bus → adapter → push-service path an automation uses,
- the notify-target directory for the automations builder: notify:* entities
  are write-only (no state), so they never appear in /state — this endpoint is
  how the UI learns they exist.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from dida_core import encrypt_secret, ensure_app_setting, prepare_command, slug
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.common import get_setting

log = logging.getLogger("dida.api.push")

router = APIRouter(tags=["push"])


async def ensure_vapid(pool) -> None:
    """Generate the VAPID application keypair on first boot. The public key is
    what every browser subscription is bound to — rotating it would silently
    strand all stored subscriptions, so once generated it is never replaced."""
    if await get_setting(pool, "webpush_vapid_public"):
        return
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02, b64urlencode

    v = Vapid02()
    v.generate_keys()
    public = b64urlencode(
        v.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    raw_priv = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    private = encrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), b64urlencode(raw_priv))
    async with pool.acquire() as conn, conn.transaction():
        await ensure_app_setting(conn, "webpush_vapid_public", public)
        await ensure_app_setting(conn, "webpush_vapid_private", private)
    log.info("web push: generated VAPID application keypair")


@router.get("/push/vapid-key")
async def vapid_key(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    key = await get_setting(request.app.state.pool, "webpush_vapid_public")
    if not key:
        # ensure_vapid runs in lifespan, so this only happens if keygen failed loudly.
        raise HTTPException(503, "web push is not initialized")
    return {"key": key}


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=512)
    auth: str = Field(..., min_length=1, max_length=256)


# The notify adapter POSTs to whatever endpoint is stored, so an endpoint outside
# the browsers' own push services would turn it into a request to anywhere.
PUSH_SERVICES = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")


class PushSubscribeIn(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def _a_browser_push_service(cls, v: str) -> str:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.port not in (None, 443) or not any(
                host == s or host.endswith("." + s) for s in PUSH_SERVICES):
            raise ValueError("not a browser push service endpoint")
        return v


@router.post("/push/subscribe", status_code=204)
async def push_subscribe(
    body: PushSubscribeIn, request: Request, user: AuthUser = Depends(current_user)
) -> None:
    """Store this browser's push subscription for the logged-in user. An endpoint
    another user already holds stays theirs: turning notifications off and on again
    subscribes the browser afresh, under a new endpoint."""
    ua = (request.headers.get("user-agent") or "")[:200]
    stored = await request.app.state.pool.fetchval(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (endpoint) DO UPDATE SET "
        "p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent "
        "WHERE push_subscriptions.user_id = EXCLUDED.user_id RETURNING true",
        user.id, body.endpoint, body.keys.p256dh, body.keys.auth, ua,
    )
    if not stored:
        raise HTTPException(409, "this subscription belongs to another user")


class PushEndpointIn(BaseModel):
    endpoint: str = Field(..., min_length=12, max_length=2048)


@router.post("/push/owner")
async def push_owner(
    body: PushEndpointIn, request: Request, user: AuthUser = Depends(current_user)
) -> dict:
    """Whose notifications this browser's subscription delivers: the caller's, another
    user's, or nobody's. A browser holds one subscription whoever is logged in, so
    its mere existence says nothing about the current user."""
    holder = await request.app.state.pool.fetchval(
        "SELECT user_id FROM push_subscriptions WHERE endpoint = $1", body.endpoint
    )
    return {"owner": "none" if holder is None else "self" if holder == user.id else "other"}


@router.post("/push/unsubscribe", status_code=204)
async def push_unsubscribe(
    body: PushEndpointIn, request: Request, user: AuthUser = Depends(current_user)
) -> None:
    # Own subscriptions only — one user can't silence another's device.
    await request.app.state.pool.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = $1 AND user_id = $2",
        body.endpoint, user.id,
    )


class PushTestIn(BaseModel):
    message: str | None = Field(default=None, max_length=500)


@router.post("/push/test")
async def push_test(
    body: PushTestIn, request: Request, user: AuthUser = Depends(current_user)
) -> dict:
    """Fire a `notify` command at the caller's own notify entity — the exact
    path an automation uses, so a received notification proves the whole chain
    (bus → notify adapter → push service → service worker)."""
    n = await request.app.state.pool.fetchval(
        "SELECT (SELECT count(*) FROM push_subscriptions WHERE user_id = $1) "
        "     + (SELECT count(*) FROM fcm_tokens WHERE user_id = $1)", user.id
    )
    if not n:
        raise HTTPException(400, "no push subscription or app on this account yet")
    await request.app.state.bus.publish_command(await prepare_command(
        request.app.state.pool, f"notify:{slug(user.username)}", "notify", "notify",
        {"title": "DIDA", "message": (body.message or "").strip() or "Probna obavijest"},
        source=f"user:{user.username}"))
    return {"ok": True, "subscriptions": int(n)}


@router.get("/notify/targets")
async def notify_targets(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Directory of notification targets for the automations builder: ntfy
    targets from the notify adapter's config + every user holding at least one
    web-push subscription + the notify:all broadcast (labelled client-side).

    Admin-only: the list enumerates every user who has a push subscription (a
    house-roster leak to a narrow login), and its only caller is the admin-only
    automations builder page."""
    pool = request.app.state.pool
    out: dict[str, str] = {}
    raw = await pool.fetchval(
        "SELECT value FROM adapter_config WHERE adapter = 'notify' AND key = 'targets'"
    )
    for part in (raw or "").split(","):
        if ":" in part:
            name, topic = part.split(":", 1)
            if name.strip() and topic.strip():
                out[f"notify:{slug(name)}"] = name.strip()
    rows = await pool.fetch(
        "SELECT DISTINCT u.username FROM push_subscriptions ps "
        "JOIN users u ON u.id = ps.user_id ORDER BY u.username"
    )
    for r in rows:
        u = r["username"]
        out.setdefault(f"notify:{slug(u)}", u[:1].upper() + u[1:])
    targets = [
        {"entity_id": k, "label": v}
        for k, v in sorted(out.items(), key=lambda kv: kv[1].lower())
    ]
    if targets:
        targets.append({"entity_id": "notify:all", "label": ""})
    return targets
