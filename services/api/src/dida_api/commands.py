from __future__ import annotations

from dida_core import prepare_command
from fastapi import HTTPException

from dida_api import opus, radio_tuner
from dida_api.auth import AuthUser
from dida_api.permissions import require_control
from dida_api.visibility import can_view_entity


async def require_visible_control(pool, user: AuthUser, entity_id: str, capability: str) -> None:
    if not await can_view_entity(pool, user, entity_id):
        raise HTTPException(404, "entity not found")
    await require_control(pool, user, entity_id, capability)


async def dispatch_command(pool, bus, user: AuthUser | None, entity_id: str,
                           capability: str, command: str, args: dict | None, *, source: str) -> None:
    if user is not None:
        await require_visible_control(pool, user, entity_id, capability)
    target = None
    if entity_id == radio_tuner.TUNER:
        target = await radio_tuner.resolve_target(pool)
        if target is None:
            raise HTTPException(409, "no radio player is configured")
        if user is not None:
            await require_visible_control(pool, user, target, "media_transport")
    cmd = await prepare_command(pool, entity_id, capability, command, args, source=source)
    if target is not None:
        try:
            await radio_tuner.relay_command(pool, bus, cmd, target)
        except opus.OpusUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
    else:
        await bus.publish_command(cmd)
