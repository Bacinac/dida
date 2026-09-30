"""The one way a command is made, whoever sends it.

The api, the assistant, a scene, a rule, a Starlark script and the heating
controller all put commands on the bus. Each used to validate its own way — the
assistant checked only the command's name, rules never saw a device's own limits
— so the same garbage was refused on one path and delivered on another. Every
sender builds its command here, and gets the same answer.
"""

from __future__ import annotations

import time

from dida_core.capabilities import CapabilityKind, validate_command_args
from dida_core.events import Command

_OPEN_CAPS = (CapabilityKind.NUMBER.value, CapabilityKind.ENUM.value)


async def prepare_command(pool, entity_id: str, capability: str, command: str,
                          args: dict | None = None, *, source: str) -> Command:
    """Validate and build a command; raises `CapabilityError` on anything an adapter
    must not receive. `number` and `enum` are open at the spec level, so their value
    is checked against the limits the device itself published."""
    args = dict(args or {})
    options = None
    if capability in _OPEN_CAPS and command.startswith("set_"):
        options = await pool.fetchval(
            "SELECT value #>> '{}' FROM current_state WHERE entity_id = $1 AND capability = $2",
            entity_id, f"{capability}_options")
    validate_command_args(capability, command, args, options)
    return Command(entity_id=entity_id, capability=capability, command=command,
                   ts_ns=time.time_ns(), args=args, source=source)
