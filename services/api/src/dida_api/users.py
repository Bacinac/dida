"""User management routes (admin-only CRUD) — extracted from app.py so the app
module stays a shell of cross-cutting concerns rather than a 1800-line monolith.

Follows the same shape as the other routers (services/pipeline/radio): an
APIRouter that reaches Postgres via request.app.state.pool.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from home_core.auth import hash_password
from pydantic import BaseModel, Field

from dida_api import owntracks
from dida_api.auth import AuthUser, require_admin

router = APIRouter(tags=["users"])

# Canonical set of pages a non-admin user's visibility can be scoped to, in
# display order. `allowed_pages = NULL` on the user row means "all of these"
# (full consumer access); a subset restricts the nav + route guard to those.
# Admins ignore the column. This is the SERVER-SIDE authority (it validates +
# orders allowed_pages); the frontend mirrors it in CONSUMER_PAGES
# (ui/src/lib/pages.ts, its single source for nav/tabs/guard/users-admin). Keep
# the two lists (keys + order) identical. See migration 0013.
PAGE_ORDER: tuple[str, ...] = ("entry", "floorplan", "devices", "cameras", "media", "heating",
                               "assistant", "history", "adapters")
SELECTABLE_PAGES = frozenset(PAGE_ORDER)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field(default="user")


CONTROL_SCOPES = frozenset(("entity", "area", "capability"))


class ControlRule(BaseModel):
    scope: str
    ref: str = Field(..., min_length=1, max_length=128)


class UserPatch(BaseModel):
    role: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    # For allowed_pages / control_rules / view_hides, explicit None (allowed_pages)
    # or omission is meaningful — presence is checked via model_fields_set.
    allowed_pages: list[str] | None = None
    can_control: bool | None = None
    control_rules: list[ControlRule] | None = None   # kind='control' (flip rules)
    view_hides: list[ControlRule] | None = None      # kind='view' (hide rules)


def _norm_role(role: str) -> str:
    if role not in ("admin", "user"):
        raise HTTPException(400, "unknown role")
    return role


def _norm_rules(rules: list[ControlRule] | None) -> list[tuple[str, str]]:
    """Validate + dedupe access rules into (scope, ref) tuples for insertion."""
    seen: set[tuple[str, str]] = set()
    for r in rules or ():
        if r.scope not in CONTROL_SCOPES:
            raise HTTPException(400, f"unknown rule scope: {r.scope}")
        seen.add((r.scope, r.ref))
    return sorted(seen)


async def _replace_rules(conn, user_id: int, kind: str, rules: list[ControlRule] | None) -> None:
    """Full-replace one kind ('control'|'view') of a user's access rules."""
    tuples = _norm_rules(rules)
    if await conn.fetchval("SELECT 1 FROM users WHERE id = $1", user_id) is None:
        raise HTTPException(404, "user does not exist")
    await conn.execute("DELETE FROM user_access_rules WHERE user_id = $1 AND kind = $2", user_id, kind)
    if tuples:
        await conn.executemany(
            "INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, $2, $3, $4)",
            [(user_id, kind, scope, ref) for scope, ref in tuples],
        )


def _norm_pages(pages: list[str] | None) -> list[str] | None:
    """Validate a requested page set. None → full access (stored NULL). A list
    must be a non-empty subset of the canonical pages; returned in canonical
    order so storage is stable and comparable."""
    if pages is None:
        return None
    unknown = set(pages) - SELECTABLE_PAGES
    if unknown:
        raise HTTPException(400, f"unknown pages: {', '.join(sorted(unknown))}")
    if not pages:
        raise HTTPException(400, "select at least one page (or all for full access)")
    chosen = set(pages)
    return [p for p in PAGE_ORDER if p in chosen]


async def _is_last_admin(pool, user_id: int) -> bool:
    """True if user_id is an admin and the only one — guards against lockout."""
    row = await pool.fetchrow("SELECT role FROM users WHERE id = $1", user_id)
    if row is None or row["role"] != "admin":
        return False
    return await pool.fetchval("SELECT count(*) FROM users WHERE role = 'admin'") <= 1


@router.get("/users")
async def list_users(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT u.id, u.username, u.role, u.last_login_at, u.allowed_pages, u.can_control, "
        "  COALESCE((SELECT jsonb_agg(jsonb_build_object('scope', r.scope, 'ref', r.ref) ORDER BY r.scope, r.ref) "
        "            FROM user_access_rules r WHERE r.user_id = u.id AND r.kind = 'control'), '[]'::jsonb) AS control_rules, "
        "  COALESCE((SELECT jsonb_agg(jsonb_build_object('scope', r.scope, 'ref', r.ref) ORDER BY r.scope, r.ref) "
        "            FROM user_access_rules r WHERE r.user_id = u.id AND r.kind = 'view'), '[]'::jsonb) AS view_hides "
        "FROM users u ORDER BY u.username"
    )
    return [
        {"id": str(r["id"]), "username": r["username"], "role": r["role"],
         "last_login_at": r["last_login_at"], "allowed_pages": r["allowed_pages"],
         "can_control": r["can_control"], "control_rules": r["control_rules"],
         "view_hides": r["view_hides"]}
        for r in rows
    ]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    role = _norm_role(body.role)
    pw_hash = await hash_password(body.password)
    try:
        row = await request.app.state.pool.fetchrow(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3) "
            "RETURNING id, username, role",
            body.username.strip(), pw_hash, role,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "korisničko ime već postoji") from exc
    return {"id": str(row["id"]), "username": row["username"], "role": row["role"]}


@router.patch("/users/{user_id}", status_code=204)
async def update_user(user_id: int, body: UserPatch, request: Request,
                      _admin: AuthUser = Depends(require_admin)) -> None:
    """Set role and/or reset password. A password reset bumps token_version so
    that user's other sessions are revoked."""
    pool = request.app.state.pool
    if body.role is not None:
        role = _norm_role(body.role)
        if role == "user" and await _is_last_admin(pool, user_id):
            raise HTTPException(400, "at least one administrator must remain")
        res = await pool.execute("UPDATE users SET role = $2 WHERE id = $1", user_id, role)
        if res.endswith("0"):
            raise HTTPException(404, "user does not exist")
    if body.password is not None:
        # Reset severs EVERYTHING: bump token_version (kills live JWT sessions) and
        # null the QR login_token (+ its expiry) so a leaked setup QR can't outlive
        # the reset — the "changing a password severs every session" guarantee now
        # holds for the bearer-token path too, not just cookies.
        res = await pool.execute(
            "UPDATE users SET password_hash = $2, token_version = token_version + 1, "
            "login_token = NULL, login_token_expires_at = NULL WHERE id = $1",
            user_id, await hash_password(body.password),
        )
        if res.endswith("0"):  # missing user → 404, matching the sibling branches
            raise HTTPException(404, "user does not exist")
    if "allowed_pages" in body.model_fields_set:
        res = await pool.execute(
            "UPDATE users SET allowed_pages = $2 WHERE id = $1",
            user_id, _norm_pages(body.allowed_pages),
        )
        if res.endswith("0"):
            raise HTTPException(404, "user does not exist")
    if body.can_control is not None:
        res = await pool.execute(
            "UPDATE users SET can_control = $2 WHERE id = $1", user_id, body.can_control
        )
        if res.endswith("0"):
            raise HTTPException(404, "user does not exist")
    # Full-replace the user's scoped rules, each kind independently, in one tx.
    if "control_rules" in body.model_fields_set or "view_hides" in body.model_fields_set:
        async with pool.acquire() as conn, conn.transaction():
            if "control_rules" in body.model_fields_set:
                await _replace_rules(conn, user_id, "control", body.control_rules)
            if "view_hides" in body.model_fields_set:
                await _replace_rules(conn, user_id, "view", body.view_hides)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int, request: Request, admin: AuthUser = Depends(require_admin)) -> None:
    if admin.id == user_id:
        raise HTTPException(400, "ne možeš obrisati vlastiti račun")
    pool = request.app.state.pool
    if await _is_last_admin(pool, user_id):
        raise HTTPException(400, "at least one administrator must remain")
    res = await pool.execute("DELETE FROM users WHERE id = $1", user_id)
    if res.endswith("0"):
        raise HTTPException(404, "user does not exist")


# --- Phone setup provisioning ---
# ONE per-user setup QR (login token) auto-logs the family member in and lands
# them on /onboard, which offers one-tap location sharing (self-service
# GET /me/owntracks in app.py). GET creates-on-open, POST rotates (old QR dies),
# DELETE revokes. `_access_payload` builds the QR; the OwnTracks receiver +
# artifact builders live in dida_api.owntracks.

async def _user_row(pool: asyncpg.Pool, user_id: int) -> asyncpg.Record:
    row = await pool.fetchrow(
        "SELECT username, owntracks_token, login_token FROM users WHERE id = $1", user_id
    )
    if row is None:
        raise HTTPException(404, "user does not exist")
    return row


@router.delete("/users/{user_id}/owntracks", status_code=204)
async def owntracks_revoke(user_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> None:
    """Admin revokes a person's location sharing (nulls their OwnTracks token) —
    e.g. someone leaves the household. The onboarding "remove access" action
    calls this alongside access revocation."""
    pool = request.app.state.pool
    res = await pool.execute("UPDATE users SET owntracks_token = NULL WHERE id = $1", user_id)
    if res.endswith("0"):
        raise HTTPException(404, "user does not exist")


async def _access_payload(pool, username: str, token: str) -> dict:
    # TWO QR codes, one job each:
    #  * setup (Android, the app) — auth/link pinned to /onboard: signs the
    #    browser in and shows ONE guided button that installs the app or opens
    #    it signed-in (intent:// with APK fallback — deterministic, no App-Link
    #    verification races). Scanned again once the app is installed+verified,
    #    the App Link opens the app straight into the permission walkthrough.
    #  * login (web / iPhone) — plain auth/link: flies straight into the web
    #    dashboard, signed in. For guests and iOS (no native app there).
    # The token is reusable until rotated, so re-scanning either QR never
    # dead-ends on a login screen.
    public = await owntracks.public_base_url(pool)
    login_url = f"{public}/api/auth/link?k={token}" if public else ""
    setup_url = f"{public}/api/auth/link?k={token}&next=/onboard" if public else ""
    return {
        "username": username,
        "url": login_url,  # kept as `url` for the copy-link button
        "setup_url": setup_url,
        "public_url": public,
        "reachable": owntracks.is_reachable(public),
        "qr_svg": owntracks.qr_svg(login_url) if login_url else "",
        "qr_svg_setup": owntracks.qr_svg(setup_url) if setup_url else "",
    }


# A setup QR is a bootstrap credential, not a standing login — it lives just long
# enough for the family member to scan it. Redemption also deletes it (one-time,
# see /auth/link), so this TTL is the backstop for a QR that is generated but never
# scanned. Generous enough to hand a phone over the weekend, short enough that a
# leaked image goes stale on its own.
_LOGIN_TOKEN_TTL = timedelta(days=14)


async def mint_login_token(
    pool: asyncpg.Pool, user_id: int, ttl: timedelta = _LOGIN_TOKEN_TTL
) -> str:
    """One-time auto-login token. Default TTL fits the admin setup-QR flow (hand
    a phone over the weekend); the mobile browser→app handoff (dida_api.mobile)
    passes a minutes-scale TTL instead."""
    token = secrets.token_urlsafe(24)
    await pool.execute(
        "UPDATE users SET login_token = $2, login_token_expires_at = $3 WHERE id = $1",
        user_id, token, datetime.now(UTC) + ttl,
    )
    return token


async def ensure_login_token(pool: asyncpg.Pool, user_id: int) -> str:
    """The user's live login token, minting one only when none is valid. EVERY
    consumer must go through this — there is ONE token per user (the QR), and
    minting a fresh one anywhere else silently kills the QR the admin is showing
    (exactly the bug the app-link handoff had)."""
    row = await pool.fetchrow(
        "SELECT login_token, login_token_expires_at FROM users WHERE id = $1", user_id
    )
    if row is None:
        raise HTTPException(404, "user does not exist")
    token = row["login_token"]
    expires = row["login_token_expires_at"]
    if not token or (expires is not None and expires <= datetime.now(UTC)):
        token = await mint_login_token(pool, user_id)
    return token


@router.get("/users/{user_id}/access")
async def access_setup(user_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Show the current setup QR, minting one if none is live. A token past its TTL
    counts as absent, so opening this panel always yields a scannable QR."""
    pool = request.app.state.pool
    row = await pool.fetchrow("SELECT username FROM users WHERE id = $1", user_id)
    if row is None:
        raise HTTPException(404, "user does not exist")
    token = await ensure_login_token(pool, user_id)
    return await _access_payload(pool, row["username"], token)


@router.post("/users/{user_id}/access")
async def access_rotate(user_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    row = await _user_row(pool, user_id)
    token = await mint_login_token(pool, user_id)
    return await _access_payload(pool, row["username"], token)


@router.delete("/users/{user_id}/access", status_code=204)
async def access_revoke(user_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> None:
    pool = request.app.state.pool
    res = await pool.execute("UPDATE users SET login_token = NULL WHERE id = $1", user_id)
    if res.endswith("0"):
        raise HTTPException(404, "user does not exist")
