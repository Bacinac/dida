"""The household address book: the Google handshake, the roster, and the lookups
other modules make.

DIDA is the house's ONE reader of the contacts book. That is the whole point of
this module living here: two readers mean two consent screens, two tokens to
expire, and two schedules that drift apart. OPUS Library needs a birth year to
tell two lookalike children apart when it groups faces, so it asks — over HTTP,
with a token — instead of keeping a second copy.

Three things this shape is built around, all checked against Google's own docs:

  * The browserless device flow does NOT allow a contacts scope (profile, Drive
    and YouTube only), so consent happens in a browser against a public callback.
  * An app in "Testing" gets a refresh token that dies after seven days when the
    scope is sensitive. The app must be PUBLISHED.
  * CardDAV refuses an app password outright (401) — Google requires OAuth.

The OAuth client belongs in a Google Cloud project of its OWN, not the one holding
the FCM service account: an OAuth client with a sensitive scope makes the consent
screen the whole project's, and whatever Google does to an unverified project would
then reach the smart home's push notifications.

The reading itself is the `contacts` adapter's job; this module mints the refresh
token it reads, and serves the table it fills.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import secrets as pysecrets
import time
import urllib.parse
from urllib.parse import urlsplit

import httpx
import jwt
from dida_core import host_setting
from dida_core.adapter_config import decrypt_secret, encrypt_secret, house_timezone
from dida_core.people import age_turning, birthday_offset, plain, read_export, store_book
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from dida_api.auth import AuthUser, current_user, require_admin

log = logging.getLogger("dida.api.contacts")

router = APIRouter(prefix="/contacts", tags=["contacts"])

_CONSENT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
# Every scope in ONE consent. What the Data Access page in the Google console
# declares is only what this client is ALLOWED to ask for; the grant carries what
# the request actually asks, so a permission added later costs another walk
# through the consent screen.
#
# `contacts` is read-WRITE, and that is deliberate. The address book is kept by
# hand and shows it — names stripped of their diacritics, the same person entered
# twice, entries with nothing in them. Tidying it at the source is the only fix
# that lasts: anything corrected locally would be overwritten by the next
# morning's read, because the book is the truth and this is only its reader.
#
# What the write scope does NOT do is let the daily read write. The sync only ever
# reads. Changes leave this house from one place, the maintenance page, one
# confirmed line at a time — DIDA proposes, a person decides, Google keeps.
_SCOPE = " ".join((
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/calendar.readonly",
))
# An export is a text file of a few hundred contacts; anything far past that is a
# mistake, not a book.
_MAX_EXPORT = 8 * 1024 * 1024


def _secret() -> str:
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(500, "DIDA_SECRET_KEY is not set — the Google token cannot be saved.")
    return secret


async def _cfg(request: Request) -> dict:
    rows = await request.app.state.pool.fetch(
        "SELECT key, value FROM adapter_config WHERE adapter = 'contacts'"
    )
    d = {r["key"]: r["value"] for r in rows}
    secret = _secret()
    client_secret = (
        decrypt_secret(secret, d["client_secret"], adapter="contacts", key="client_secret")
        if d.get("client_secret") else ""
    )
    redirect_uri = (d.get("redirect_uri") or "").strip()
    if not redirect_uri:
        base = (await host_setting(request.app.state.pool, "public_url")).rstrip("/")
        redirect_uri = f"{base}/api/contacts/callback" if base else ""
    return {
        "client_id": (d.get("client_id") or "").strip(),
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "connected": "_oauth" in d,
    }


async def _app_origin(pool, redirect_uri: str) -> str:
    p = urlsplit(redirect_uri)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    base = await host_setting(pool, "app_url")
    return (base or await host_setting(pool, "public_url")).rstrip("/")


@router.get("/status")
async def status(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """What the Contacts card shows: whether it can connect, whether it has, how
    many people are in the book and what the last read did."""
    cfg = await _cfg(request)
    pool = request.app.state.pool
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'contacts_last_sync'")
    try:
        last = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        last = None
    return {
        "configured": bool(cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]),
        "connected": cfg["connected"],
        "redirect_uri": cfg["redirect_uri"],
        "people": await pool.fetchval("SELECT count(*) FROM people"),
        "announced": await pool.fetchval("SELECT count(*) FROM people WHERE announce"),
        "last_sync": last,
    }


@router.get("/login")
async def login(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    cfg = await _cfg(request)
    if not (cfg["client_id"] and cfg["client_secret"] and cfg["redirect_uri"]):
        raise HTTPException(503, "Unesi Client ID i Client Secret pa spremi.")
    state = jwt.encode(
        {"a": "contacts", "n": pysecrets.token_hex(8), "exp": int(time.time()) + 600},
        request.app.state.secret_key, algorithm="HS256",
    )
    params = {
        "client_id": cfg["client_id"], "response_type": "code",
        "redirect_uri": cfg["redirect_uri"], "scope": _SCOPE, "state": state,
        # Both, or Google hands back an access token and no way to ask again.
        "access_type": "offline", "prompt": "consent",
    }
    return {"url": f"{_CONSENT}?{urllib.parse.urlencode(params)}"}


@router.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    cfg = await _cfg(request)
    origin = await _app_origin(request.app.state.pool, cfg["redirect_uri"])

    def back(result: str, reason: str = "") -> RedirectResponse:
        q = f"contacts={result}" + (f"&reason={urllib.parse.quote(reason)}" if reason else "")
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
        r = await cx.post(_TOKEN, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": cfg["redirect_uri"],
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        })
    if r.status_code != 200:
        log.warning("contacts code exchange failed: %s %s", r.status_code, r.text[:200])
        return back("error", "token")
    tok = r.json()
    if not tok.get("refresh_token"):
        # Without it the grant is a one-hour access token and no way to ask again —
        # which is the same end state as never connecting, reached an hour later.
        log.warning("contacts token response has no refresh_token: %s", sorted(tok))
        return back("error", "offline")
    await request.app.state.pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ('contacts', '_oauth', $1) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value",
        encrypt_secret(_secret(), json.dumps({"refresh_token": tok["refresh_token"]})),
    )
    log.info("contacts: connected — refresh token stored")
    return back("connected")


@router.post("/disconnect")
async def disconnect(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Forget the token. The people stay: they were read, they are still the
    household, and a birth date does not stop being true."""
    await request.app.state.pool.execute(
        "DELETE FROM adapter_config WHERE adapter = 'contacts' AND key = '_oauth'"
    )
    return {"ok": True}


@router.post("/import")
async def import_export(
    request: Request, text: str = Body(..., embed=True), _admin: AuthUser = Depends(require_admin)
) -> dict:
    """A contacts export (vCard or CSV) — the way in that needs no consent screen
    and no token. Never prunes: an export is usually a subset, and pruning against
    one would empty the table."""
    if len(text) > _MAX_EXPORT:
        raise HTTPException(413, "Datoteka je prevelika.")
    book = read_export(text)
    if not book:
        raise HTTPException(400, "U datoteci nema nijednog kontakta s rođendanom.")
    return await store_book(request.app.state.pool, book, source="file", prune=False)


class AnnouncePatch(BaseModel):
    announce: bool


@router.get("/people")
async def list_people(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, born_on, contact_id, source, announce FROM people ORDER BY name"
    )
    return [dict(r) for r in rows]


@router.patch("/people/{person_id}")
async def set_announce(
    person_id: int, patch: AnnouncePatch, request: Request,
    _admin: AuthUser = Depends(require_admin),
) -> dict:
    """Whose birthday the house says out loud. The book holds everyone; the kitchen
    speaker is for the household, so this is opted into per person."""
    row = await request.app.state.pool.fetchrow(
        "UPDATE people SET announce = $2, updated_at = now() WHERE id = $1 RETURNING id",
        person_id, patch.announce,
    )
    if row is None:
        raise HTTPException(404, "person_not_found")
    return {"ok": True}


# ── what other modules ask ────────────────────────────────────────────────────
# Read-only, any signed-in principal. OPUS Library holds a DIDA account of its own
# (`can_control=false`), so revoking the link is bumping one user's token_version —
# the peer-adapter pattern, in the other direction.


@router.get("/person")
async def person(
    name: str, request: Request, _user: AuthUser = Depends(current_user)
) -> dict:
    """A person's birth date, looked up by name.

    Matched on the accent-, case- and order-free form: a caller holding a photo
    knows "Bošković" or "Boskovic" or "Kovacic Ana Marija", and a match that
    insists on one spelling finds almost nobody. An AMBIGUOUS name is a 404, not a
    guess — handing back one of two same-named people would put a birth year on
    the wrong face, silently."""
    rows = await request.app.state.pool.fetch(
        "SELECT name, born_on, contact_id FROM people WHERE plain_name = $1", plain(name)
    )
    if len(rows) != 1:
        raise HTTPException(404, "ambiguous" if rows else "unknown")
    row = rows[0]
    return {"name": row["name"], "born_on": row["born_on"].isoformat(),
            "contact_id": row["contact_id"]}


@router.get("/birthdays")
async def birthdays(
    request: Request, within: int = 2, _user: AuthUser = Depends(current_user)
) -> list[dict]:
    """Who has a birthday in the next `within` days, and which age they turn.

    The whole book, not just the announced ones: this answers "whose birthday is
    it", and who the house says that out loud for is a separate question."""
    within = max(0, min(within, 366))
    # The house's date, not the container's: this service runs UTC, and between
    # local midnight and 02:00 a UTC "today" is yesterday — which would answer
    # yesterday's birthdays on the one night of the year that matters.
    today = datetime.datetime.now(await house_timezone(request.app.state.pool)).date()
    rows = await request.app.state.pool.fetch(
        "SELECT name, born_on, announce FROM people ORDER BY name"
    )
    out = []
    for r in rows:
        offset = birthday_offset(r["born_on"], today, within)
        if offset is None:
            continue
        out.append({
            "name": r["name"],
            "born_on": r["born_on"].isoformat(),
            "on": (today + datetime.timedelta(days=offset)).isoformat(),
            "in_days": offset,
            "turns": age_turning(r["born_on"], today, offset),
            "announce": r["announce"],
        })
    return sorted(out, key=lambda p: (p["in_days"], p["name"]))
