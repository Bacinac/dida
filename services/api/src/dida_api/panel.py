"""Wall-panel plumbing — telemetry + display configuration.

The living-room panel (/panel, wallpanel user) is a headless kiosk: no one is
standing in front of it to change settings, so its behaviour is stored server-
side and edited from Settings → Panel. Both the panel and the settings page read
one config blob (app_settings key `panel_config`); only an admin writes it.

Telemetry (/panel/event) logs the panel's own state transitions so a headless
device's behaviour — DashCast touch delivery included — is verifiable from
`docker logs dida-api` without standing in front of it. /panel/alive is the
separate, unlogged liveness beat the cast adapter watches (see PANEL_SEEN_KEY).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from dida_core import set_app_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import WALLPANEL_USERNAME, AuthUser, current_user, require_admin
from dida_api.common import get_setting

log = logging.getLogger("dida.api")
router = APIRouter(tags=["panel"])

_PANEL_EVENTS = {"boot", "tap", "wake", "saver", "bell", "sleep"}
# Liveness stamp, written here by the panel's heartbeat and by the cast adapter
# when it (re)casts the panel; the adapter reads the row's `updated_at` age to
# decide whether the display is still ours. Shared key, one meaning: "the wall
# panel was last known alive at this moment".
PANEL_SEEN_KEY = "panel_seen_at"
_TRANSITIONS = ("kenburns", "fade", "none")
_FITS = ("blur", "contain", "cover")
_ENTITY_RE = re.compile(r"^[a-zA-Z0-9_:.-]{1,128}$")

# The panel's display behaviour, with its shipped defaults. A stored blob is
# merged OVER these, so a new field added here appears on every panel without a
# migration, and a partial/corrupt stored value never leaves a field undefined.
DEFAULTS: dict = {
    "idle_s": 120,           # no touch for this long → screensaver
    "photo_interval_s": 20,  # screensaver: seconds per photo
    "transition": "kenburns",  # photo transition: kenburns | fade | none
    "photo_fit": "blur",     # how a photo fills the screen: blur | contain | cover
    "presence_entity": "",   # sleep the display when this entity is empty ("" → always on)
}


async def get_panel_config(pool) -> dict:
    """The effective panel config: shipped defaults with the stored blob merged
    over them. Shared by the panel and the settings page, so both agree on one
    source of truth."""
    cfg = dict(DEFAULTS)
    raw = await get_setting(pool, "panel_config")
    if raw:
        try:
            stored = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            stored = {}
        if isinstance(stored, dict):
            for k in DEFAULTS:
                if k in stored:
                    cfg[k] = stored[k]
    return cfg


class PanelEventBody(BaseModel):
    kind: str = Field(..., max_length=32)


class PanelConfigIn(BaseModel):
    # Only provided fields are touched; each is range/enum-validated on write.
    idle_s: int | None = Field(default=None, ge=30, le=3600)
    photo_interval_s: int | None = Field(default=None, ge=5, le=300)
    transition: str | None = None
    photo_fit: str | None = None
    presence_entity: str | None = Field(default=None, max_length=128)


@router.post("/panel/event", status_code=204)
async def panel_event(body: PanelEventBody, user: AuthUser = Depends(current_user)) -> None:
    """Wall-panel telemetry: the display logs its state transitions (boot, tap,
    screensaver in/out, bell interrupt) so a headless device's behaviour — in
    particular whether DashCast on the Nest Hub actually delivers touch — is
    verifiable from `docker logs dida-api`."""
    if body.kind not in _PANEL_EVENTS:
        raise HTTPException(400, "unknown event")
    log.info("panel: %s (user=%s)", body.kind, user.username)


@router.post("/panel/alive", status_code=204)
async def panel_alive(request: Request, user: AuthUser = Depends(current_user)) -> None:
    """Kiosk liveness beat — deliberately NOT logged (it is a heartbeat, not a
    transition; logging it would bury the telemetry above under 1400 lines a day).

    Why it exists: nothing running in the panel can recover the panel. If the page
    dies — a reload that lands mid-deploy on a proxy error page, the Hub's aged
    Chromium losing the tab — the Cast receiver stays busy showing that corpse, so
    the adapter's `is_idle` check reads "screen in use" and leaves it there. That
    is exactly how an error page sat on the living-room wall for twelve hours on
    2026-07-22. The missing beat is the signal the adapter re-casts on.

    Only the kiosk user stamps it: an admin idly leaving /panel open in a desktop
    tab must not keep the watchdog fed while the Hub itself is dead."""
    if user.username != WALLPANEL_USERNAME:
        return
    await set_app_setting(
        request.app.state.pool, PANEL_SEEN_KEY, datetime.now(UTC).isoformat()
    )


async def _panel_room(pool) -> int | None:
    """The area the panel physically lives in — the area of the cast device it's
    displayed on (cast adapter's `panel_device`). Used to narrow the presence
    picker to sensors in the panel's own room. None if unconfigured."""
    from dida_core import AdapterConfig
    try:
        cfg = AdapterConfig("cast", pool)
        await cfg.load()
    except Exception:
        log.debug("panel: cast config unreadable", exc_info=True)
        return None
    dev = cfg.get("panel_device").strip()
    if not dev:
        return None
    return await pool.fetchval("SELECT area_id FROM entities WHERE entity_id = $1", dev)


@router.get("/panel/config")
async def panel_config(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """The panel's display config. Any authed user reads it (the wallpanel needs
    it at boot); only an admin changes it via PUT. `room_area_id` (read-only) is
    the panel's own area, so Settings can scope the presence picker to that room."""
    pool = request.app.state.pool
    cfg = await get_panel_config(pool)
    return {**cfg, "room_area_id": await _panel_room(pool)}


@router.put("/panel/config", status_code=204)
async def set_panel_config(
    body: PanelConfigIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    """Merge the provided fields into the stored config (partial update). Values
    are already range-checked by the model; the enums and the entity shape are
    checked here — a bad transition or entity id is rejected loud, never stored."""
    pool = request.app.state.pool
    cfg = await get_panel_config(pool)
    data = body.model_dump(exclude_none=True)
    if "transition" in data and data["transition"] not in _TRANSITIONS:
        raise HTTPException(400, "unknown transition")
    if "photo_fit" in data and data["photo_fit"] not in _FITS:
        raise HTTPException(400, "unknown photo fit")
    if "presence_entity" in data and data["presence_entity"] and not _ENTITY_RE.match(data["presence_entity"]):
        raise HTTPException(400, "invalid presence entity")
    cfg.update(data)
    # Store only the fields we know — never let an unexpected key ride in.
    await set_app_setting(pool, "panel_config", json.dumps({k: cfg[k] for k in DEFAULTS}))
