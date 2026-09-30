"""OwnTracks HTTP-mode receiver — native background GPS without exposing MQTT.

The OwnTracks app (HTTP mode) POSTs location frames to
`https://<dida-host>/api/owntracks` with HTTP Basic auth = the person's DIDA
username and a per-user **location token** (NOT the login password). That rides
the existing HTTPS ingress (Cloudflare tunnel → UI proxy → API): no new open
ports, no TLS certificates to run, reachable from any network. The MQTT presence
adapter (`adapters/presence`) remains for broker-based setups; both converge on
the same `presence:<user>` entities via the shared publisher
(`dida_api.presence`).

Auth: Basic `username : token`, the token compared against `users.owntracks_token`
(provisioned in Settings → Users, migration 0015). It is endpoint-scoped — it
can NOT log into the UI — so a leaked phone config or a snapshot of the setup QR
never exposes the account, and this endpoint is not a login-password oracle. A
successful verify is cached (hash of the credentials, 10 min TTL) so the app's
frequent pings skip the DB round-trip; failed/uncached attempts are rate-limited
per IP. Rotating the token (admin) revokes a lost phone; the old credential can
survive here for up to the cache TTL.

This module also builds the provisioning artifacts (`build_config` /
`config_inline_url` / `qr_svg` / `provision_payload`) that Settings → Users hands
to the phone; the admin routes live in `dida_api.users`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time

import asyncpg
import segno
from dida_core import decrypt_secret
from fastapi import APIRouter, HTTPException, Request
from home_core.rate_limit import TokenBucketLimiter, client_ip

from dida_api.common import get_setting
from dida_api.presence import publish_report, publish_transition
from dida_api.rate_limit import TRUSTED_PROXIES

log = logging.getLogger("dida.api.owntracks")

router = APIRouter(tags=["owntracks"])

_CACHE_TTL_S = 600.0
_CACHE_MAX = 256  # a handful of family phones; anything beyond this is abuse
_auth_cache: dict[str, tuple[str, float]] = {}  # sha256(basic token) -> (username, expires)

# username -> the zone-set signature we last pushed to that phone as waypoints.
# Cleared on restart (a re-push is idempotent — setWaypoints merges by tst), so
# the worst case is one redundant push per phone after a restart. Tiny (family
# scale), so no eviction.
_wp_pushed: dict[str, str] = {}

# Same budget as the login endpoint, but a separate bucket: a misconfigured
# phone hammering bad credentials here must not lock the family out of the UI.
_LIMITER = TokenBucketLimiter(capacity=5, refill_per_s=5 / 60.0)

_401 = HTTPException(
    status_code=401,
    detail="authentication required",
    headers={"WWW-Authenticate": 'Basic realm="dida-owntracks"'},
)


async def basic_user(request: Request) -> str:
    """Resolve the OwnTracks device's Basic credentials (`username : location
    token`) to a DIDA username. The token is compared against
    `users.owntracks_token`, never the login password."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("basic "):
        raise _401
    creds = header[6:].strip()
    key = hashlib.sha256(creds.encode()).hexdigest()
    now = time.monotonic()
    hit = _auth_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    try:
        username, _, token = base64.b64decode(creds).decode().partition(":")
    except ValueError:
        raise _401 from None
    if not username or not token:
        raise _401
    if not _LIMITER.take(client_ip(request, TRUSTED_PROXIES)):
        log.warning("owntracks: auth attempts exhausted for %s", client_ip(request, TRUSTED_PROXIES))
        raise HTTPException(429, "too many attempts", headers={"Retry-After": "15"})
    # Case-insensitive on purpose: phone keyboards auto-capitalize the first
    # letter ("Marko"), and every family member would trip over it. The canonical
    # (stored) username is what gets returned, so the presence entity is right.
    row = await request.app.state.pool.fetchrow(
        "SELECT username, owntracks_token FROM users WHERE lower(username) = lower($1)", username
    )
    stored = row["owntracks_token"] if row else None
    if not stored or not secrets.compare_digest(token, stored):
        # Username + a coarse reason, never the token — enough to tell a typo'd
        # user / an unprovisioned account / a stale token apart when a phone
        # won't authenticate, without leaking the secret to the log.
        reason = "unknown user" if row is None else ("not provisioned" if not stored else "wrong token")
        log.warning("owntracks: bad credentials for user %r (%s) from %s",
                    username, reason, client_ip(request, TRUSTED_PROXIES))
        raise _401
    if len(_auth_cache) >= _CACHE_MAX:
        for k in [k for k, v in _auth_cache.items() if v[1] <= now] or [next(iter(_auth_cache))]:
            _auth_cache.pop(k, None)
    _auth_cache[key] = (row["username"], now + _CACHE_TTL_S)
    return row["username"]


async def _decrypt_frame(request: Request, username: str, body: dict) -> dict | None:
    """Open an OwnTracks `_type: encrypted` frame (Preferences → Security on the
    phone): libsodium secretbox, key = the shared passphrase zero-padded to 32
    bytes, data = base64(nonce ‖ ciphertext). The passphrase lives in
    app_settings (`owntracks_secret`, set in Settings → Keys). Every failure is
    logged and swallowed (return None → acknowledged) — a 4xx would just make
    the phone retry a frame that can never succeed."""
    enc = await get_setting(request.app.state.pool, "owntracks_secret")
    if not enc:
        log.warning(
            "owntracks: %s sent an encrypted frame but no OwnTracks key is "
            "configured (Settings → Keys) — dropping", username,
        )
        return None
    secret = decrypt_secret(
        os.environ.get("DIDA_SECRET_KEY", ""), enc, adapter="owntracks", key="owntracks_secret"
    )
    if not secret:
        return None  # decrypt_secret already logged (rotated DIDA_SECRET_KEY?)
    from nacl.secret import SecretBox

    try:
        raw = base64.b64decode(body.get("data") or "")
        plain = SecretBox(secret.encode()[:32].ljust(32, b"\0")).decrypt(raw)
        out = json.loads(plain)
    except Exception as exc:  # wrong key, corrupt frame — the phone can't fix a 4xx
        log.warning("owntracks: %s encrypted frame failed to decrypt "
                    "(key mismatch with the phone?): %s", username, exc, exc_info=True)
        return None
    return out if isinstance(out, dict) else None


@router.post("/owntracks")
async def owntracks_report(request: Request) -> list:
    """Accept an OwnTracks publish. Location frames are resolved + published to
    the bus; everything else the app sends (waypoints, status, transitions) is
    acknowledged and ignored. The response is the protocol's expected JSON
    array (empty — no server→device commands, no friends cards)."""
    username = await basic_user(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON body") from None
    if isinstance(body, dict) and body.get("_type") == "encrypted":
        body = await _decrypt_frame(request, username, body)
        if body is None:
            return []
    if isinstance(body, dict) and body.get("_type") == "transition":
        # Phone-side geofence edge: the app crossed a DIDA zone we pushed as a
        # waypoint. In significant-change mode this is the RELIABLE presence edge
        # — it wakes the app on enter/leave even while it's otherwise quiet, so a
        # stationary phone at home stays present without the network ARP signal
        # (which reads offline the moment the phone dozes).
        # The home waypoint's name carries the iOS mode-switch suffix ("Home|1|2");
        # both platforms echo the raw region name back as `desc` — strip it so the
        # published zone is the clean DB zone name.
        desc = re.sub(r"\|\d+\|\d+$", "", str(body.get("desc") or ""))
        result = await publish_transition(
            request.app.state, username, body.get("event", ""), desc
        )
        log.info("owntracks: %s transition %s %r -> %s (%s) [%s]", username,
                 body.get("event"), body.get("desc"), result.get("zone"),
                 "accepted" if result.get("accepted") else result.get("reason"),
                 (request.headers.get("user-agent") or "?").split(" ")[0])
        return []
    if not isinstance(body, dict) or body.get("_type") != "location":
        # Waypoints/status/etc are a normal part of the protocol — acknowledged,
        # not published. Logged so a phone that only ever sends these (a
        # misconfigured reporting mode) is visible, not a silent mystery.
        kind = body.get("_type") if isinstance(body, dict) else type(body).__name__
        log.info("owntracks: %s sent non-location frame (_type=%s) — ignored", username, kind)
        return []
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "lat/lon required") from None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(400, "lat/lon out of range")
    acc = body.get("acc")
    batt = body.get("batt")
    result = await publish_report(
        request.app.state,
        username,
        lat,
        lon,
        accuracy=float(acc) if isinstance(acc, int | float) else None,
        battery=float(batt) if isinstance(batt, int | float) and 0 <= batt <= 100 else None,
    )
    # Which reporter sent this — "DIDA-App/vX" (companion app) vs "Owntracks/…".
    # During the OwnTracks→companion migration this is the ONLY place the two
    # sources are distinguishable (same credentials, same frames).
    client = (request.headers.get("user-agent") or "?").split(" ")[0]
    if result.get("accepted"):
        log.info("owntracks: %s -> %s [%s]", username, result.get("zone"), client)
    else:
        # Fail loud: a phone whose every fix is too coarse must be visible
        # (indoors on network location, GPS off, …), not silently "away".
        log.info("owntracks: %s report skipped (%s, acc=%s m) [%s]",
                 username, result.get("reason"), acc, client)
    # Piggyback the zone→waypoint sync on this reply: if the zone set changed
    # since we last pushed to this phone, the array carries a setWaypoints command
    # the app applies with no user action (no re-scan). Otherwise it's empty.
    return await _waypoint_sync(request.app.state.pool, username)


# --- Provisioning (Settings → Users generates these; admin routes in users.py) ---

# A loopback / localhost public URL can't be reached from a phone off the home
# network — the config would silently only work on LAN. The UI surfaces this so
# the admin sets the public address (Settings → Network) to the tunnel origin.
_LOOPBACK_HINTS = ("localhost", "127.0.0.1", "[::1]", "//::1")


async def public_base_url(pool) -> str:
    """The externally reachable origin (Cloudflare tunnel) the phone POSTs to.
    The same stored setting the Spotify/SmartThings OAuth flows use."""
    from dida_core import host_setting

    return (await host_setting(pool, "public_url")).rstrip("/")


def is_reachable(url: str) -> bool:
    return url.startswith(("http://", "https://")) and not any(h in url for h in _LOOPBACK_HINTS)


def build_config(
    *, public_url: str, username: str, token: str, secret: str | None,
    waypoints: list[dict] | None = None,
) -> dict:
    """An OwnTracks `_type: configuration` message pre-filled for DIDA. `mode: 3`
    is HTTP mode; `monitoring: 1` (significant-change) is the reliable, battery-
    friendly default for presence. `waypoints` are DIDA's zones pushed as phone-
    side geofences: significant-change monitoring fires an enter/leave transition
    the instant the phone crosses one, which is what keeps a stationary phone
    present without leaning on the network ARP signal (dead the moment it dozes).

    Freshness: significant mode alone is COARSE by design — iOS reports only on
    ≥500 m / ~5 min (Apple's service; locatorInterval/Displacement don't apply to
    it), Android on a ~15-min balanced-power cadence. Two compensations: the home
    zone waypoint carries the iOS mode-switch suffix ("Home|1|2" — see
    `zone_waypoints`), flipping iPhones into move mode while away from home
    (fresh fixes on the locatorInterval/Displacement cadence below) and back to
    significant at home; and `ping` guarantees a stationary baseline check-in.
    Unknown keys are ignored by the app, so this is a safe superset.
    `encryptionKey` is included only when the receiver has an OwnTracks key set
    (Settings → Keys), turning on payload encryption for free."""
    tid = "".join(ch for ch in username if ch.isalnum())[:2].upper() or "ID"
    cfg = {
        "_type": "configuration",
        "mode": 3,                          # HTTP mode (0 would be private MQTT)
        "url": f"{public_url}/api/owntracks",
        "username": username,
        "password": token,
        "deviceId": username.lower(),
        "tid": tid,                         # 2-char tracker id shown on maps
        "monitoring": 1,                    # significant-change: reliable + easy on battery
        "locatorInterval": 300,             # s between fixes in move mode (iOS away from home)
        "locatorDisplacement": 100,         # m of movement before a fresh fix (move mode)
        "ping": 15,                         # min heartbeat so a parked phone still checks in
        "pubExtendedData": True,            # include battery %, etc.
        "cmd": True,                        # accept server→device remote commands —
                                            # the channel that pushes waypoint updates
                                            # in the HTTP reply (no re-scan on zone edits)
        "allowRemoteLocation": False,       # …but never on-demand location pulls
        "ignoreInaccurateLocations": 200,   # m — drop very coarse network fixes
    }
    if waypoints:
        cfg["waypoints"] = waypoints
    if secret:
        cfg["encryptionKey"] = secret
    return cfg


def config_inline_url(config: dict) -> str:
    """`owntracks:///config?inline=…` — what the setup QR encodes and the "copy
    link" action yields. The payload is url-safe base64 of the compact JSON;
    OwnTracks imports it via Configuration Management → scan / open.

    The padding (`=`) is KEPT: the OwnTracks Android importer decodes with
    padding required and rejects an unpadded string ("the padding option is set
    to PRESENT, but the input is not properly padded"). The `=` is a trailing
    query-value char, so URL parsing keeps it intact."""
    raw = json.dumps(config, separators=(",", ":")).encode()
    return "owntracks:///config?inline=" + base64.urlsafe_b64encode(raw).decode()


def qr_svg(data: str) -> str:
    """Inline SVG (no XML prolog, so it embeds directly in the page) of a QR for
    `data`, rendered black-on-white for scan reliability regardless of UI theme.
    `omitsize` drops the fixed width/height and emits a `viewBox` instead — without
    it the SVG has no coordinate mapping, so CSS-driven sizing crops the QR to the
    top-left corner (cutting the finder patterns) rather than scaling it, which
    makes it undecodable by a phone camera."""
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=5, border=2, dark="#000000", light="#ffffff", xmldecl=False, omitsize=True
    )
    return buf.getvalue().decode()


async def ensure_location_token(pool: asyncpg.Pool, user_id: int) -> tuple[str, str]:
    """The user's endpoint-scoped location token (creating it on first use,
    idempotent thereafter). Shared by the self-service OwnTracks config
    (/me/owntracks) and the native app provisioning (/me/mobile-config) — one
    token per user, whichever client reports."""
    row = await pool.fetchrow(
        "SELECT username, owntracks_token FROM users WHERE id = $1", user_id
    )
    token = row["owntracks_token"]
    if not token:
        token = secrets.token_urlsafe(24)
        await pool.execute(
            "UPDATE users SET owntracks_token = $2 WHERE id = $1", user_id, token
        )
    return row["username"], token


async def zone_waypoints(pool: asyncpg.Pool) -> list[dict]:
    """DIDA's zones as OwnTracks waypoints (phone-side geofences). `tst` is the
    waypoint's stable id — the zone's creation time, so re-provisioning the same
    zone updates rather than duplicates it. Ordered home-first: iOS/Android only
    monitor the ~20 regions nearest the phone, so the home zone must never be the
    one dropped.

    The home zone's description carries OwnTracks iOS's region-based
    monitoring-mode suffix: "Home|1|2" = on ENTER switch to significant (1,
    battery-friendly while at home), on LEAVE switch to move (2 — fresh fixes on
    the road; the staleness complaint was significant mode's ≥500 m / ~5 min iOS
    floor). Android ignores the semantics and just echoes the raw name in
    transitions, which the receiver strips (see `owntracks_report`)."""
    rows = await pool.fetch(
        "SELECT name, latitude, longitude, radius_m, created_at, is_home FROM zones "
        "ORDER BY is_home DESC, name"
    )
    return [
        {
            "_type": "waypoint",
            "desc": r["name"] + ("|1|2" if r["is_home"] else ""),
            "lat": round(r["latitude"], 6),
            "lon": round(r["longitude"], 6),
            "rad": int(r["radius_m"]),
            "tst": int(r["created_at"].timestamp()),
        }
        for r in rows
    ]


async def _zones_signature(pool: asyncpg.Pool) -> str:
    """A cheap fingerprint of the zone set (name/lat/lon/radius/home). Changes iff
    a zone is added, moved, resized, or removed — the trigger to re-push waypoints
    to the phones. Explicit ::text casts so booleans/numerics concatenate."""
    row = await pool.fetchrow(
        "SELECT md5(coalesce(string_agg("
        "  name || ':' || latitude::text || ':' || longitude::text || ':' "
        "  || radius_m::text || ':' || is_home::text, '|' ORDER BY name), '')) AS sig "
        "FROM zones"
    )
    return row["sig"] or ""


async def _waypoint_sync(pool: asyncpg.Pool, username: str) -> list[dict]:
    """Push DIDA's current zones to the phone as OwnTracks waypoints via the HTTP
    reply. The app MERGES them (by `tst` = the zone's created_at) with no user
    action, so a zone edit reaches every phone on its next ping — the QR scan is a
    one-time bootstrap, not a recurring chore. Gated on the zone-set signature so a
    steady state returns an empty reply; only a real change re-pushes.

    The phone honours this only with Remote Commands enabled (the config sets
    `cmd: true`); a phone that still has it off harmlessly ignores the command.
    Unlike the QR (hard byte cap → trimmed set), the HTTP reply has no size limit,
    so ALL zones go — the OS still monitors only the ~20 nearest."""
    sig = await _zones_signature(pool)
    if _wp_pushed.get(username) == sig:
        return []
    waypoints = await zone_waypoints(pool)
    _wp_pushed[username] = sig
    log.info("owntracks: %s — pushed %d zone waypoints (zone set changed)", username, len(waypoints))
    return [{
        "_type": "cmd",
        "action": "setWaypoints",
        "waypoints": {"_type": "waypoints", "waypoints": waypoints},
    }]


async def provision_payload(pool: asyncpg.Pool, username: str, token: str) -> dict:
    """Everything Settings → Users needs to render the setup panel for one user:
    the OwnTracks config, the inline import URL, a QR of it, and a fail-loud
    `reachable` flag when DIDA_PUBLIC_URL is still loopback."""
    public = await public_base_url(pool)
    enc = await get_setting(pool, "owntracks_secret")
    secret = (
        decrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), enc, adapter="owntracks", key="owntracks_secret")
        if enc else None
    )
    # The inline config is delivered as a QR, which has a hard byte capacity. A
    # big zone set overflows it, so shed the LOWEST-priority waypoints (home +
    # dwell zones sort first; transient POIs — shops/gyms — sort last and drop
    # first) until it encodes. Onboarding must never 500 on a large zone set.
    waypoints = await zone_waypoints(pool)
    total = len(waypoints)
    while True:
        config = build_config(
            public_url=public, username=username, token=token, secret=secret,
            waypoints=waypoints or None,
        )
        inline = config_inline_url(config)
        try:
            qr = qr_svg(inline)
            break
        except segno.DataOverflowError:
            if not waypoints:
                raise  # base config alone can't fit a QR — a real, loud problem
            waypoints = waypoints[:-1]
    if len(waypoints) < total:
        log.info("owntracks: %s — %d/%d zones pushed as waypoints (trimmed to fit the QR)",
                 username, len(waypoints), total)
    return {
        "username": username,
        "token": token,
        "url": f"{public}/api/owntracks" if public else "",
        "public_url": public,
        "reachable": is_reachable(public),
        "encrypted": bool(secret),
        "config": config,
        "inline_url": inline,
        "qr_svg": qr,
    }
