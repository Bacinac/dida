"""Entry-control surface (/entry) — a minimal, least-privilege page exposing only
the property's access points (car gate, pedestrian gate, door lock) as one-tap
actions. Purpose-built for a limited-trust login (a cleaner, a trusted neighbour)
scoped to this page via `allowed_pages` and to these entities via `can_control`:
the page is the face, permissions.py at the command boundary is the real gate.

Config lives in `app_settings.entry_controls` (JSON) — which entity backs each
slot, an optional door-contact sensor for real open/closed state, and whether
opening the door broadcasts a notification. No migration: it's one key in the
existing key/value store.
"""

from __future__ import annotations

import json

from dida_core import CapabilityError, prepare_command, set_app_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.common import as_list, get_setting
from dida_api.permissions import require_control
from dida_api.visibility import is_hidden

router = APIRouter(tags=["entry"])

SETTING_KEY = "entry_controls"
ACTION_SLOTS = ("car", "pedestrian", "door")  # door_contact is state-only

# How to OPEN an access point, by the capability its entity exposes, in priority
# order: a door lock opens by unlocking; a cover-gate by opening; a momentary
# relay by a press or an on-pulse (it re-closes via its own local automation).
_OPEN_BY_CAP: tuple[tuple[str, str, str], ...] = (
    ("lock", "lock", "unlock"),
    ("open_close", "open_close", "open"),
    ("press", "press", "press"),
    ("on_off", "on_off", "turn_on"),
)
_KIND_BY_CAP = {"lock": "lock", "open_close": "cover", "press": "press", "on_off": "switch"}


def _open_action(caps: list[str]) -> tuple[str, str] | None:
    """(capability, command) that opens an entity with these caps, or None."""
    cs = set(caps)
    for cap, capability, command in _OPEN_BY_CAP:
        if cap in cs:
            return capability, command
    return None


async def _load(pool) -> dict:
    raw = await get_setting(pool, SETTING_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _entity(pool, entity_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT entity_id, name, label, capabilities FROM entities WHERE entity_id = $1",
        entity_id,
    )
    if row is None:
        return None
    return {
        "entity_id": row["entity_id"],
        "name": row["label"] or row["name"] or row["entity_id"],
        "caps": as_list(row["capabilities"]),
    }


# ── read: the resolved surface (any authenticated user) ──────────────────────


class EntrySlot(BaseModel):
    slot: str            # "car" | "pedestrian" | "door"
    entity_id: str
    name: str
    kind: str            # "lock" | "cover" | "press" | "switch" — drives the tile
    # Optional live-status source for the tile badge — a DIFFERENT entity than the
    # actuator: the door's Zigbee contact, a gate's BABA occupancy zone, etc. The
    # frontend reads its state cap (contact/occupancy/open_close/scene_state) from
    # the store; `state_fallback` is used only while the primary source is silent.
    state_entity: str | None
    state_fallback: str | None


class EntryConfigOut(BaseModel):
    slots: list[EntrySlot]
    notify_on_open: bool


@router.get("/entry/config", response_model=EntryConfigOut)
async def entry_config(request: Request, _user: AuthUser = Depends(current_user)) -> EntryConfigOut:
    """The configured access points, resolved to live entities. Any logged-in user
    may read it — the page itself is gated by allowed_pages, and this only names
    gates/doors. Actuation is separately permission-checked at /entry/action."""
    pool = request.app.state.pool
    cfg = await _load(pool)
    slots: list[EntrySlot] = []
    for slot in ACTION_SLOTS:
        eid = (cfg.get(slot) or "").strip()
        if not eid:
            continue
        ent = await _entity(pool, eid)
        if ent is None:
            continue  # configured entity vanished — skip rather than show a dead tile
        act = _open_action(ent["caps"])
        if act is None:
            continue
        kind = next((_KIND_BY_CAP[c] for c, *_ in _OPEN_BY_CAP if c in ent["caps"]), "switch")
        slots.append(EntrySlot(
            slot=slot, entity_id=eid, name=ent["name"], kind=kind,
            state_entity=(cfg.get(f"state_{slot}") or "").strip() or None,
            state_fallback=(cfg.get(f"state_{slot}_fallback") or "").strip() or None,
        ))
    return EntryConfigOut(slots=slots, notify_on_open=bool(cfg.get("notify_on_open", True)))


# ── act: open an access point (permission-enforced) ──────────────────────────


class EntryActionIn(BaseModel):
    slot: str


@router.post("/entry/action")
async def entry_action(
    body: EntryActionIn, request: Request, user: AuthUser = Depends(current_user)
) -> dict:
    """Open one access point. The real security boundary (NOT the page): the same
    is_hidden + require_control checks as /command run on the resolved entity, so a
    limited-trust user can only ever open the gates/door explicitly granted them.
    Opening the door optionally broadcasts a notify:all — the honest audit trail
    for an irreversible physical act that software cannot undo."""
    pool = request.app.state.pool
    if body.slot not in ACTION_SLOTS:
        raise HTTPException(400, "unknown action")
    cfg = await _load(pool)
    eid = (cfg.get(body.slot) or "").strip()
    if not eid:
        raise HTTPException(404, "access point is not configured")
    ent = await _entity(pool, eid)
    if ent is None:
        raise HTTPException(404, "device does not exist")
    act = _open_action(ent["caps"])
    if act is None:
        raise HTTPException(409, "device cannot be opened")
    capability, command = act
    # Per-user boundary — identical to /command. A hidden entity is a 404, an
    # un-granted one a 403, both rejected before the bus.
    if await is_hidden(pool, user, eid):
        raise HTTPException(404, "device does not exist")
    await require_control(pool, user, eid, capability)
    try:
        cmd = await prepare_command(pool, eid, capability, command,
                                    source=f"user:{user.username}:entry")
    except CapabilityError as exc:
        raise HTTPException(400, str(exc)) from exc
    bus = request.app.state.bus
    await bus.publish_command(cmd)
    if body.slot == "door" and bool(cfg.get("notify_on_open", True)):
        await bus.publish_command(await prepare_command(
            pool, "notify:all", "notify", "notify",
            {"title": "Ulaz", "message": f"Ulazna vrata otvorena — {user.username}"},
            source=f"user:{user.username}:entry"))
    return {"ok": True}


# ── configure: which entities back the slots (admin) ─────────────────────────


class EntryConfigIn(BaseModel):
    car: str | None = Field(default=None, max_length=128)
    pedestrian: str | None = Field(default=None, max_length=128)
    door: str | None = Field(default=None, max_length=128)
    # Per-slot live-status source (optional). Door also has a fallback (Tuya lock
    # contact) used while its primary (Zigbee) is silent.
    state_car: str | None = Field(default=None, max_length=128)
    state_pedestrian: str | None = Field(default=None, max_length=128)
    state_door: str | None = Field(default=None, max_length=128)
    state_door_fallback: str | None = Field(default=None, max_length=128)
    notify_on_open: bool = True


@router.put("/entry/config", status_code=204)
async def set_entry_config(
    body: EntryConfigIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    """Admin: bind each slot to an entity (empty clears it) and toggle the door
    notification. Stored as one JSON blob in app_settings.entry_controls."""
    data = {
        "car": (body.car or "").strip(),
        "pedestrian": (body.pedestrian or "").strip(),
        "door": (body.door or "").strip(),
        "state_car": (body.state_car or "").strip(),
        "state_pedestrian": (body.state_pedestrian or "").strip(),
        "state_door": (body.state_door or "").strip(),
        "state_door_fallback": (body.state_door_fallback or "").strip(),
        "notify_on_open": bool(body.notify_on_open),
    }
    await set_app_setting(request.app.state.pool, SETTING_KEY, json.dumps(data))
