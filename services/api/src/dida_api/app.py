from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import clickhouse_connect
from dida_core import (
    CORE,
    Bus,
    CapabilityError,
    apply_migrations,
    attach_log_bus,
    jsonb_init,
    pg_pool,
    set_app_setting,
)
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from home_core.auth import (
    bootstrap_admin_if_empty,
    encode_session_token,
    hash_password,
    load_or_create_secret,
    verify_password,
)
from pydantic import BaseModel, Field

from dida_api import (  # routers
    adapters,
    automations,
    camera,
    computed_helpers,
    devices,
    entities,
    entry,
    floors,
    heating,
    history_routes,
    mobile,
    orphans,
    owntracks,
    panel,
    photos,
    presence,
    push,
    radio_tuner,
    scenes,
    schedules,
    settings,
    translations,
    users,
    vacuum,
    virtual,
    zones,
)
from dida_api.alerts import alert_loop
from dida_api.alerts import router as alerts_router
from dida_api.androidtv import router as androidtv_router
from dida_api.assistant import AssistantCtx, assistant_events
from dida_api.auth import (
    SESSION_COOKIE,
    TOKEN_TTL,
    AuthUser,
    can_see_page,
    current_user,
    ensure_wallpanel,
    refresh_session_cookie,
    request_token,
    require_admin,
    revoke_session,
    session_user,
    verify_panel_key,
)
from dida_api.backup import backup_scheduler
from dida_api.backup import router as backup_router
from dida_api.broker import serve_broker
from dida_api.commands import dispatch_command
from dida_api.common import get_setting, resolve_assistant_client, stored_api_key
from dida_api.contacts import router as contacts_router
from dida_api.hub import Hub
from dida_api.opus_media import router as opus_router
from dida_api.pipeline import router as pipeline_router
from dida_api.rate_limit import LOGIN, enforce_assistant_limit
from dida_api.retention import router as retention_router
from dida_api.seed import seed_from_env
from dida_api.smartthings import router as smartthings_router
from dida_api.system import router as system_router
from dida_api.upkeep import router as upkeep_router
from dida_api.visibility import hidden_for, page_allowed_ids
from dida_api.volume_presets import router as volume_presets_router

log = logging.getLogger("dida.api")

# Ordered low → high; the /logs level filter takes everything from the named
# level upward. Matches Python's own levels, which is what the handler stores.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# A capability value on the wire is always one of these scalars (mirrors
# dida_core.capabilities.Value). Kept explicit so FastAPI/pydantic can validate
# the command args without importing the engine.
Scalar = bool | int | float | str

_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await pg_pool(max_size=8, init=jsonb_init)
    state_dir = Path(os.environ.get("DIDA_STATE_PATH", "/state"))
    app.state.secret_key = load_or_create_secret(state_dir, "DIDA_SECRET_KEY")
    # Schema first (db/migrations single source), THEN anything that reads/writes
    # tables — bootstrap_admin needs `users`.
    await apply_migrations(app.state.pool)
    await bootstrap_admin_if_empty(app.state.pool, state_dir, env_prefix="DIDA")
    await ensure_wallpanel(app.state.pool)  # living-room display auto-login user + token
    await push.ensure_vapid(app.state.pool)  # web-push keypair on first boot
    # ClickHouse client for history queries. Best-effort: a CH blip must not
    # take down the API, so /history handles a missing client with a 503.
    try:
        app.state.ch = await clickhouse_connect.get_async_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "dida"),
            password=os.environ["CLICKHOUSE_PASSWORD"],  # no default — fail loud
            database=os.environ.get("CLICKHOUSE_DB", "dida"),
        )
    except Exception:
        log.exception("clickhouse client init failed; /history will 503 until it recovers")
        app.state.ch = None
    # Assistant client is built lazily from the stored key (app_settings) or env,
    # so the key can be set / rotated from the UI without an API restart.
    app.state.anthropic_client = None
    app.state.anthropic_key = None
    app.state.hub = Hub()
    # The environment's one say: copy what it declared into app_settings, once.
    await seed_from_env(app.state.pool)
    await adapters.lift_baba_nats_credentials(app.state.pool)
    app.state.bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-api", user=CORE)
    await app.state.bus.connect()
    attach_log_bus(app.state.bus, "api")
    await app.state.bus.subscribe_events(app.state.hub.on_event)
    await app.state.bus.subscribe_journal(app.state.hub.on_journal)
    await radio_tuner.start(app)  # radio:tuner — cycle the OPUS stations onto a player
    await camera.serve_snap_requests(app)  # dida.camera.snap → a frame a push can show
    await serve_broker(app.state.bus, app.state.pool)  # adapters' only way to config and records
    backup_task = asyncio.create_task(backup_scheduler(app))  # scheduled backups
    alert_task = asyncio.create_task(alert_loop(app))  # system-health alert evaluator
    log.info("api up")
    try:
        yield
    finally:
        backup_task.cancel()
        alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await backup_task
        with suppress(asyncio.CancelledError):
            await alert_task
        await app.state.bus.close()
        await app.state.pool.close()
        if app.state.ch is not None:
            await app.state.ch.close()


app = FastAPI(title="DIDA API", lifespan=lifespan)
app.state.hub = Hub()
app.include_router(system_router)  # observability: /system/stats + /metrics (Prometheus)
app.include_router(alerts_router)  # system-health alerts: /system/alerts + /system/alert-rules
app.include_router(orphans.router)  # dangling entity references: /system/orphans
app.include_router(smartthings_router)  # SmartThings OAuth handshake (Samsung cloud)
app.include_router(contacts_router)  # household address book: Google handshake + birthdays
app.include_router(upkeep_router)  # address-book upkeep: propose, confirm, write back
app.include_router(pipeline_router)  # audio signal-path (source → speaker)
app.include_router(opus_router)  # the media page's content: the OPUS shelf and stations
app.include_router(volume_presets_router)  # global AVR volume presets (Quiet/Normal/Loud)
app.include_router(users.router)  # user management (admin CRUD)
app.include_router(zones.router)  # geofence zones (list; admin CRUD)
app.include_router(vacuum.router)  # the robot's map + where it lies on the plan
app.include_router(virtual.router)  # virtual-entity helpers (list; admin CRUD)
app.include_router(computed_helpers.router)  # computed helpers (pure derivation layer)
app.include_router(schedules.router)  # calendar/"beat" schedules (list; admin CRUD)
app.include_router(heating.router)  # heating: house-wide settings + per-room config
app.include_router(settings.router)  # admin settings (API keys, TTS lang, network)
app.include_router(automations.router)  # automation rules (CRUD, AI synth, run)
app.include_router(devices.router)  # device list / label+room edit / remove
app.include_router(entities.router)  # entity + area (room) topology
app.include_router(entry.router)  # /entry access surface (gates + door lock)
app.include_router(camera.router)  # /cameras — go2rtc stream proxy (snapshot + mp4)
app.include_router(photos.router)  # OPUS photographs — wall-panel screensaver slideshow
app.include_router(panel.router)  # wall-panel telemetry (/panel/event) + display config
app.include_router(androidtv_router)  # androidtv live screen snapshots (screencap)
app.include_router(floors.router)  # floors: levels + scaffold plan image (upload/serve)
app.include_router(adapters.router)  # Settings → Adapters (config, discovery, onboarding)
app.include_router(retention_router)  # history retention matrix (admin)
app.include_router(push.router)  # web-push subscriptions + notify-target directory
app.include_router(owntracks.router)  # OwnTracks HTTP-mode GPS receiver (Basic auth)
app.include_router(mobile.router)  # Android companion app: provisioning + APK sideload
app.include_router(translations.router)  # display-name translations (descriptors → localised)
app.include_router(backup_router)  # Settings → Backup: pg_dump download + restore (admin)
app.include_router(scenes.router)  # scenes: snapshot + recall device state

_origins = [o for o in os.environ.get("DIDA_CORS_ORIGINS", "").split(",") if o] or _DEV_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    # Unauthenticated on purpose — container healthcheck + tunnel origin probe.
    return {"ok": True}


# Fallback when revision.json is absent (fresh checkout before the hooks ran).
_DEV_REVISION = {
    "version": "0.0.0-dev",
    "base": "0.0",
    "count": 0,
    "sha": "unknown",
    "branch": "unknown",
    "committed_at": None,
    "dirty": True,
}
_REVISION_FILE = os.environ.get("DIDA_REVISION_FILE", "/app/revision.json")


@app.get("/version")
def version() -> dict:
    # Unauthenticated (like /healthz). Single source: revision.json, stamped from
    # git by the push hook (scripts/gen-revision.sh) and bind-mounted read-only.
    # Read per request so a push refreshes it without restarting the API.
    try:
        with open(_REVISION_FILE, encoding="utf-8") as fh:
            return {"product": "dida", **json.load(fh)}
    except (OSError, ValueError):
        return {"product": "dida", **_DEV_REVISION}




# --- auth ----------------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class MeResponse(BaseModel):
    id: str
    username: str
    role: str
    # None = full consumer access; a list = only these page keys are visible.
    allowed_pages: list[str] | None = None
    # False = view-only (cannot operate devices); scoped exceptions live server-side.
    can_control: bool = True
    # UI preferences (theme + language). None = no explicit choice → the client
    # uses its device default; otherwise these follow the user across devices.
    theme: str | None = None
    locale: str | None = None
    # An assistant key is set, so the assistant can answer — the client offers it
    # under a help article only then.
    assistant: bool = False


async def _assistant_ready() -> bool:
    return bool(await stored_api_key(app.state.pool, "anthropic_api_key"))


@app.post("/auth/login", response_model=MeResponse)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    _rl: None = Depends(LOGIN.enforce_client),
) -> MeResponse:
    LOGIN.enforce_account(body.username)
    row = await app.state.pool.fetchrow(
        "SELECT id, username, password_hash, role, token_version, allowed_pages, can_control, "
        "theme, locale FROM users WHERE username = $1",
        body.username,
    )
    if not await verify_password(body.password, row["password_hash"] if row is not None else None):
        # Deliberately vague — don't reveal which of username/password was wrong.
        raise HTTPException(401, "invalid username or password")
    await app.state.pool.execute(
        "UPDATE users SET last_login_at = $1 WHERE id = $2", datetime.now(UTC), row["id"]
    )
    token = encode_session_token(
        int(row["id"]), app.state.secret_key, token_version=int(row["token_version"]), ttl=TOKEN_TTL
    )
    SESSION_COOKIE.set(response, token, request)
    return MeResponse(
        id=str(row["id"]), username=row["username"], role=row["role"],
        allowed_pages=row["allowed_pages"], can_control=row["can_control"],
        theme=row["theme"], locale=row["locale"], assistant=await _assistant_ready(),
    )


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    token = request_token(request)
    if token:
        await revoke_session(app.state.pool, token, app.state.secret_key)
        await app.state.hub.disconnect_token(token)
    SESSION_COOKIE.clear(response)


class PanelAuthBody(BaseModel):
    k: str = Field(..., min_length=8, max_length=128)


@app.post("/auth/panel", response_model=MeResponse)
async def auth_panel(
    body: PanelAuthBody, request: Request, response: Response,
    _rl: None = Depends(LOGIN.enforce_client),
) -> MeResponse:
    """Auto-login for the living-room wall panel. Its cast URL carries the stable
    panel token (`?k=`); we exchange it for a normal session cookie for the
    `wallpanel` user so the display never shows a login screen. Constant-time
    compare; rate-limited like /auth/login and /auth/link so the token can't be
    brute-forced; revocable by regenerating app_settings.panel_token."""
    user = await verify_panel_key(app.state.pool, body.k)
    if user is None:
        raise HTTPException(401, "invalid panel key")
    token = encode_session_token(user.id, app.state.secret_key, token_version=user.token_version, ttl=TOKEN_TTL)
    SESSION_COOKIE.set(response, token, request)
    return MeResponse(
        id=str(user.id), username=user.username, role=user.role,
        allowed_pages=user.allowed_pages, can_control=user.can_control,
        theme=user.theme, locale=user.locale, assistant=await _assistant_ready(),
    )


@app.get("/auth/link")
async def auth_link(
    k: str, request: Request, next: str | None = None, _rl: None = Depends(LOGIN.enforce_client)
) -> RedirectResponse:
    """Per-user QR auto-login (family onboarding). The login QR encodes
    `{public}/api/auth/link?k=<login_token>`; scanning it exchanges the token for a
    normal session cookie and redirects.

    The token is REUSABLE until rotated/revoked (or its TTL backstop expires) —
    deliberately, per Ivo: onboarding a phone takes several scans in practice
    (install, then sign in; a mis-scan in between), and a one-time token turned
    every second scan into a surprise login screen. The QR is a household
    credential the admin hands out and can kill at any moment ("Novi QR" /
    "Ukloni pristup"); determinism beats one-shot hygiene here.

    Where it lands is chosen by WHO opened it, not a URL param: the native app's
    WebView (DIDA-App UA) goes to /onboard for the location walkthrough; any plain
    browser (iPhone, or an Android phone without the app) flies straight into the
    web dashboard. An explicit same-origin `next` still wins when given."""
    if next and next.startswith("/") and not next.startswith("//"):
        dest = next
    elif "DIDA-App" in request.headers.get("user-agent", ""):
        dest = "/onboard"  # in the app → run the native location walkthrough
    else:
        dest = "/"  # browser (iPhone / not-yet-installed) → straight into the web
    row = None
    if 8 <= len(k) <= 128:
        row = await app.state.pool.fetchrow(
            "UPDATE users SET last_login_at = $2 "
            "WHERE login_token = $1 "
            "  AND (login_token_expires_at IS NULL OR login_token_expires_at > $2) "
            "RETURNING id, username, role, token_version, allowed_pages, can_control",
            k, datetime.now(UTC),
        )
    if row is None:
        # Bad/expired/rotated/revoked QR → just show the login screen, no cookie.
        return RedirectResponse("/", status_code=303)
    token = encode_session_token(
        int(row["id"]), app.state.secret_key, token_version=int(row["token_version"]), ttl=TOKEN_TTL
    )
    resp = RedirectResponse(dest, status_code=303)
    SESSION_COOKIE.set(resp, token, request)
    return resp


@app.get("/me/owntracks")
async def my_owntracks(user: AuthUser = Depends(current_user)) -> dict:
    """Self-service OwnTracks location config for the signed-in user. The
    onboarding page turns `inline_url` into a one-tap `owntracks:///config` import,
    so a family member sets up location sharing from their own phone without an
    admin handing them anything. Generates the user's location token on first
    request (idempotent thereafter)."""
    pool = app.state.pool
    username, token = await owntracks.ensure_location_token(pool, user.id)
    payload = await owntracks.provision_payload(pool, username, token)
    return {"inline_url": payload["inline_url"], "reachable": payload["reachable"], "encrypted": payload["encrypted"]}


@app.get("/auth/me", response_model=MeResponse)
async def me(
    request: Request, response: Response, user: AuthUser = Depends(current_user)
) -> MeResponse:
    # Sliding session: every app open hits /auth/me, so an active device renews
    # its cookie past half-TTL and never sees the login screen again; an idle
    # one still expires after the full TTL.
    refresh_session_cookie(request, response, user, app.state.secret_key)
    return MeResponse(
        id=str(user.id), username=user.username, role=user.role,
        allowed_pages=user.allowed_pages, can_control=user.can_control,
        theme=user.theme, locale=user.locale, assistant=await _assistant_ready(),
    )


class PrefsBody(BaseModel):
    theme: Literal["light", "dark", "system"] | None = None
    locale: Literal["hr", "en"] | None = None


@app.patch("/auth/prefs", status_code=204)
async def update_prefs(body: PrefsBody, user: AuthUser = Depends(current_user)) -> None:
    """Persist the signed-in user's UI preferences (theme + language) so a choice
    made on one device follows them to the next — localStorage is only a per-device
    cache. Partial: only the fields present in the body are written, so the theme
    switcher and the language switcher each update just their own column."""
    sets: list[str] = []
    args: list[object] = []
    if body.theme is not None:
        args.append(body.theme)
        sets.append(f"theme = ${len(args)}")
    if body.locale is not None:
        args.append(body.locale)
        sets.append(f"locale = ${len(args)}")
    if not sets:
        return
    args.append(user.id)
    await app.state.pool.execute(
        f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(args)}", *args  # noqa: S608
    )


class ChangePasswordBody(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


@app.post("/auth/change-password", status_code=204)
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    user: AuthUser = Depends(current_user),
) -> None:
    """Self-service change. Bumps token_version so every OTHER open session is
    invalidated, then reissues a cookie for THIS one so the caller stays in."""
    row = await app.state.pool.fetchrow("SELECT password_hash FROM users WHERE id = $1", user.id)
    if row is None or not await verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(401, "current password is wrong")
    new_hash = await hash_password(body.new_password)
    new_tv = await app.state.pool.fetchval(
        "UPDATE users SET password_hash = $1, token_version = token_version + 1, "
        "login_token = NULL, login_token_expires_at = NULL "
        "WHERE id = $2 RETURNING token_version",
        new_hash,
        user.id,
    )
    token = encode_session_token(user.id, app.state.secret_key, token_version=int(new_tv), ttl=TOKEN_TTL)
    SESSION_COOKIE.set(response, token, request)
    await app.state.hub.disconnect_user(user.id, 1012)


# --- presence (web-app geolocation report) -------------------------------


class PresenceReportIn(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)  # metres
    battery: float | None = Field(default=None, ge=0, le=100)
    tst: float = Field(..., gt=0, allow_inf_nan=False)


@app.post("/presence/report")
async def presence_report(body: PresenceReportIn, user: AuthUser = Depends(current_user)) -> dict:
    """A logged-in DIDA app reports this device's position. Identity is the
    session user — the shared publisher (`dida_api.presence`, also behind the
    OwnTracks HTTP receiver) resolves the zone and publishes onto the bus, so
    every GPS source converges on one `presence:<username>` entity.

    Returns the resolved zone so the reporting device can show "registered as
    <zone>" — the report is observable, not fire-and-forget."""
    return await presence.publish_report(
        app.state, user.username, body.latitude, body.longitude,
        accuracy=body.accuracy, battery=body.battery,
        observed_ns=presence.timestamp_ns(body.tst),
    )


@app.get("/presence/last-locations")
async def presence_last_locations(user: AuthUser = Depends(current_user)) -> dict:
    """Per presence entity, the last GPS-geolocated zone (Island House, Coast
    House, Home-via-GPS …) and when. Lets the UI show "Island House · 09:28" once
    the live signal lapses instead of a bare 'away'.

    ONLY GPS sources count, so the filter is a whitelist on the `presence`
    adapter (the /owntracks and /presence/report publishers) — a blacklist rots
    the day a new network adapter appears (it did: unifi replaced opnsense/wifi
    and its 'Home' rows slipped past the old NOT IN). Network adapters emit just
    'Home'/'away' — a network *state*, not a place, and 'Home' can be a ghost
    association — so a network verdict must never masquerade as a last-known
    LOCATION; a person seen only on the network has no location on record and
    the UI falls back to 'away'.
    Sourced from ClickHouse because current_state holds only the latest value."""
    ch = app.state.ch
    if ch is None:
        return {}
    res = await ch.query(
        "SELECT entity_id, argMax(value_str, ts) AS zone, toUnixTimestamp64Milli(max(ts)) AS ms "
        "FROM state_history "
        "WHERE capability = 'location' AND entity_id LIKE 'presence:%' "
        "  AND adapter = 'presence' "
        "  AND value_str != '' AND value_str != 'away' "
        "GROUP BY entity_id"
    )
    hidden = await hidden_for(app.state.pool, user)
    return {
        eid: {"zone": zone, "ts": int(ms)}
        for eid, zone, ms in res.result_rows
        if eid not in hidden
    }


class PresenceHiddenIn(BaseModel):
    hidden: list[str] = Field(default_factory=list)


@app.get("/presence/hidden")
async def presence_hidden_get(user: AuthUser = Depends(current_user)) -> dict:
    """Which presence people the household curated OUT of the presence panel — a
    GLOBAL display choice (distinct from the per-user view boundary in
    visibility.py). Everyone reads the same set; only an admin edits it (below).
    The panel filters these out client-side; the entities still exist and keep
    tracking, they're just not shown in the family row."""
    raw = await get_setting(app.state.pool, "presence_hidden")
    try:
        hidden = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        hidden = []
    return {"hidden": [h for h in hidden if isinstance(h, str)]}


@app.put("/presence/hidden")
async def presence_hidden_put(
    body: PresenceHiddenIn, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Set the hidden-from-presence list. Only `presence:*` entity_ids are stored —
    the panel is presence-scoped, so a stray id can't smuggle in a non-presence
    entity."""
    hidden = sorted({e for e in body.hidden if isinstance(e, str) and e.startswith("presence:")})
    await set_app_setting(app.state.pool, "presence_hidden", json.dumps(hidden))
    return {"hidden": hidden}


# --- state + history (read: current snapshot + ClickHouse firehose) ------


@app.get("/state")
async def state(user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await app.state.pool.fetch(
        "SELECT entity_id, capability, value, unit, updated_at "
        "FROM current_state ORDER BY entity_id, capability"
    )
    # hidden_for folds BOTH the view-hide rules AND page scope: a narrow-scoped
    # login (e.g. ulaz-only) receives only the entities its pages expose, so it
    # can't enumerate the whole house through the snapshot.
    hidden = await hidden_for(app.state.pool, user)
    return [dict(r) for r in rows if r["entity_id"] not in hidden]


app.include_router(history_routes.router)  # /history, /logs, /history/energy



class CommandIn(BaseModel):
    entity_id: str
    capability: str
    command: str
    args: dict[str, Scalar] = {}


@app.post("/command")
async def command(body: CommandIn, user: AuthUser = Depends(current_user)) -> dict:
    """Act on a device. Validated at the boundary (fail loud) and published to
    the bus; the owning adapter translates it to native protocol. We never write
    state here — the resulting value comes back through the normal device →
    adapter → engine → events path, so the UI reflects what actually happened."""
    try:
        await dispatch_command(app.state.pool, app.state.bus, user, body.entity_id,
                               body.capability, body.command, body.args, source=f"user:{user.username}")
    except CapabilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# --- assistant (Claude, auth-protected) ---------------------------------


class AssistantTurn(BaseModel):
    role: str
    content: str


class AssistantIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[AssistantTurn] = []


@app.post("/assistant")
async def assistant(body: AssistantIn, user: AuthUser = Depends(current_user)) -> StreamingResponse:
    """Natural-language home control, streamed as SSE. Claude drives a tool loop over
    the same validated command/automation path as the REST API AND the same per-user
    access boundary — it enforces the caller's control/view rules and admin-only
    writes, so it can neither bypass the capability model nor act above the grants.

    Events: `{"type":"tool","name":…}` per dispatched tool, then exactly one
    `{"type":"done","reply":…,"actions":[…]}` — or `{"type":"error","detail":…}` if the
    turn dies mid-stream. A turn that touches several tools takes tens of seconds; the
    tool events are what tell the user it is working rather than wedged.

    The gate checks below run BEFORE the response starts, so 403/429/503 are still
    ordinary status codes; only failures after the first byte become error events.
    """
    if not can_see_page(user, "assistant"):
        raise HTTPException(403, "you do not have access to the assistant")
    enforce_assistant_limit(user.username)
    client = await resolve_assistant_client(app.state)
    if client is None:
        raise HTTPException(503, "assistant is not configured — add an Anthropic key in System → Settings")
    ctx = AssistantCtx(
        client=client,
        pool=app.state.pool,
        bus=app.state.bus,
        ch=app.state.ch,
        user=user,
        alert_evaluator=getattr(app.state, "alert_evaluator", None),
    )
    history = [t.model_dump() for t in body.history]

    async def sse():
        try:
            async for event in assistant_events(ctx, history, body.message):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            # Headers are long gone, so this cannot be a 500 — surface it in-band
            # rather than closing the stream silently and leaving a dead spinner.
            log.exception("assistant turn failed mid-stream")
            payload = json.dumps({"type": "error", "detail": "assistant failed"})
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        # no-transform/no-buffering: the UI proxies /api (vite in dev, adapter-node in
        # prod) and the tunnel sits in front of that — any of them buffering the body
        # would collapse the stream back into one late blob.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# --- websocket (live event fan-out to the dashboard) --------------------


def _ws_origin_allowed(ws: WebSocket) -> bool:
    # A browser always sends Origin on a WebSocket, so a client without one is not a
    # page riding someone's cookie. Our proxies rewrite Host to the api's and carry
    # the browser's in X-Forwarded-Host.
    origin = ws.headers.get("origin")
    if origin is None or origin in _origins:
        return True
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host") or ""
    return urlsplit(origin).netloc.lower() == host.lower()


async def _authenticate_ws(ws: WebSocket) -> AuthUser | None:
    """The session check at WS upgrade; on success the user, so the connection can be
    scoped to their visibility. A refused session is accepted and then closed with
    4401: a close before accept answers the handshake with a bare 403, the browser
    sees only 1006, and the UI would keep reconnecting instead of going to sign-in."""
    if not _ws_origin_allowed(ws):
        log.warning("websocket from a foreign origin refused: %s", ws.headers.get("origin"))
        await ws.close(code=4403)
        return None
    token = ws.cookies.get(SESSION_COOKIE.name)
    try:
        if not token:
            raise HTTPException(401, "not authenticated")
        return await session_user(app.state.pool, token, app.state.secret_key)
    except HTTPException:
        await ws.accept()
        await ws.close(code=4401)
        return None


@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    user = await _authenticate_ws(ws)
    if user is None:
        return
    await ws.accept()
    hub = app.state.hub
    token = ws.cookies[SESSION_COOKIE.name]
    while True:
        revision = hub.revision
        try:
            user = await session_user(app.state.pool, token, app.state.secret_key)
        except HTTPException:
            await ws.close(code=4401)
            return
        hidden = await hidden_for(app.state.pool, user)
        allowed = await page_allowed_ids(app.state.pool, user)
        if revision == hub.revision:
            break
    hub.add(ws, hidden, allowed, user.id, token)
    loop = asyncio.get_running_loop()
    checked_at = loop.time()
    try:
        while hub.connected(ws):
            # Events are pushed by the hub; inbound traffic is the client's
            # keepalive. Answering "ping" with a pong gives the client a
            # round-trip liveness signal — a socket a phone's sleep/NAT killed
            # looks OPEN locally, and only a missing pong exposes that.
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=max(0.01, 15 - (loop.time() - checked_at)))
            except TimeoutError:
                msg = None
            if not hub.connected(ws):
                break
            if msg == "ping" or loop.time() - checked_at >= 15:
                try:
                    fresh = await session_user(app.state.pool, token, app.state.secret_key)
                except HTTPException:
                    await hub.disconnect(ws, 4401)
                    break
                if (fresh != user or await hidden_for(app.state.pool, fresh) != hidden
                        or await page_allowed_ids(app.state.pool, fresh) != allowed):
                    await hub.disconnect(ws, 1012)
                    break
                checked_at = loop.time()
            if msg == "ping":
                # Route the pong through the client's writer (the sole sender) so
                # it never races the event fan-out on the same socket.
                hub.enqueue(ws, '{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(ws)
