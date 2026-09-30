"""Adapter protocol — the contract every protocol integration implements.

An adapter is the ONLY place that knows a concrete protocol (MQTT, Z-Wave,
Matter, Modbus…). It runs as its own process/container, so its failure is
isolated: if it hangs, leaks, or crashes, the engine and every other adapter
keep running. The container runtime restarts it.

Each adapter is its own package, started by its own `__main__`. Adding a new
protocol = shipping a new package: none of its CODE lives in core, and nothing
here imports it.

Two declarations do land in core, both deliberately:

  * its config-form schema, in `adapter_config.ADAPTER_CONFIG` — because the API
    renders those forms and CANNOT import adapter packages (separate containers;
    the api image genuinely has no dida_adapter_* installed). Announcing the
    schema over the bus instead would leave a not-yet-enabled adapter — every
    adapter is behind a compose profile — with no process to announce from, and
    so no form to enable it with;
  * a genuinely new capability, in `capabilities.py` — which is the canonical
    model working as intended, not an escape from it.

So "core is never touched" is the shape of the CODE, not a literal invariant.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dida_core.bus import Bus
from dida_core.events import Command


class CommandRejected(Exception):
    """A command this adapter cannot carry out: unknown device, bad arguments, the
    device refused or did not answer. Raised out of handle_command, it becomes a
    command_fail journal entry against the entity (AdapterRunner), so the timeline
    shows the refusal next to the ask instead of a log line nobody reads."""


@runtime_checkable
class Adapter(Protocol):
    """Lifecycle an adapter must implement.

    The runner wires `bus` in, calls `start()`, routes inbound `Command`s to
    `handle_command()`, and calls `stop()` on shutdown. Everything the adapter
    learns from its devices it pushes back via `bus.publish_state(...)`.
    """

    #: Stable adapter name; also the entity_id namespace it owns ("mqtt").
    name: str

    async def start(self, bus: Bus) -> None:
        """Connect to the native protocol and begin publishing state updates."""
        ...

    async def handle_command(self, command: Command) -> None:
        """Translate a validated capability command into native protocol."""
        ...

    async def stop(self) -> None:
        """Disconnect cleanly."""
        ...
