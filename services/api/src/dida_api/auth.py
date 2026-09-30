"""Who is signed in to DIDA: users by id with page visibility and device
control, per-token sign-out, the wall panel's key, and a sliding session.
Passwords, the signed token and its cookie are home_core.auth.

Per-user revocation is the `token_version` counter in the users row (bumped on
password change) compared against the token's `tv` claim; signing out one
device records that token in `revoked_sessions`.

The cookie is Secure whenever the request arrived over HTTPS (the Cloudflare
tunnel), so the long-lived token is never sent back in the clear on that path,
and plain on LAN HTTP so the same cookie still works there.
DIDA_COOKIE_SECURE=true forces Secure on.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import jwt
from dida_core import ensure_app_setting
from fastapi import Depends, HTTPException, Request, Response
from home_core.auth import (
    ALGO,
    SessionCookie,
    decode_session_token,
    encode_session_token,
    hash_password,
)

log = logging.getLogger("dida.api.auth")

# Effectively permanent (a decade), sliding. A household has occasional guests —
# friends given access who may not open the app for months and must NOT be
# logged out between visits. Revocation here is EXPLICIT, not time-based:
# "remove access" bumps the user's token_version and kills every session at once.
# The only thing a shorter expiry would add is auto-cleanup of a session you
# forgot to revoke — a safety net we trade away for guests who never expire.
TOKEN_TTL = timedelta(days=3650)

SESSION_COOKIE = SessionCookie(
    "dida_session",
    TOKEN_TTL,
    always_secure=os.environ.get("DIDA_COOKIE_SECURE", "").lower() == "true",
)


@dataclass(slots=True, frozen=True)
class AuthUser:
    id: int
    username: str
    role: str
    token_version: int = 0
    # Per-user page visibility. None = full consumer access (default); a list =
    # exactly those page keys. Admins ignore it. See migration 0013.
    allowed_pages: list[str] | None = None
    # Baseline for device control (Phase 2). True = may operate devices; False =
    # view-only. Scoped exceptions live in user_access_rules. See migration 0014.
    can_control: bool = True
    # UI preferences (migration 0029). None = no explicit choice → the client uses
    # its device default; otherwise these follow the user across devices.
    theme: str | None = None
    locale: str | None = None


def _auth_user(row: asyncpg.Record) -> AuthUser:
    return AuthUser(
        id=int(row["id"]),
        username=row["username"],
        role=row["role"],
        token_version=int(row["token_version"]),
        allowed_pages=row["allowed_pages"],
        can_control=row["can_control"],
        theme=row["theme"],
        locale=row["locale"],
    )


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def request_token(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE.name)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    return token or None


async def session_user(pool: asyncpg.Pool, token: str, secret: str) -> AuthUser:
    """The one check every session passes, over HTTP and the live socket: signed and
    unexpired, the user still exists, the password has not changed since (`tv`), and
    this token has not been signed out."""
    user_id, token_tv = decode_session_token(token, secret, int)
    row = await pool.fetchrow(
        "SELECT id, username, role, token_version, allowed_pages, can_control, theme, locale, "
        "EXISTS (SELECT 1 FROM revoked_sessions WHERE token_hash = $2) AS signed_out "
        "FROM users WHERE id = $1",
        user_id, _token_hash(token),
    )
    if row is None:
        raise HTTPException(401, "user no longer exists")
    if row["signed_out"] or int(row["token_version"]) != token_tv:
        raise HTTPException(401, "session revoked, please sign in again")
    return _auth_user(row)


async def revoke_session(pool: asyncpg.Pool, token: str, secret: str) -> None:
    """Sign out this one token; the user's other devices stay signed in."""
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGO])
        user_id = int(payload["sub"])
        expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return
    await pool.execute(
        "WITH expired AS (DELETE FROM revoked_sessions WHERE expires_at < now()) "
        "INSERT INTO revoked_sessions (token_hash, user_id, expires_at) "
        "SELECT $1, id, $3 FROM users WHERE id = $2 ON CONFLICT DO NOTHING",
        _token_hash(token), user_id, expires_at,
    )


async def current_user(request: Request) -> AuthUser:
    """FastAPI dependency: the session cookie, or the same JWT as
    `Authorization: Bearer` from a headless client (DIDA Auto), through
    `session_user`."""
    token = request_token(request)
    if not token:
        raise HTTPException(401, "not authenticated")
    return await session_user(request.app.state.pool, token, request.app.state.secret_key)


async def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(403, "administrators only")
    return user


def can_see_page(user: AuthUser, *pages: str) -> bool:
    """Server-side page-visibility check: True if the user may access ANY of the
    named pages. Admins and unrestricted users (allowed_pages is None) always can.
    The single authority every page-gated read endpoint mirrors — the frontend nav
    is a convenience, this is the boundary."""
    if user.role == "admin" or user.allowed_pages is None:
        return True
    return any(p in user.allowed_pages for p in pages)


# --- wall panel (living-room display auto-login) -------------------------

WALLPANEL_USERNAME = "wallpanel"


async def fetch_user_by_username(pool: asyncpg.Pool, username: str) -> AuthUser | None:
    row = await pool.fetchrow(
        "SELECT id, username, role, token_version, allowed_pages, can_control, theme, locale "
        "FROM users WHERE username = $1",
        username,
    )
    return None if row is None else _auth_user(row)


async def ensure_wallpanel(pool: asyncpg.Pool) -> None:
    """Seed the wall-panel user + its stable panel token (idempotent).

    The living-room display auto-logs-in by presenting this token in its cast URL
    (see the `/auth/panel` endpoint), never by password — so the password is an
    unusable random. `role='user'` (non-admin): the panel can view + operate the
    house, but can never reach admin config. Revoke access by regenerating the
    `panel_token` row (a fresh cast URL then supersedes it)."""
    # can_control is stated, not inherited: the column defaults to FALSE (least
    # privilege for a person an admin creates — migration 0032), but operating the
    # house IS this account's whole purpose, so it must say so for itself. Without
    # this, a fresh install would cast a panel that can only look.
    await pool.execute(
        "INSERT INTO users (username, password_hash, role, can_control) VALUES ($1, $2, 'user', true) "
        "ON CONFLICT (username) DO NOTHING",
        WALLPANEL_USERNAME,
        await hash_password(secrets.token_urlsafe(32)),
    )
    await ensure_app_setting(pool, "panel_token", secrets.token_urlsafe(24))


async def panel_cast_url(pool: asyncpg.Pool, base_url: str) -> str | None:
    """The full URL to cast at the display: `{base}/panel?k={token}`. None if the
    token hasn't been seeded yet."""
    token = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'panel_token'")
    if not token:
        return None
    return f"{base_url.rstrip('/')}/panel?k={token}"


async def verify_panel_key(pool: asyncpg.Pool, key: str) -> AuthUser | None:
    """Constant-time-check the presented panel key against the stored token;
    return the wallpanel user on match, else None."""
    token = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'panel_token'")
    if not token or not secrets.compare_digest(key, str(token)):
        return None
    return await fetch_user_by_username(pool, WALLPANEL_USERNAME)


def refresh_session_cookie(
    request: Request, response: Response, user: AuthUser, secret: str
) -> None:
    """Sliding session: called from /auth/me (hit on every app open), re-issues
    the cookie once it is past half its TTL. An ACTIVE device — family phone,
    wall panel, the Android app's WebView — stays signed in indefinitely; only a
    device idle for the full TTL falls back to the login screen."""
    raw = request.cookies.get(SESSION_COOKIE.name)
    if not raw:
        return  # authenticated some other way — nothing to slide
    try:
        payload = jwt.decode(raw, secret, algorithms=[ALGO])
    except jwt.InvalidTokenError:
        return
    age = datetime.now(UTC).timestamp() - float(payload.get("iat", 0))
    if age < TOKEN_TTL.total_seconds() / 2:
        return
    token = encode_session_token(user.id, secret, token_version=user.token_version, ttl=TOKEN_TTL)
    SESSION_COOKIE.set(response, token, request)
