"""SmartThings OAuth handshake (adapter-level, one house account).

SmartThings killed long-lived Personal Access Tokens (new PATs expire in 24h),
so the durable path is a self-registered OAuth client whose refresh token rolls
on every use. This module owns the one-time authorization_code exchange; the
`smartthings` adapter owns the ongoing rolling refresh.

Unlike the per-user music services (services.py), this is a single integration
for the whole home, so the tokens live in the adapter-managed `_oauth` row of
`adapter_config` (encrypted with the shared DIDA_SECRET_KEY the adapter reads),
NOT the per-user tokens table. Only client_id/secret/redirect_uri are user-
editable config fields; the tokens are minted here, never typed.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import time
import urllib.parse
from urllib.parse import urlsplit

import httpx
import jwt
from dida_core import decrypt_secret, encrypt_secret
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from dida_api.auth import AuthUser, require_admin

log = logging.getLogger("dida.api.smartthings")

router = APIRouter(prefix="/smartthings", tags=["smartthings"])

_AUTHORIZE = "https://api.smartthings.com/oauth/authorize"
_TOKEN = "https://auth-global.api.smartthings.com/oauth/token"
# Read + control devices; locations for enumeration. Must match the scopes
# whitelisted on the OAuth app.
_SCOPES = "r:devices:* x:devices:* r:locations:*"


def _secret() -> str:
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(500, "DIDA_SECRET_KEY is not set — SmartThings tokens cannot be saved.")
    return secret


async def _cfg(request: Request) -> dict:
    """The smartthings adapter config the OAuth flow needs (client creds +
    redirect), plus whether it's already connected."""
    rows = await request.app.state.pool.fetch(
        "SELECT key, value FROM adapter_config WHERE adapter = 'smartthings'"
    )
    d = {r["key"]: r["value"] for r in rows}
    secret = _secret()
    client_secret = (
        decrypt_secret(secret, d["client_secret"], adapter="smartthings", key="client_secret")
        if d.get("client_secret") else ""
    )
    # Redirect URI defaults to the installation's public origin (Settings → Network,
    # already set to the Cloudflare tunnel for Spotify) so the user doesn't type it
    # twice — only registers it once at SmartThings. An explicit field overrides.
    redirect_uri = (d.get("redirect_uri") or "").strip()
    if not redirect_uri:
        from dida_core import host_setting

        base = (await host_setting(request.app.state.pool, "public_url")).rstrip("/")
        redirect_uri = f"{base}/api/smartthings/callback" if base else ""
    return {
        "client_id": (d.get("client_id") or "").strip(),
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "connected": "_oauth" in d,
    }


async def _app_origin(pool, redirect_uri: str) -> str:
    """Where to send the browser after the dance — the SAME origin it is on (the
    redirect_uri's scheme+host, i.e. the Cloudflare tunnel). Falls back to the
    installation's app URL (Settings → Network) for the error-before-config case."""
    p = urlsplit(redirect_uri)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    from dida_core import host_setting

    base = await host_setting(pool, "app_url")
    return (base or await host_setting(pool, "public_url")).rstrip("/")


@router.get("/status")
async def status(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    cfg = await _cfg(request)
    return {
        "configured": bool(cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]),
        "connected": cfg["connected"],
    }


@router.get("/locations")
async def locations(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """The account's SmartThings locations [{id, name}], published to app_settings by
    the adapter on connect. Feeds the home-location picker in the ST card."""
    raw = await request.app.state.pool.fetchval(
        "SELECT value FROM app_settings WHERE key = 'smartthings_locations'"
    )
    try:
        items = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        items = []
    return {"locations": items}


@router.get("/login")
async def login(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    cfg = await _cfg(request)
    if not (cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]):
        raise HTTPException(503, "Unesi Client ID, Client Secret i Redirect URI pa spremi.")
    # Signed state: CSRF + survives the top-level redirect (no cookie reliance).
    state = jwt.encode(
        {"a": "smartthings", "n": pysecrets.token_hex(8), "exp": int(time.time()) + 600},
        request.app.state.secret_key, algorithm="HS256",
    )
    params = {
        "client_id": cfg["client_id"], "response_type": "code",
        "redirect_uri": cfg["redirect_uri"], "scope": _SCOPES, "state": state,
    }
    return {"url": f"{_AUTHORIZE}?{urllib.parse.urlencode(params)}"}


@router.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    cfg = await _cfg(request)
    origin = await _app_origin(request.app.state.pool, cfg["redirect_uri"])

    def back(result: str, reason: str = "") -> RedirectResponse:
        q = f"smartthings={result}" + (f"&reason={urllib.parse.quote(reason)}" if reason else "")
        return RedirectResponse(f"{origin}/settings/adapters?{q}")

    if error:
        return back("error", error)
    if not (cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]) or not code or not state:
        return back("error", "config")
    try:
        jwt.decode(state, request.app.state.secret_key, algorithms=["HS256"])  # CSRF + expiry
    except jwt.PyJWTError:
        return back("error", "state")

    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.post(
            _TOKEN,
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": cfg["redirect_uri"], "client_id": cfg["client_id"]},
            auth=(cfg["client_id"], cfg["client_secret"]),
        )
    if r.status_code != 200:
        log.warning("smartthings code exchange failed: %s %s", r.status_code, r.text[:200])
        return back("error", "token")
    tok = r.json()
    if not tok.get("access_token") or not tok.get("refresh_token"):
        # Without the refresh token the connection dies in 24 h with no way back —
        # the same end state as never connecting, reached a day later and silently.
        log.warning("smartthings token response has no refresh_token: %s", sorted(tok))
        return back("error", "token")
    blob = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 86400)) - 60,
    }
    await request.app.state.pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ('smartthings', '_oauth', $1) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value",
        encrypt_secret(_secret(), json.dumps(blob)),
    )
    log.info("smartthings: connected — tokens stored")
    return back("connected")


@router.post("/disconnect")
async def disconnect(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    await request.app.state.pool.execute(
        "DELETE FROM adapter_config WHERE adapter = 'smartthings' AND key = '_oauth'"
    )
    return {"ok": True}
