"""Device-level routes — list, label/room edit, uniform remove, and restore.
Extracted from app.py. A device is one physical thing grouping several entities;
remove blocks the key on the engine (so a still-polling adapter can't re-create
it), then deletes its state/entities/row. Restore lifts that block — the adapter
brings the device back on its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dida_api.auth import AuthUser, can_see_page, current_user, require_admin
from dida_api.common import DEVICE_TYPES

router = APIRouter(tags=["devices"])


class DevicePatch(BaseModel):
    name: str | None = None      # friendly label (empty clears → auto name shows)
    area_id: int | None = None   # room for the whole device (null clears)
    device_type: str | None = None  # canonical DIDA type for the whole device (all its gangs)


@router.get("/devices")
async def devices_list(request: Request, user: AuthUser = Depends(current_user)) -> list[dict]:
    """One row per physical device: adapter, auto name, user label. The UI shows
    label|name as the card header/prefix; entities keep their own names. Gated to
    the pages that render the device list, so a narrow-scoped login (e.g. ulaz-only)
    can't enumerate the household's hardware."""
    if not can_see_page(user, "devices", "floorplan"):
        raise HTTPException(403, "no access to devices")
    rows = await request.app.state.pool.fetch(
        "SELECT device_key, adapter, name, label, site FROM devices ORDER BY device_key"
    )
    return [dict(r) for r in rows]


@router.patch("/devices/{key}")
async def patch_device(key: str, body: DevicePatch, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Device-level edits. `name` sets the LABEL (one devices row, entities' own
    names untouched). `area_id` sets the ROOM for the whole device (all its
    entities). Only fields present in the request are applied."""
    pool = request.app.state.pool
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        label = (body.name or "").strip() or None
        # Grouped devices already have a `devices` row (engine creates it). Ungrouped
        # ones — a single-entity cast/dlna/ha device where the entity IS the device —
        # don't, so UPSERT keyed by the node key (= the entity_id the UI groups on).
        # adapter is NOT NULL, so learn it from the entity/device the key names.
        adapter = await pool.fetchval(
            "SELECT adapter FROM entities WHERE device_key = $1 OR entity_id = $1 LIMIT 1", key
        )
        if adapter is None:
            raise HTTPException(404, "device not found")
        await pool.execute(
            "INSERT INTO devices (device_key, adapter, label) VALUES ($1, $2, $3) "
            "ON CONFLICT (device_key) DO UPDATE SET label = EXCLUDED.label",
            key, adapter, label,
        )
    if "area_id" in fields:
        # Match grouped (device_key) AND ungrouped (entity_id == key) entities.
        await pool.execute(
            "UPDATE entities SET area_id = $1 WHERE device_key = $2 OR entity_id = $2",
            body.area_id, key,
        )
    if "device_type" in fields:
        dt = body.device_type
        if dt not in DEVICE_TYPES:
            raise HTTPException(400, "invalid device_type")
        # Set DIDA's canonical type across the whole device (all its gangs).
        await pool.execute(
            "UPDATE entities SET device_type = $1 WHERE device_key = $2 OR entity_id = $2",
            dt, key,
        )
    return {"ok": True}


@router.get("/devices/removed")
async def removed_devices(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Devices someone removed, newest first. Without this the block list is
    invisible: a device that refuses to come back looks like a broken adapter."""
    rows = await request.app.state.pool.fetch(
        "SELECT key, adapter, removed_at FROM removed_devices ORDER BY removed_at DESC"
    )
    return [dict(r) for r in rows]


@router.delete("/devices/removed/{key}", status_code=204)
async def restore_device(key: str, request: Request,
                         _admin: AuthUser = Depends(require_admin)) -> None:
    """Lift the block on a removed device. Nothing is re-created here — the
    adapter re-announces the device on its next poll or reconnect, which is why
    the block had to exist in the first place. A device whose adapter is gone
    (uninstalled, unconfigured, unplugged) simply never returns.

    The engine keeps the block in memory, so the row alone is not enough: tell it
    too, or the restore quietly does nothing until the next engine restart."""
    deleted = await request.app.state.pool.execute(
        "DELETE FROM removed_devices WHERE key = $1", key
    )
    if deleted.endswith(" 0"):
        raise HTTPException(404, "not a removed device")
    await request.app.state.bus.nc.publish("dida.engine.restore", key.encode())


@router.delete("/devices/{key}", status_code=204)
async def delete_device(key: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> None:
    """Remove a device from every adapter, uniformly. `key` is the device_key of a
    grouped device, or the entity_id of an ungrouped one. The engine blocks the key
    so a still-polling adapter can't re-create it (durable); its entities, state and
    friendly-name row are deleted so it disappears from the UI immediately."""
    pool = request.app.state.pool
    adapter = await pool.fetchval(
        "SELECT adapter FROM entities WHERE device_key = $1 OR entity_id = $1 LIMIT 1", key
    )
    await pool.execute(
        "INSERT INTO removed_devices (key, adapter) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET adapter = EXCLUDED.adapter, removed_at = now()",
        key, adapter,
    )
    # Tell the engine to start dropping this key BEFORE we delete, so an in-flight
    # re-publish can't resurrect the rows we're about to remove.
    await request.app.state.bus.nc.publish("dida.engine.forget", key.encode())
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM current_state WHERE entity_id IN "
            "(SELECT entity_id FROM entities WHERE device_key = $1 OR entity_id = $1)", key
        )
        await conn.execute("DELETE FROM entities WHERE device_key = $1 OR entity_id = $1", key)
        await conn.execute("DELETE FROM devices WHERE device_key = $1", key)
