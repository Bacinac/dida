"""Automation rule routes — CRUD, AI synthesize/explain, and force-run.
Extracted from app.py. Definitions are validated against the capability model on
write (the boundary rejects a bad/AI-hallucinated rule); run publishes to the
automation service over the bus.
"""

from __future__ import annotations

import json
import logging

from dida_core import CapabilityError, validate_definition
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.assistant import CHECK_SUBJECT as _CHECK_SUBJECT
from dida_api.assistant import RUN_SUBJECT as _RUN_SUBJECT
from dida_api.assistant import explain_automation, synthesize_definition
from dida_api.auth import AuthUser, require_admin
from dida_api.common import resolve_assistant_client

log = logging.getLogger("dida.api.automations")

router = APIRouter(prefix="/automations", tags=["automations"])

_COLS = ("id, name, enabled, definition, last_triggered_at, last_error, "
         "created_at, updated_at")


class AutomationIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    definition: dict
    enabled: bool = True


class EnabledPatch(BaseModel):
    enabled: bool


class SynthesizeIn(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    mode: str = "typed"  # "typed" | "starlark"


class CheckScriptIn(BaseModel):
    script: str = Field(..., min_length=1, max_length=20000)
    trigger: dict | None = None


def _validate_def_or_400(definition: dict) -> None:
    try:
        validate_definition(definition)
    except CapabilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Admin-only, like every write route below and the whole /automations UI page
# (admin in +layout nav + ADMIN_ROUTES). A rule DEFINITION names every entity it
# touches — including ones hidden from a narrow-scoped login — so returning it to
# a non-admin would leak house structure the view boundary otherwise withholds.
# The automations builder is the only caller and is admin-only; gating (vs. the
# heavier per-entity definition filter) matches the product intent and keeps the
# router uniformly admin.
@router.get("")
async def list_automations(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    rows = await request.app.state.pool.fetch(f"SELECT {_COLS} FROM automations ORDER BY id")  # noqa: S608
    return [dict(r) for r in rows]


@router.get("/runs")
async def list_runs(request: Request, limit: int = 100, automation_id: int | None = None,
                    _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Recent automation runs (append-only log), newest first — the full history,
    not just each rule's LAST run. Optionally filter to one rule via automation_id.
    Admin-only for the same reason as list_automations (a run row carries the rule
    name + entity references)."""
    lim = max(1, min(limit, 500))
    pool = request.app.state.pool
    if automation_id is not None:
        rows = await pool.fetch(
            "SELECT id, automation_id, name, outcome, detail, fired_at FROM automation_runs "
            "WHERE automation_id = $1 ORDER BY fired_at DESC LIMIT $2", automation_id, lim)
    else:
        rows = await pool.fetch(
            "SELECT id, automation_id, name, outcome, detail, fired_at FROM automation_runs "
            "ORDER BY fired_at DESC LIMIT $1", lim)
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_automation(body: AutomationIn, request: Request,
                            _admin: AuthUser = Depends(require_admin)) -> dict:
    _validate_def_or_400(body.definition)
    # Pass the dict directly: the pool's jsonb codec (encoder=json.dumps)
    # serialises it. Calling json.dumps here too would double-encode it.
    row = await request.app.state.pool.fetchrow(
        f"INSERT INTO automations (name, enabled, definition) VALUES ($1, $2, $3::jsonb) "  # noqa: S608
        f"RETURNING {_COLS}",
        body.name, body.enabled, body.definition,
    )
    return dict(row)


@router.put("/{automation_id}")
async def update_automation(automation_id: int, body: AutomationIn, request: Request,
                            _admin: AuthUser = Depends(require_admin)) -> dict:
    _validate_def_or_400(body.definition)
    row = await request.app.state.pool.fetchrow(
        f"UPDATE automations SET name = $2, enabled = $3, definition = $4::jsonb, "  # noqa: S608
        f"       updated_at = now(), last_error = NULL "
        f"WHERE id = $1 RETURNING {_COLS}",
        automation_id, body.name, body.enabled, body.definition,
    )
    if row is None:
        raise HTTPException(404, "automation not found")
    return dict(row)


# ── domain ops shared with the assistant's tools ─────────────────────────────
# The assistant drives these through the same functions rather than reimplementing
# them, so the breaker-reset semantics and the disabled-rule guard cannot drift
# between the button and the chat. They raise CapabilityError; the routes below
# translate it to HTTP, the tool layer hands it to the model.


async def set_enabled(pool, automation_id: int, enabled: bool) -> dict:
    """Toggle enabled. Re-enabling also clears a tripped breaker's last_error."""
    row = await pool.fetchrow(
        f"UPDATE automations SET enabled = $2, updated_at = now(), "  # noqa: S608
        f"       last_error = CASE WHEN $2 THEN NULL ELSE last_error END "
        f"WHERE id = $1 RETURNING {_COLS}",
        automation_id, enabled,
    )
    if row is None:
        raise CapabilityError("automation not found")
    return dict(row)


async def run_now(pool, bus, automation_id: int) -> dict:
    """Force-run a rule's actions, skipping trigger + conditions.

    The engine owns execution — we only publish the request, so the actions go out
    with the same delays/validation/single-flight as a real firing. This DRIVES REAL
    DEVICES (running the gate rule opens the gate).
    """
    row = await pool.fetchrow("SELECT id, enabled FROM automations WHERE id = $1", automation_id)
    if row is None:
        raise CapabilityError("automation not found")
    if not row["enabled"]:
        raise CapabilityError("the automation is disabled — enable it first")
    await bus.publish_raw(_RUN_SUBJECT, json.dumps({"id": automation_id}).encode())
    return {"ok": True}  # the route's published shape — callers add their own context


@router.patch("/{automation_id}")
async def set_automation_enabled(automation_id: int, body: EnabledPatch, request: Request,
                                 _admin: AuthUser = Depends(require_admin)) -> dict:
    """Toggle enabled. Re-enabling also clears a tripped breaker's last_error."""
    try:
        return await set_enabled(request.app.state.pool, automation_id, body.enabled)
    except CapabilityError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{automation_id}", status_code=204)
async def delete_automation(automation_id: int, request: Request,
                            _admin: AuthUser = Depends(require_admin)) -> None:
    result = await request.app.state.pool.execute(
        "DELETE FROM automations WHERE id = $1", automation_id
    )
    if result.endswith("0"):
        raise HTTPException(404, "automation not found")


@router.post("/synthesize")
async def synthesize_automation(body: SynthesizeIn, request: Request,
                                _admin: AuthUser = Depends(require_admin)) -> dict:
    """Draft an automation from a natural-language description — Claude synthesises
    it, the boundary validates it, but it is NOT saved. mode='typed' →
    Trigger→Condition→Action; mode='starlark' → a Layer-2 script, sandbox-checked
    (compile + dry-run + command validation) before return. The editor loads the
    result for review/tweak before the user saves it through the normal
    (re-validated) create path. AI proposes, the boundary disposes."""
    client = await resolve_assistant_client(request.app.state)
    if client is None:
        raise HTTPException(503, "assistant is not configured — add an Anthropic key in System → Settings")
    try:
        definition = await synthesize_definition(
            client, request.app.state.pool, body.description,
            bus=request.app.state.bus, mode=body.mode,
        )
    except CapabilityError as exc:
        raise HTTPException(422, f"AI je predložio nevažeće pravilo: {exc}") from exc
    except Exception as exc:  # malformed model output / timeout / SDK error → clean 502, not a raw 500
        log.exception("automation synthesis failed (mode=%s)", body.mode)
        raise HTTPException(502, "AI generation failed — try again or rephrase the description.") from exc
    return {"definition": definition}


@router.post("/{automation_id}/explain")
async def explain_automation_endpoint(automation_id: int, request: Request,
                                      _admin: AuthUser = Depends(require_admin)) -> dict:
    """Plain-language explanation of a rule + a 'would it fire now / why not'
    diagnosis grounded in the live state of the entities it observes."""
    client = await resolve_assistant_client(request.app.state)
    if client is None:
        raise HTTPException(503, "assistant is not configured — add an Anthropic key in System → Settings")
    try:
        explanation = await explain_automation(client, request.app.state.pool, automation_id)
    except CapabilityError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"explanation": explanation}


@router.post("/{automation_id}/run")
async def run_automation_now(automation_id: int, request: Request,
                             _admin: AuthUser = Depends(require_admin)) -> dict:
    """Force-run a rule's actions now (skip trigger + conditions), like HA's 'Run'.
    NOTE: this drives real devices (running the gate rule opens the gate)."""
    try:
        return await run_now(request.app.state.pool, request.app.state.bus, automation_id)
    except CapabilityError as exc:
        # "not found" vs "disabled" — 404 and 409 as before.
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc


@router.post("/check-script")
async def check_script(body: CheckScriptIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Compile + dry-run a Starlark script via the automation service (which owns
    the sandbox) and return {ok, error?, commands?}. The editor's Check button
    calls this so a script is validated — syntax AND the commands it would emit,
    each checked against the capability model — BEFORE it's saved or ever fires.
    Gives Layer 2 the same write-time boundary a typed rule already gets."""
    payload = json.dumps({"script": body.script, "trigger": body.trigger}).encode()
    try:
        reply = await request.app.state.bus.nc.request(_CHECK_SUBJECT, payload, timeout=5.0)
    except Exception as exc:
        raise HTTPException(503, "automation servis ne odgovara") from exc
    return json.loads(reply.data)
