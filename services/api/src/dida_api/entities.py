"""Entity + area (room) routes — the device-topology domain. Extracted from
app.py. Entities carry metadata (name, room, floor-plan placement, exposure);
areas group them into rooms; auto-assign derives rooms from entity ids.
All read/write Postgres directly; no bus.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.common import DEVICE_TYPES
from dida_api.stats import entity_stats
from dida_api.visibility import hidden_for, is_hidden

log = logging.getLogger("dida.api.entities")

router = APIRouter(tags=["entities"])


class AreaIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class EntityPatch(BaseModel):
    area_id: int | None = None
    label: str | None = Field(default=None, max_length=128)  # user name override (null → adapter name)
    device_type: str | None = None  # canonical DIDA type for this entity/gang (DIDA's truth)
    exposed: bool | None = None
    voice_exposed: bool | None = None      # push this entity to the voice assistant (Google/Matter)
    hidden_caps: list[str] | None = None   # capabilities the user hid from Devices
    fp_floor: str | None = Field(default=None, max_length=32)
    fp_x: float | None = None
    fp_y: float | None = None
    fp_motion_floor: str | None = Field(default=None, max_length=32)  # motion marker, placed apart from the value label
    fp_motion_x: float | None = None
    fp_motion_y: float | None = None
    fp_glow: dict | None = None            # floor-plan illumination override ({r} radius), null → area fallback
    fp_style: dict | None = None           # floor-plan marker style ({icon, scale}), null → auto


class AreaPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)
    fp_floor: str | None = Field(default=None, max_length=32)
    fp_x: float | None = None
    fp_y: float | None = None
    fp_poly: list | None = None            # floor-plan room polygon [[x,y],…] (% coords)
    sensor_config: dict | None = None      # room sensor-label config {hidden, excluded, primary}
    media_config: dict | None = None       # room media-source registry {sources:[…]} (MediaHub)


@router.get("/entities")
async def entities(request: Request, user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT e.entity_id, e.name, e.label, e.adapter, e.device_type, e.area_id, e.diagnostic, e.category, "
        "       e.exposed, e.voice_exposed, e.voice_effective, e.hidden_caps, e.device_key, e.fp_floor, e.fp_x, e.fp_y, "
        "       e.fp_motion_floor, e.fp_motion_x, e.fp_motion_y, e.fp_glow, e.fp_style, e.capabilities, e.last_seen, "
        # Reachability is a device property; an entity inherits its device's. No
        # device row (ungrouped) or no adapter verdict yet → reachable, so nothing
        # reads as dead until an adapter actually says so — or its adapter has gone
        # silent, when the verdict it left behind is nobody's any more.
        "       COALESCE(d.reachable, true) AND COALESCE(l.alive, true) AS reachable, "
        "       CASE WHEN l.alive = false AND COALESCE(d.reachable, true) THEN l.since "
        "            ELSE d.reachable_since END AS reachable_since, "
        "       m.since AS manual_since "
        "FROM entities e LEFT JOIN devices d ON d.device_key = e.device_key "
        "LEFT JOIN adapter_liveness l ON l.adapter = e.adapter "
        "LEFT JOIN manual_overrides m ON m.entity_id = e.entity_id ORDER BY e.entity_id"
    )
    hidden = await hidden_for(request.app.state.pool, user)  # view rules + page scope
    return [dict(r) for r in rows if r["entity_id"] not in hidden]


@router.get("/entities/{entity_id:path}/detail")
async def entity_detail(entity_id: str, request: Request,
                        user: AuthUser = Depends(current_user)) -> dict:
    """Everything about ONE entity, in one call — the backend half of "click a
    device and see all of it".

    The pieces are already in the system but scattered across four surfaces: the
    registry row (Devices), the live values (the WS stream), who else sits on the
    same physical device (nowhere), what recently commanded it (Settings → Commands,
    admin-only and global) and which rules act on it (Automations, by reading every
    definition). Answering "what IS this thing and why did it just do that" meant
    visiting all of them; this returns the lot.

    Same view gate as everywhere else: an entity hidden from this user is a 404,
    not an empty shell that confirms it exists."""
    pool = request.app.state.pool
    if await is_hidden(pool, user, entity_id):
        raise HTTPException(404, "entity not found")
    row = await pool.fetchrow(
        "SELECT entity_id, name, label, adapter, device_type, area_id, diagnostic, category, "
        "       exposed, voice_exposed, voice_effective, hidden_caps, device_key, capabilities, fp_style, "
        "       last_seen FROM entities WHERE entity_id = $1", entity_id)
    if row is None:
        raise HTTPException(404, "entity not found")
    detail: dict = dict(row)

    # Live values, with age — a stale reading is a fact about the device, so the
    # surface can grey it out instead of showing a number that stopped being true.
    detail["state"] = [dict(r) for r in await pool.fetch(
        "SELECT capability, value, unit, updated_at, changed_at, "
        "       EXTRACT(EPOCH FROM (now() - updated_at))::int AS age_s "
        "FROM current_state WHERE entity_id = $1 ORDER BY capability", entity_id)]

    # The rest of the physical device. A TRV is nine entities; knowing which ones
    # are siblings is what turns a list of ids back into one thing.
    hidden = await hidden_for(pool, user)
    if row["device_key"]:
        sibs = [
            dict(r) for r in await pool.fetch(
                "SELECT entity_id, name, label, category, diagnostic FROM entities "
                "WHERE device_key = $1 AND entity_id <> $2 ORDER BY entity_id",
                row["device_key"], entity_id)
            if r["entity_id"] not in hidden
        ]
        # …and their VALUES. A device is often several entities (a meter is five:
        # power, voltage, current, import, export), so a panel showing only the one
        # that happened to be clicked reads as half-empty — the user asked what the
        # DEVICE is doing, not that entity. One query for the whole group.
        by_entity: dict[str, list] = {}
        for r in await pool.fetch(
            "SELECT entity_id, capability, value, unit, updated_at, "
            "       EXTRACT(EPOCH FROM (now() - updated_at))::int AS age_s "
            "FROM current_state WHERE entity_id = ANY($1::text[]) ORDER BY entity_id, capability",
            [s["entity_id"] for s in sibs],
        ):
            by_entity.setdefault(r["entity_id"], []).append(
                {k: v for k, v in dict(r).items() if k != "entity_id"})
        for sib in sibs:
            sib["state"] = by_entity.get(sib["entity_id"], [])
        detail["siblings"] = sibs
    else:
        detail["siblings"] = []

    # Which rules reference it — trigger, condition or action. The definition is
    # jsonb and the shape varies by layer (typed vs Starlark), so match on the id
    # appearing anywhere in the definition rather than teaching this route the
    # whole rule grammar. Cheap: a house has tens of rules, not thousands.
    detail["automations"] = [dict(r) for r in await pool.fetch(
        "SELECT id, name, enabled, last_triggered_at FROM automations "
        "WHERE definition::text LIKE '%' || $1 || '%' ORDER BY name", entity_id)]

    # Who recently told it what. ADMIN ONLY, matching /history/commands: `source`
    # names the person or rule behind each command, so showing it to a non-admin
    # would expose other users' actions. A non-admin simply gets no commands key
    # rather than an empty list that implies nothing happened.
    ch = getattr(request.app.state, "ch", None)

    # What HAPPENED to it — the journal (adapter online/offline, a rejected
    # reading, the rule that fired, a car arriving at a parking place). Not
    # admin-gated the way commands are: `commands` name WHO ACTED, which is the
    # thing that has to be earned. The journal names services and rules, and —
    # since 28.08 — an identity the entity it is filed against already shows in
    # its own state, so the panel's view gate above is what decides, and nothing
    # here reveals a person to a reader the state would not have. It answers the
    # question the panel exists for ("why did it stop reporting") for whoever is
    # looking.
    # Scoped to the whole physical device, since an event about one entity of a
    # TRV is an event about the TRV.
    detail["events"] = []
    if ch is not None:
        ids = [entity_id, *(s["entity_id"] for s in detail["siblings"])]
        try:
            res = await ch.query(
                "SELECT toUnixTimestamp64Milli(ts) AS ms, entity_id, source, kind, severity, message, data "
                "FROM device_events "
                "WHERE (entity_id IN {ids:Array(String)} "
                "       OR (device_key != '' AND device_key = {dk:String}) "
                # An adapter's own online/offline carries no entity — it is one
                # event for the whole protocol. Without this the panel would show
                # a device that simply stopped reporting and stay silent about the
                # bridge that died under it, which is the question it exists for.
                "       OR (entity_id = '' AND device_key = '' AND source = {src:String})) "
                "  AND ts > now() - toIntervalHour(168) ORDER BY ts DESC LIMIT 50",
                parameters={"ids": ids, "dk": row["device_key"] or "",
                            "src": f"adapter:{row['adapter']}"})
            detail["events"] = [
                {"ms": r[0], "entity_id": r[1], "source": r[2], "kind": r[3],
                 "severity": r[4], "message": r[5], "data": r[6]}
                for r in res.result_rows
            ]
        except Exception:
            # Same as commands below: a ClickHouse blip degrades the timeline, it
            # does not take down the page that answers "what is this device".
            log.warning("entity detail: journal of %s unreadable", entity_id, exc_info=True)
            pass

    if user.role == "admin" and ch is not None:
        try:
            res = await ch.query(
                "SELECT toUnixTimestamp64Milli(ts) AS ms, capability, command, source, args "
                "FROM command_history WHERE entity_id = {e:String} "
                "  AND ts > now() - toIntervalHour(168) ORDER BY ts DESC LIMIT 20",
                parameters={"e": entity_id})
            detail["commands"] = [
                {"ms": r[0], "capability": r[1], "command": r[2], "source": r[3], "args": r[4]}
                for r in res.result_rows
            ]
        except Exception:
            # The audit trail is a nice-to-have here; a ClickHouse blip must not
            # take down the page that answers "what is this device".
            log.warning("entity detail: command history of %s unreadable", entity_id, exc_info=True)
            detail["commands"] = []
    return detail


@router.get("/entities/{entity_id:path}/stats")
async def entity_stats_route(entity_id: str, request: Request, days: int = 30,
                             user: AuthUser = Depends(current_user)) -> dict:
    """How much and how often — kWh drawn, hours on, cycles — over `days`.

    Same visibility rule as the rest of the panel: an entity this user may not see
    is a 404, not an empty answer."""
    pool = request.app.state.pool
    if await is_hidden(pool, user, entity_id):
        raise HTTPException(404, "entity not found")
    row = await pool.fetchrow("SELECT capabilities FROM entities WHERE entity_id = $1", entity_id)
    if row is None:
        raise HTTPException(404, "entity not found")
    ch = getattr(request.app.state, "ch", None)
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    caps = row["capabilities"]
    if isinstance(caps, str):
        caps = json.loads(caps)
    return {"entity_id": entity_id, **await entity_stats(ch, entity_id, list(caps or []), days)}


@router.patch("/entities/{entity_id:path}")
async def patch_entity(entity_id: str, body: EntityPatch, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Assign an entity to a room, rename it, set its canonical type, or place it."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "nothing to update")
    # device_type is DIDA's canonical kind — must be a real type, never cleared to
    # null (every entity carries a concrete type).
    if "device_type" in fields and fields["device_type"] not in DEVICE_TYPES:
        raise HTTPException(400, "invalid device_type")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await request.app.state.pool.fetchrow(
        f"UPDATE entities SET {sets} WHERE entity_id = $1 "  # noqa: S608
        "RETURNING entity_id, name, label, adapter, device_type, area_id, exposed, voice_exposed, voice_effective, "
        "          hidden_caps, fp_floor, fp_x, fp_y, fp_motion_floor, fp_motion_x, fp_motion_y, fp_glow, fp_style",
        entity_id, *fields.values(),
    )
    if row is None:
        raise HTTPException(404, "entity not found")
    return dict(row)


@router.get("/areas")
async def list_areas(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, kind, fp_floor, fp_x, fp_y, fp_poly, sensor_config, media_config "
        "FROM areas ORDER BY name NULLS LAST, id"
    )
    return [dict(r) for r in rows]


@router.patch("/areas/{area_id}")
async def patch_area(area_id: int, body: AreaPatch, request: Request,
                     _admin: AuthUser = Depends(require_admin)) -> dict:
    """Rename, retype (kind), place, and/or trace an area (room) on the floorplan.
    fp_poly is jsonb — asyncpg's codec encodes the list from the column type."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "nothing to update")
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    row = await request.app.state.pool.fetchrow(
        f"UPDATE areas SET {sets} WHERE id = $1 "  # noqa: S608
        "RETURNING id, name, kind, fp_floor, fp_x, fp_y, fp_poly, sensor_config, media_config",
        area_id, *fields.values(),
    )
    if row is None:
        raise HTTPException(404, "area not found")
    return dict(row)


@router.post("/areas", status_code=201)
async def create_area(body: AreaIn, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    # Manually-created rooms carry a user name; kind is optional (set later).
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO areas (name) VALUES ($1) RETURNING id, name, kind", body.name.strip(),
    )
    return dict(row)


@router.delete("/areas/{area_id}", status_code=204)
async def delete_area(area_id: int, request: Request, _admin: AuthUser = Depends(require_admin)) -> None:
    # entities.area_id is ON DELETE SET NULL, so members just become unassigned.
    await request.app.state.pool.execute("DELETE FROM areas WHERE id = $1", area_id)


# Keyword → (canonical room kind, optional fixed name). First match wins.
# `kind` is translated in the UI (room.kind.*); `name` is set only for rooms
# that are proper names (the kids' rooms) rather than a type.
_ROOM_KEYWORDS: list[tuple[str, str, str | None]] = [
    ("ada", "bedroom", "Ada"), ("bea", "bedroom", "Bea"), ("cleo", "bedroom", "Cleo"),
    ("bedroom", "bedroom", None), ("bathroom", "bathroom", None), ("kitchen", "kitchen", None),
    ("living", "living", None), ("dining", "dining", None), ("toilet", "toilet", None),
    ("downstairs", "downstairs", None), ("upstairs", "upstairs", None), ("midstairs", "stairs", None),
    ("entrance", "entrance", None), ("backdoor", "backdoor", None), ("gate", "gate", None),
    ("patio", "patio", None), ("outside", "outdoor", None), ("garage", "garage", None),
    ("barrack", "utility", None),
    ("fridge", "kitchen", None), ("dryer", "kitchen", None),
    # NB: functional groupings (irrigation / reflectors / appliances via plug/ups)
    # are deliberately NOT auto-roomed — they are device functions, not spaces, and
    # only cluttered the room list. Such devices stay unassigned for manual placing.
]


@router.post("/areas/auto-assign")
async def auto_assign_areas(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Best-effort: derive a room (kind + optional name) from each unassigned
    entity's id and assign it, reusing existing rooms. Existing assignments and
    names are left untouched."""
    pool = request.app.state.pool
    # Seed dedup cache from existing rooms so we reuse, never duplicate. Keys are
    # NAMESPACED: a proper-named room ("Ada") is keyed by name, a generic room by
    # kind. Crucially a generic room is seeded by kind ONLY when it has no proper
    # name — else "Outdoor" (name that equals its type) would be keyed by name and
    # a generic outdoor match (keyed by kind) would miss it and spawn a duplicate,
    # while a named bedroom (Ada) must NOT swallow generic "bedroom" devices.
    cache: dict[str, int] = {}
    for a in await pool.fetch("SELECT id, name, kind FROM areas"):
        if a["name"]:
            cache.setdefault(f"name:{a['name']}", a["id"])
        elif a["kind"]:
            cache.setdefault(f"kind:{a['kind']}", a["id"])

    rows = await pool.fetch("SELECT entity_id FROM entities WHERE area_id IS NULL")
    assigned = 0
    for r in rows:
        eid = r["entity_id"].lower()
        hit = next(((kind, name) for kw, kind, name in _ROOM_KEYWORDS if kw in eid), None)
        if hit is None:
            continue
        kind, name = hit
        key = f"name:{name}" if name else f"kind:{kind}"
        area_id = cache.get(key)
        if area_id is None:
            area_id = await pool.fetchval(
                "INSERT INTO areas (name, kind) VALUES ($1, $2) RETURNING id", name, kind
            )
            cache[key] = area_id
        await pool.execute("UPDATE entities SET area_id = $2 WHERE entity_id = $1", r["entity_id"], area_id)
        assigned += 1
    return {"assigned": assigned, "rooms": len(cache)}
