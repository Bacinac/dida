"""Scenes — named snapshots of device state, recalled in one tap.

A scene captures the current
SETTABLE state of a set of entities (or the whole house) from current_state, and
recall turns each captured {entity_id, capability, value} back into a command via
dida_core.setter_command and republishes it through the normal command path. Only
settable capabilities are captured (sensors and momentary verbs are skipped by
setter_command), so a recall is always a well-formed set of commands.

Create/delete are admin. Recall runs the SAME per-entity boundary as /command
(is_hidden + require_control) on every target, silently skipping what the caller
may not see or control — a scene can never become a permission-escalation path.
"""
from __future__ import annotations

import asyncio
import json

import asyncpg
from dida_core import CapabilityError, prepare_command, setter_command
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.permissions import require_control
from dida_api.visibility import hidden_for, is_hidden

router = APIRouter(tags=["scenes"])


class SceneCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    # Which entities to capture. Empty = every CONTROL entity with settable state.
    # It used to mean literally every row of current_state, which swept up device
    # config (power-on behaviour, startup levels) and every helper value — a
    # whole-house restore wearing the word "scene", and the recall button drove the
    # door lock. Explicit ids are still honoured verbatim: the user chose them.
    entity_ids: list[str] = Field(default_factory=list)


class SceneState(BaseModel):
    entity_id: str = Field(..., min_length=1)
    capability: str = Field(..., min_length=1)
    value: bool | float | int | str


class SceneUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    states: list[SceneState]


@router.get("/scenes")
async def list_scenes(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, name, jsonb_array_length(states) AS count, created_at "
        "FROM scenes ORDER BY lower(name)"
    )
    return [dict(r) for r in rows]


@router.post("/scenes", status_code=201)
async def create_scene(
    body: SceneCreate, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Capture the current settable state into a named scene. jsonb_init decodes
    current_state.value to native Python, so setter_command sees the real value."""
    pool = request.app.state.pool
    if body.entity_ids:
        rows = await pool.fetch(
            "SELECT entity_id, capability, value FROM current_state WHERE entity_id = ANY($1)",
            body.entity_ids,
        )
    else:
        rows = await pool.fetch(
            "SELECT cs.entity_id, cs.capability, cs.value FROM current_state cs "
            "JOIN entities e ON e.entity_id = cs.entity_id "
            "WHERE e.category = 'control' AND e.diagnostic = false"
        )

    states = [
        {"entity_id": r["entity_id"], "capability": r["capability"], "value": r["value"]}
        for r in rows
        if setter_command(r["capability"], r["value"]) is not None  # settable only
    ]
    if not states:
        raise HTTPException(400, "no settable state to capture")

    try:
        # Pass the native list — the pool's jsonb codec (jsonb_init) json.dumps it
        # exactly once. Pre-dumping + ::jsonb would double-encode it into a scalar
        # string and break jsonb_array_length in the list query.
        row = await pool.fetchrow(
            "INSERT INTO scenes (name, states) VALUES ($1, $2) RETURNING id, name, created_at",
            body.name.strip(), states,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "scena s tim imenom već postoji") from exc
    return {"id": row["id"], "name": row["name"], "count": len(states),
            "created_at": row["created_at"]}


@router.get("/scenes/{scene_id}")
async def get_scene(scene_id: int, request: Request,
                    user: AuthUser = Depends(current_user)) -> dict:
    """One scene WITH its contents. The list endpoint returns only a count, so
    until this existed there was no way to see what a scene would do before
    pressing recall — including, in the whole-house snapshots the UI used to
    create, a door lock."""
    row = await request.app.state.pool.fetchrow(
        "SELECT id, name, states, created_at FROM scenes WHERE id = $1", scene_id
    )
    if row is None:
        raise HTTPException(404, "scene not found")
    states = row["states"]
    if isinstance(states, str):
        states = json.loads(states)
    # Don't leak the entity_ids/values of entities this user can't see. Empty
    # for admins and unrestricted users (no change); a narrow-scoped login (e.g. an
    # `ulaz` guest) sees only its own entities, matching what recall already enforces.
    hidden = await hidden_for(request.app.state.pool, user)
    if hidden:
        states = [s for s in states if s.get("entity_id") not in hidden]
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "states": states}


@router.put("/scenes/{scene_id}")
async def update_scene(scene_id: int, body: SceneUpdate, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Replace a scene's name and contents.

    Every entry is re-validated the same way a captured one was: it must be a
    SETTABLE capability, and its value must satisfy that capability's own spec. An
    edited scene therefore cannot hold something recall would have to skip — or,
    worse, something that would reach an adapter as garbage.
    """
    states: list[dict] = []
    for s in body.states:
        setter = setter_command(s.capability, s.value)
        if setter is None:
            raise HTTPException(
                400, f"{s.entity_id}: {s.capability} has no settable value to store"
            )
        command, args = setter
        try:
            await prepare_command(request.app.state.pool, s.entity_id, s.capability,
                                  command, args, source="")
        except CapabilityError as exc:
            raise HTTPException(400, f"{s.entity_id}: {exc}") from exc
        states.append({"entity_id": s.entity_id, "capability": s.capability, "value": s.value})

    try:
        row = await request.app.state.pool.fetchrow(
            "UPDATE scenes SET name = $2, states = $3 WHERE id = $1 "
            "RETURNING id, name, created_at",
            scene_id, body.name.strip(), states,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "a scene with that name already exists") from exc
    if row is None:
        raise HTTPException(404, "scene not found")
    return {"id": row["id"], "name": row["name"], "count": len(states),
            "created_at": row["created_at"]}


# A scene of 200+ entries used to go out as one uninterrupted burst. NATS copes;
# the cloud adapters at the other end (Tuya, Govee, SmartThings) are the ones that
# rate-limit or drop. A small gap costs a five-entity scene 80 ms — imperceptible —
# and gives a large one room to breathe.
_RECALL_GAP_S = 0.02


async def apply_scene(pool, bus, user: AuthUser, scene_id: int, source: str | None = None) -> dict:
    """Apply a scene for `user`; returns {applied, skipped}.

    The boundary lives HERE, not in the route, because the assistant recalls scenes
    through the same code — a second implementation is a second place for the
    per-entity check to rot. Raises CapabilityError when the scene is missing so
    both callers can translate it (404 in the route, tool error for the model).
    """
    row = await pool.fetchrow("SELECT states FROM scenes WHERE id = $1", scene_id)
    if row is None:
        raise CapabilityError("scene not found")
    states = row["states"]
    if isinstance(states, str):
        states = json.loads(states)

    applied = skipped = 0
    for s in states:
        eid, cap, val = s.get("entity_id"), s.get("capability"), s.get("value")
        if not eid or not cap:
            skipped += 1
            continue
        # Same boundary as /command: never act on a hidden or un-granted entity.
        if await is_hidden(pool, user, eid):
            skipped += 1
            continue
        try:
            await require_control(pool, user, eid, cap)
        except HTTPException:
            skipped += 1
            continue
        setter = setter_command(cap, val)
        if setter is None:
            skipped += 1
            continue
        command, args = setter
        try:
            cmd = await prepare_command(pool, eid, cap, command, args,
                                        source=source or f"user:{user.username}:scene:{scene_id}")
        except CapabilityError:
            skipped += 1
            continue
        if applied:
            await asyncio.sleep(_RECALL_GAP_S)  # pace the burst, not the first command
        await bus.publish_command(cmd)
        applied += 1
    return {"applied": applied, "skipped": skipped}


@router.post("/scenes/{scene_id}/recall")
async def recall_scene(
    scene_id: int, request: Request, user: AuthUser = Depends(current_user)
) -> dict:
    """Apply a scene: republish a set-command for each captured state the caller is
    allowed to control. Returns how many were applied vs skipped (hidden / not
    permitted / no longer settable)."""
    try:
        return await apply_scene(
            request.app.state.pool, request.app.state.bus, user, scene_id
        )
    except CapabilityError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/scenes/{scene_id}", status_code=204)
async def delete_scene(
    scene_id: int, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    res = await request.app.state.pool.execute("DELETE FROM scenes WHERE id = $1", scene_id)
    if res.endswith("0"):
        raise HTTPException(404, "scene not found")
