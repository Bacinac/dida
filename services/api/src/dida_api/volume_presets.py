"""Volume presets — a small global list of named loudness levels (Quiet / Normal /
Loud …) shown as one-tap buttons on each AVR zone's volume row. Stored as JSON in
app_settings so it's shared across zones and editable by an admin from the UI. The
levels also drive the green→red volume tint in the media UI."""

from __future__ import annotations

import json

from dida_core import set_app_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.common import get_setting

router = APIRouter(tags=["media"])

_KEY = "volume_presets"
# Labels are the ENGLISH source (house convention): the UI localises them through
# the translations dictionary (Settings → Prijevodi / autofill), so a Croatian
# session shows "Tiho/Normalno/Glasno" while EN shows the base below.
_DEFAULT = [
    {"label": "Quiet", "value": 25},
    {"label": "Normal", "value": 40},
    {"label": "Loud", "value": 60},
]


class Preset(BaseModel):
    label: str = Field(..., min_length=1, max_length=24)
    value: int = Field(..., ge=0, le=100)


class PresetsBody(BaseModel):
    presets: list[Preset] = Field(..., max_length=8)


async def _load(pool) -> list[dict]:
    raw = await get_setting(pool, _KEY)
    if not raw:
        return _DEFAULT
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else _DEFAULT
    except (ValueError, TypeError):
        return _DEFAULT


@router.get("/media/volume-presets")
async def list_presets(request: Request, _user: AuthUser = Depends(current_user)) -> list[dict]:
    return await _load(request.app.state.pool)


@router.put("/media/volume-presets")
async def set_presets(
    body: PresetsBody, request: Request, _admin: AuthUser = Depends(require_admin)
) -> list[dict]:
    if not body.presets:
        raise HTTPException(400, "at least one preset is required")
    # Store ascending by level so the buttons read low→high (green→red) left→right.
    presets = sorted((p.model_dump() for p in body.presets), key=lambda p: p["value"])
    await set_app_setting(request.app.state.pool, _KEY, json.dumps(presets))
    return presets
