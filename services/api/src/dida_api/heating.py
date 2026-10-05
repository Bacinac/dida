"""Heating routes — the config surface behind the Heating page.

Config only: what the controller DECIDED (each room's target, whether it calls for
heat, whether the boiler runs) is published as `heating:*` state and reaches the UI
over the same WebSocket as every other device, so no second implementation of the
control logic can drift from the first.

House-wide config is one `app_settings` row; a room's config is a column on the
room, validated by the same module the controller loads it with.
"""

from __future__ import annotations

import json
import time

import msgspec
from dida_core import CapabilityError, app_setting, set_app_setting
from dida_core.heating import (
    PROFILES,
    TARGET_MAX,
    TARGET_MIN,
    HeatingSettings,
    RoomEntities,
    RoomHeating,
    derive_rooms,
    validate_room,
    validate_settings,
    valve_ids,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, can_see_page, current_user, require_admin
from dida_api.commands import require_visible_control
from dida_api.visibility import hidden_for

router = APIRouter(prefix="/heating", tags=["heating"])

SETTINGS_KEY = "heating"
# A boost is a nudge, not a new schedule: hours, never days.
MAX_BOOST_MINUTES = 24 * 60


class BoostIn(BaseModel):
    target: float = Field(..., ge=TARGET_MIN, le=TARGET_MAX)
    minutes: int = Field(..., ge=1, le=MAX_BOOST_MINUTES)


async def _settings(pool) -> dict:
    raw = await app_setting(pool, SETTINGS_KEY)
    if not raw:
        return _as_dict(HeatingSettings())
    try:
        return _as_dict(validate_settings(json.loads(raw)))
    except (ValueError, CapabilityError) as exc:
        # A stored config that stopped decoding is a fault to show, not to paper
        # over with defaults that would read as "heating is configured off".
        raise HTTPException(500, f"stored heating settings are invalid: {exc}") from exc


def _as_dict(cfg: HeatingSettings) -> dict:
    # to_builtins, not a field sweep: the house schedule is a list of Slot structs,
    # and a raw struct neither serialises to JSON nor stores as jsonb.
    return msgspec.to_builtins(cfg)


@router.get("")
async def get_heating(request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """The whole picture: house-wide settings, plus every room that HAS a thermostatic
    valve in it — no separate act of adding one. Each room carries what it already
    contains (`sensors`/`valves`, derived from the entities assigned to it) and its
    stored config, which is an overlay on that and is usually empty.

    `orphan_valves` are heads that belong to no room: they cannot be heated, and
    naming them beats leaving the user to notice a room that never appeared.
    """
    if not can_see_page(user, "heating"):
        raise HTTPException(403, "heating access required")
    pool = request.app.state.pool
    contains, orphans = await derive_rooms(pool)
    rows = await pool.fetch("SELECT id, name, kind, heating_config FROM areas ORDER BY id")
    # Don't leak valve/sensor entity_ids this user can't see. Empty for admins
    # and unrestricted users (no change); a narrow-scoped login sees only its own.
    hidden = await hidden_for(pool, user)
    _keep = (lambda ids: [i for i in ids if i not in hidden]) if hidden else (lambda ids: list(ids))
    rooms = []
    for r in rows:
        here = contains.get(r["id"], RoomEntities([], [], []))
        if not here.valves and r["heating_config"] is None:
            continue
        config = dict(r["heating_config"] or _room_dict(RoomHeating()))
        if user.role != "admin" and not _keep(config.get("valves") or here.valves):
            continue
        config["valves"] = _keep(config.get("valves") or [])
        if config.get("sensor") in hidden:
            config["sensor"] = ""
        rooms.append({
            "area_id": r["id"], "name": r["name"], "kind": r["kind"],
            "sensors": _keep(here.sensors), "valves": _keep(here.valves),
            "config": config,
        })
    settings = await _settings(pool)
    for key in ("boiler", "outdoor", "away_helper"):
        if settings.get(key) in hidden:
            settings[key] = ""
    return {
        "settings": settings,
        "profiles": list(PROFILES),
        "rooms": rooms,
        "orphan_valves": _keep(orphans),
    }


@router.put("/settings")
async def put_settings(body: dict, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    pool = request.app.state.pool
    try:
        cfg = validate_settings(body)
    except CapabilityError as exc:
        raise HTTPException(400, str(exc)) from exc
    if cfg.boiler:
        await _require_capability(pool, cfg.boiler, "on_off")
    await set_app_setting(pool, SETTINGS_KEY, json.dumps(_as_dict(cfg)))
    return _as_dict(cfg)


@router.put("/rooms/{area_id}")
async def put_room(area_id: int, body: dict, request: Request,
                   _admin: AuthUser = Depends(require_admin)) -> dict:
    """Write one room's heating config."""
    pool = request.app.state.pool
    try:
        room = validate_room(body)
    except CapabilityError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Every referenced entity must exist — a typo'd valve id would otherwise be a
    # room that silently never heats.
    for valve in room.valves:
        await _require_valve(pool, valve)
    if room.sensor:
        await _require_capability(pool, room.sensor, "temperature")
    stored = _room_dict(room)
    row = await pool.fetchrow(
        "UPDATE areas SET heating_config = $2 WHERE id = $1 RETURNING id", area_id, stored
    )
    if row is None:
        raise HTTPException(404, "room not found")
    return {"area_id": area_id, "config": stored}


@router.delete("/rooms/{area_id}", status_code=204)
async def delete_room(area_id: int, request: Request,
                      _admin: AuthUser = Depends(require_admin)) -> None:
    """Drop this room's config back to the defaults. The room itself stays — it is
    heated because it holds a valve, and clearing an overlay cannot change that."""
    row = await request.app.state.pool.fetchrow(
        "UPDATE areas SET heating_config = NULL WHERE id = $1 AND heating_config IS NOT NULL RETURNING id",
        area_id,
    )
    if row is None:
        raise HTTPException(404, "this room has no heating config")


@router.post("/rooms/{area_id}/boost")
async def boost_room(area_id: int, body: BoostIn, request: Request,
                     user: AuthUser = Depends(current_user)) -> dict:
    """Hold a room at a temperature for a while, then fall back to its schedule.
    Permission is the room's own: whoever may set its valves may boost it."""
    pool = request.app.state.pool
    valves, stored = await _load_room(pool, area_id)
    await _require_room_control(pool, user, valves)
    stored["override_target"] = body.target
    stored["override_until"] = time.time() + body.minutes * 60
    await pool.execute("UPDATE areas SET heating_config = $2 WHERE id = $1", area_id, stored)
    return {"area_id": area_id, "config": stored}


@router.delete("/rooms/{area_id}/boost")
async def clear_boost(area_id: int, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Drop the override and hand the room back to its schedule."""
    pool = request.app.state.pool
    valves, stored = await _load_room(pool, area_id)
    await _require_room_control(pool, user, valves)
    stored["override_target"] = None
    stored["override_until"] = None
    await pool.execute("UPDATE areas SET heating_config = $2 WHERE id = $1", area_id, stored)
    return {"area_id": area_id, "config": stored}


async def _load_room(pool, area_id: int):
    """A room's stored config (defaults when it has none) plus the valves it actually
    contains — a room is heatable because it holds a valve, not because someone once
    saved a config for it."""
    row = await pool.fetchrow("SELECT id, heating_config FROM areas WHERE id = $1", area_id)
    if row is None:
        raise HTTPException(404, "room not found")
    contains, _ = await derive_rooms(pool)
    valves = list(contains.get(area_id, RoomEntities([], [], [])).valves)
    raw = row["heating_config"]
    if raw is None and not valves:
        raise HTTPException(404, "this room has no heating")
    try:
        room = validate_room(raw) if raw is not None else RoomHeating()
    except CapabilityError as exc:
        raise HTTPException(500, f"stored room config is invalid: {exc}") from exc
    return (room.valves or valves), _room_dict(room)


def _room_dict(room) -> dict:
    """Round-trip through the validated struct, so what we store is exactly what the
    controller will decode — no stray keys, no half-typed leftovers."""
    return {
        "enabled": room.enabled,
        "sensor": room.sensor,
        "valves": list(room.valves),
        "offset": room.offset,
        "schedule": [{"days": list(s.days), "at": s.at, "profile": s.profile} for s in room.schedule],
        "window_pause": room.window_pause,
        "can_call_boiler": room.can_call_boiler,
        "override_target": room.override_target,
        "override_until": room.override_until,
    }


async def _require_room_control(pool, user: AuthUser, valves: list[str]) -> None:
    if user.role == "admin":
        return
    if not valves:
        raise HTTPException(403, "you do not have permission to control this room's heating")
    for valve in valves:
        await require_visible_control(pool, user, valve, "target_temperature")


async def _require_capability(pool, entity_id: str, capability: str) -> None:
    caps = await _caps(pool, entity_id)
    if capability not in caps:
        raise HTTPException(400, f"{entity_id} does not expose {capability}")


async def _require_valve(pool, entity_id: str) -> None:
    """A valve override must name an actual thermostatic head — judged by the SAME
    classifier the room derivation uses, so the boundary can't accept something the
    controller would never treat as a valve."""
    await _caps(pool, entity_id)  # exists at all
    rows = await pool.fetch("SELECT entity_id, capabilities FROM entities")
    if entity_id not in valve_ids(rows):
        raise HTTPException(400, f"{entity_id} is not a thermostatic valve")


async def _caps(pool, entity_id: str) -> set[str]:
    caps = await pool.fetchval("SELECT capabilities FROM entities WHERE entity_id = $1", entity_id)
    if caps is None:
        raise HTTPException(400, f"unknown entity {entity_id}")
    return set(caps or [])
