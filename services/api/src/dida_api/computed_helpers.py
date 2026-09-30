"""Computed-helper routes — CRUD for the PURE derivation layer below automations.

A computed helper is a named value DEFINED by a rule over statuses, using the SAME
Trigger/Condition model as automations (evaluated by the same `eval_condition`), plus
an optional advanced Starlark expression. It runs NO action, so it cannot loop; and it
reads only RAW statuses (never another helper/derived), so helpers cannot race/cycle.
The API validates the shape + value type + the no-computed-input rule; the automation
service evaluates it and publishes `helper:<slug>`, surfacing eval errors via last_error.

  definition = {
    "branches": [{"conditions": [<Condition>…], "value": <v>}, …],  # first match wins
    "default":  <v>,                                                # no branch matched
    "script":   ""   # optional advanced Starlark (value = …); overrides branches when set
  }
"""

from __future__ import annotations

import re

import asyncpg
import msgspec
from dida_core import CapabilityError, CapabilityKind, Condition, validate_state
from dida_core.automations import _validate_condition
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin

router = APIRouter(prefix="/computed-helpers", tags=["computed-helpers"])

_COLS = "id, entity_id, name, capability, definition, enabled, last_error, created_at, updated_at"

# The value TYPES a helper may hold — a readable capability. `text` is the free-form
# one (any string); the rest give a typed value when the user wants it.
_ALLOWED = {"text", "boolean", "number", "enum", "time", "identity_presence"}

# A helper may read only KNOWN statuses, never another COMPUTED value — a
# `helper:`/`derived:` reference would make its result depend on the OTHER's evaluation
# order (a race) and let helpers cycle. Forbidding it keeps the layer a FLAT derivation.
_COMPUTED_NS = ("helper", "derived")
_STATE_CALL = re.compile(r"""state\(\s*["']([^"']+)["']""")  # state("eid", …) refs in a script


class HelperIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    capability: str = "text"
    definition: dict = Field(default_factory=dict)  # {branches, default, script?}
    enabled: bool = True


def _cond_entities(c: Condition) -> list[str]:
    """Every entity_id a condition tree references (leaf + nested groups)."""
    if c.kind == "":
        return [c.entity_id] if c.entity_id else []
    return [e for sub in c.conditions for e in _cond_entities(sub)]


def _reject_computed(entity_id: str) -> None:
    if entity_id.split(":", 1)[0] in _COMPUTED_NS:
        raise HTTPException(
            400,
            f"a computed helper may read raw statuses, not another computed value "
            f"({entity_id!r}) — that would create a race/cycle. Read the underlying status instead.",
        )


def _validate(body: HelperIn) -> None:
    if body.capability not in _ALLOWED:
        raise HTTPException(400, f"value type must be one of {sorted(_ALLOWED)}")
    try:
        CapabilityKind(body.capability)
    except ValueError as exc:
        raise HTTPException(400, f"unknown capability {body.capability!r}") from exc

    d = body.definition or {}
    script = str(d.get("script") or "").strip()
    branches = d.get("branches") or []
    if not script and not branches:
        raise HTTPException(400, "a helper needs either branches (conditions→value) or an advanced script")

    if script:
        for ref in _STATE_CALL.findall(script):
            _reject_computed(ref)
        return  # the script's compile is the automation service's job (last_error)

    # Typed branches: validate each condition + value against the model, and that no
    # condition reads a computed value. `default` is required so the helper always
    # resolves to something.
    if "default" not in d:
        raise HTTPException(400, "typed helper needs a `default` value")
    try:
        validate_state(body.capability, d["default"])
        for br in branches:
            conds = msgspec.convert(br.get("conditions") or [], list[Condition])
            if not conds:
                raise HTTPException(400, "each branch needs at least one condition")
            for cond in conds:
                _validate_condition(cond)
                for e in _cond_entities(cond):
                    _reject_computed(e)
            validate_state(body.capability, br.get("value"))
    except (CapabilityError, msgspec.ValidationError) as exc:
        raise HTTPException(400, f"invalid rule: {exc}") from exc


@router.get("")
async def list_helpers(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    rows = await request.app.state.pool.fetch(f"SELECT {_COLS} FROM computed_helpers ORDER BY name")  # noqa: S608
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_helper(body: HelperIn, request: Request,
                        _admin: AuthUser = Depends(require_admin)) -> dict:
    _validate(body)
    import json
    slug = re.sub(r"[^a-z0-9_]+", "_", body.name.strip().lower()).strip("_") or "helper"
    entity_id = f"helper:{slug}"
    try:
        row = await request.app.state.pool.fetchrow(
            f"INSERT INTO computed_helpers (entity_id, name, capability, definition, enabled) "  # noqa: S608
            f"VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING {_COLS}",
            entity_id, body.name.strip(), body.capability, json.dumps(body.definition), body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, f"a helper named {entity_id!r} already exists") from exc
    return dict(row)


@router.put("/{helper_id}")
async def update_helper(helper_id: int, body: HelperIn, request: Request,
                        _admin: AuthUser = Depends(require_admin)) -> dict:
    _validate(body)
    import json
    row = await request.app.state.pool.fetchrow(
        f"UPDATE computed_helpers SET name = $2, capability = $3, definition = $4::jsonb, "  # noqa: S608
        f"enabled = $5, last_error = NULL, updated_at = now() WHERE id = $1 RETURNING {_COLS}",
        helper_id, body.name.strip(), body.capability, json.dumps(body.definition), body.enabled,
    )
    if row is None:
        raise HTTPException(404, "helper not found")
    return dict(row)


@router.delete("/{helper_id}", status_code=204)
async def delete_helper(helper_id: int, request: Request,
                        _admin: AuthUser = Depends(require_admin)) -> None:
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        eid = await conn.fetchval("SELECT entity_id FROM computed_helpers WHERE id = $1", helper_id)
        if eid is None:
            raise HTTPException(404, "helper not found")
        await conn.execute("DELETE FROM current_state WHERE entity_id = $1", eid)
        await conn.execute("DELETE FROM entities WHERE entity_id = $1", eid)
        await conn.execute("DELETE FROM computed_helpers WHERE id = $1", helper_id)
