"""Standard adapter entry point — collapses every adapter's ``__main__.py``.

An adapter package should ship protocol logic and nothing else. This runner owns
the identical boot/shutdown lifecycle every adapter used to hand-roll:

- connect the bus,
- serve a ``StatusReporter`` for EVERY adapter (so the UI badge always exists —
  a mis-typed host/credential fails loud instead of a silent "nothing appears"),
- run the health-touch loop,
- hand it its broker (``adapter.broker``) — the only way an adapter reaches config,
  secrets or the house's records,
- route inbound commands to ``handle_command``,
- graceful, signal-driven shutdown.

``start()`` runs as a supervised task, so an adapter whose ``start()`` loops
forever (esphome, shelly, astro…) still shuts down cleanly — the old
``await adapter.start(bus)`` skipped the teardown for those.

The runner injects the status reporter as ``adapter.status`` (public — the
private ``_status`` name is already taken by esphome for its per-node dict);
adapters report via ``self.status.{idle,connecting,ok,error}(detail)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from home_core.health import HealthMarker
from home_core.tasks import cancel_all_tasks, spawn

from dida_core.adapter import CommandRejected
from dida_core.broker import Broker
from dida_core.bus import Bus
from dida_core.health import StatusReporter
from dida_core.journal import emit_journal
from dida_core.logbus import attach_log_bus
from dida_core.reachability import reassert_loop

log = logging.getLogger("dida.adapter")


async def run_adapter(adapter) -> None:
    """Boot ``adapter`` and run until SIGTERM/SIGINT, then shut it down cleanly."""
    name = adapter.name
    bus = Bus(os.environ["DIDA_NATS_URL"], name=f"dida-adapter-{name}", user=name)
    await bus.connect()
    # Alive from boot, not from its first publish: an adapter whose devices are all
    # quiet is still the one that would speak for them.
    bus.speaks_for(name)

    # Every adapter's logs join the durable stream from here — one site for the
    # whole fleet, the same way the status journal hangs off StatusReporter.
    attach_log_bus(bus, f"adapter:{name}")
    status = StatusReporter(name)
    await status.serve(bus)
    adapter.status = status  # adapters report via self.status.{idle,ok,error,...}
    adapter.broker = Broker(bus, name)  # config, secrets and house facts — no database

    health = HealthMarker("dida", f"adapter-{name}")

    # Command dispatch: per-entity ORDERED, cross-entity CONCURRENT. NATS delivers
    # this subscription's callbacks serially, so an inline `await handle_command`
    # lets a command to a slow/dead device head-of-line-block commands to healthy
    # ones (a dead Shelly's 8 s timeout stalls every other Shelly). We spawn each
    # command instead — but serialize per entity_id behind a FIFO lock so two
    # commands to the SAME entity (a slider drag: set 50 then 60) still apply in
    # order and never race to the wrong final value.
    cmd_locks: dict[str, asyncio.Lock] = {}

    async def _dispatch(command) -> None:
        lock = cmd_locks.setdefault(command.entity_id, asyncio.Lock())

        async def _run() -> None:
            async with lock:
                try:
                    await adapter.handle_command(command)
                except CommandRejected as exc:
                    log.warning("command %s %s/%s rejected: %s", command.entity_id,
                                command.capability, command.command, exc)
                    await emit_journal(
                        bus, "command_failed", entity_id=command.entity_id,
                        source=f"adapter:{name}", severity="error", message=str(exc),
                        data={"capability": command.capability, "command": command.command},
                    )
                except Exception as exc:
                    # One choke point for the whole fleet: every adapter's commands
                    # come through here, so a failed one is recorded against the
                    # ENTITY. The command audit says a command was issued; without
                    # this the timeline shows the ask and never the refusal, which
                    # reads as "DIDA sent it, the device just ignored it".
                    await emit_journal(
                        bus, "command_failed", entity_id=command.entity_id,
                        source=f"adapter:{name}", severity="error", message=str(exc),
                        data={"capability": command.capability, "command": command.command},
                    )
                    raise  # spawn() still logs it — the journal adds, never replaces

        spawn(_run(), log=log, name=f"command {command.entity_id}")

    await bus.subscribe_commands(_dispatch, name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Health loop through spawn() so its unexpected death is LOGGED, not silent
    # (it had no done-callback before). start() keeps its own callback below —
    # it must both log AND trigger shutdown, which spawn() doesn't do.
    spawn(health.run_loop(), log=log, name=f"health {name}")
    # …and re-state every reachability verdict on a timer. An adapter publishes its
    # verdict once, on the change; if nobody was listening at that instant — the
    # engine still starting after a deploy — the device stays wrong until something
    # flaps. See dida_core.reachability.
    spawn(reassert_loop(bus), log=log, name=f"reachability re-assert {name}")
    start_task = asyncio.create_task(adapter.start(bus))

    def _on_start_done(t: asyncio.Task) -> None:
        # A start() that crashes early must fail loud (and trigger shutdown),
        # not hang the process until an external SIGTERM.
        if not t.cancelled() and t.exception() is not None:
            log.error("adapter %s start() crashed", name, exc_info=t.exception())
            stop.set()

    start_task.add_done_callback(_on_start_done)

    await stop.wait()
    log.info("adapter %s shutting down", name)
    start_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await start_task
    # Cancel every spawned background task — the health loop, the adapter's own
    # poll/discovery/config loops, any in-flight command — BEFORE stop() closes the
    # pool/session they use, so shutdown is clean instead of a spray of
    # used-after-close errors from loops still running under a torn-down resource.
    await cancel_all_tasks()
    with contextlib.suppress(Exception):
        await adapter.stop()
    await bus.close()
