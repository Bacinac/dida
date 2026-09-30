"""Regression tests for the shared adapter runtime (dida_core.adapter_runner).

`run_adapter()` is the fault-isolation backbone every adapter process boots
through: connect the bus, serve a StatusReporter (so the UI badge always exists),
start the health touch-loop, route inbound commands to `handle_command`, and shut
down cleanly on signal. A `start()` that CRASHES instead fails loud — the
exception propagates so the container dies visibly rather than hanging. These
tests drive that lifecycle deterministically against fake collaborators (bus /
status / health); only the collaborators are stubbed, the orchestration is real.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_adapter_runner.py"
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import pytest
from dida_core import Command, CommandRejected, adapter_runner


def _patch_collaborators(monkeypatch):
    """Swap Bus / StatusReporter / HealthMarker / Broker for recording fakes and
    return a `created` dict the test reads back the constructed instances from."""
    created: dict[str, object] = {}

    class FakeBus:
        def __init__(self, url, *, name, user):
            created["bus"] = self
            self.url = url
            self.name = name
            self.user = user
            self.connected = False
            self.closed = False
            self.command_handler = None

        async def connect(self):
            self.connected = True

        def speaks_for(self, adapter):
            self.speaks = adapter

        async def subscribe_commands(self, handler, namespace):
            self.command_handler = handler
            self.command_namespace = namespace

        async def close(self):
            self.closed = True

    class FakeStatus:
        def __init__(self, name):
            created["status"] = self
            self.name = name
            self.served_on = None

        async def serve(self, bus):
            self.served_on = bus

    class FakeHealth:
        def __init__(self, product, service, *, path=None):
            created["health"] = self
            self.service = service
            self.ran = False

        async def run_loop(self, interval=5.0):
            self.ran = True
            await asyncio.Event().wait()  # run forever until cancelled at shutdown

    monkeypatch.setattr(adapter_runner, "Bus", FakeBus)
    monkeypatch.setattr(adapter_runner, "StatusReporter", FakeStatus)
    monkeypatch.setattr(adapter_runner, "HealthMarker", FakeHealth)

    class FakeBroker:
        def __init__(self, bus, name):
            self.bus, self.adapter = bus, name
            created["broker"] = self

    monkeypatch.setattr(adapter_runner, "Broker", FakeBroker)
    return created


class LongAdapter:
    """A well-behaved adapter whose start() loops forever (esphome/shelly/astro
    shape) — it only unwinds when the runner cancels it at shutdown."""

    name = "loopy"

    def __init__(self):
        self.commands: list = []
        self.stopped = False
        self.start_cancelled = False
        self.started = asyncio.Event()

    async def start(self, bus):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise

    async def handle_command(self, command):
        self.commands.append(command)

    async def stop(self):
        self.stopped = True


class CrashAdapter:
    """Its start() raises immediately — the runner must fail loud, not hang."""

    name = "boomer"

    def __init__(self):
        self.stopped = False

    async def start(self, bus):
        raise RuntimeError("start blew up")

    async def handle_command(self, command):
        pass

    async def stop(self):
        self.stopped = True


async def _run_until_signal(adapter, sig=signal.SIGINT):
    """Boot the adapter, wait until its start() is actually running (so the loop's
    signal handlers are installed), then deliver `sig` and await clean shutdown."""
    run_task = asyncio.create_task(adapter_runner.run_adapter(adapter))
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    os.kill(os.getpid(), sig)
    await asyncio.wait_for(run_task, timeout=5)


async def test_run_adapter_wires_collaborators_and_shuts_down_on_signal(monkeypatch):
    monkeypatch.setenv("DIDA_NATS_URL", "nats://nats:4222")
    created = _patch_collaborators(monkeypatch)
    adapter = LongAdapter()

    await _run_until_signal(adapter)

    bus = created["bus"]
    assert bus.url == "nats://nats:4222", "runner reads DIDA_NATS_URL for the bus URL"
    assert bus.name == "dida-adapter-loopy", "bus client name is namespaced per adapter"
    assert bus.user == "loopy", "an adapter logs into the bus as itself"
    assert bus.connected is True, "the bus is connected before anything else"
    assert bus.speaks == "loopy", "the adapter heartbeats from boot, not from its first publish"
    assert created["status"].name == "loopy", "a StatusReporter is created for every adapter"
    assert created["status"].served_on is bus, "the status reporter is served on the bus"
    assert adapter.status is created["status"], "runner injects the reporter as adapter.status"
    assert adapter.broker is created["broker"], "runner injects the broker as adapter.broker"
    assert (created["broker"].bus, created["broker"].adapter) == (bus, "loopy"), \
        "the broker asks on the adapter's own bus, in its own name"
    assert created["health"].service == "adapter-loopy", "health marker names the adapter"
    assert created["health"].ran is True, "the health touch-loop was started"
    assert bus.command_handler is not None, "a command dispatcher is subscribed (see the dispatch test)"
    assert bus.command_namespace == "loopy", "it hears the commands for its own namespace only"
    # SIGINT flips the internal stop event -> graceful teardown.
    assert adapter.start_cancelled is True, "a forever-looping start() is cancelled at shutdown"
    assert adapter.stopped is True, "adapter.stop() is awaited during graceful shutdown"
    assert bus.closed is True, "the bus is closed last on shutdown"


class Cmd:
    """A minimal command stand-in — the dispatcher keys per-entity off `entity_id`."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id


async def test_run_adapter_dispatches_commands_to_the_adapter(monkeypatch):
    monkeypatch.setenv("DIDA_NATS_URL", "nats://localhost:4222")
    created = _patch_collaborators(monkeypatch)
    adapter = LongAdapter()

    await _run_until_signal(adapter)

    # The runner subscribes a dispatcher (per-entity ordered, cross-entity concurrent)
    # that spawns handle_command — so a command reaches the adapter, and two commands
    # to the SAME entity stay in order.
    handler = created["bus"].command_handler
    a, b = Cmd("mqtt:light"), Cmd("mqtt:light")
    await handler(a)
    await handler(b)
    for _ in range(50):
        if len(adapter.commands) == 2:
            break
        await asyncio.sleep(0.01)
    assert adapter.commands == [a, b], "commands dispatch into the adapter, in order per entity"


async def test_a_rejected_command_reaches_the_journal(monkeypatch):
    monkeypatch.setenv("DIDA_NATS_URL", "nats://localhost:4222")
    created = _patch_collaborators(monkeypatch)
    journal: list[tuple] = []

    async def record(bus, kind, **fields):
        journal.append((kind, fields))

    monkeypatch.setattr(adapter_runner, "emit_journal", record)

    class RefusingAdapter(LongAdapter):
        name = "refuser"

        async def handle_command(self, command):
            raise CommandRejected("device not connected")

    adapter = RefusingAdapter()
    await _run_until_signal(adapter)

    await created["bus"].command_handler(
        Command(entity_id="refuser:plug", capability="on_off", command="turn_on", ts_ns=1))
    for _ in range(50):
        if journal:
            break
        await asyncio.sleep(0.01)
    assert journal == [("command_failed", {
        "entity_id": "refuser:plug", "source": "adapter:refuser", "severity": "error",
        "message": "device not connected",
        "data": {"capability": "on_off", "command": "turn_on"},
    })]


async def test_run_adapter_start_crash_fails_loud(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="dida.adapter")
    monkeypatch.setenv("DIDA_NATS_URL", "nats://localhost:4222")
    created = _patch_collaborators(monkeypatch)
    adapter = CrashAdapter()

    # A start() that crashes must propagate (fail loud) instead of hanging until an
    # external SIGTERM — the container then dies visibly.
    with pytest.raises(RuntimeError, match="start blew up"):
        await asyncio.wait_for(adapter_runner.run_adapter(adapter), timeout=5)

    # Everything up to start() was still wired, and the crash was logged loud.
    assert created["bus"].connected is True, "the bus connected before start() was attempted"
    assert created["status"].served_on is created["bus"], "status was served before the crash"
    assert adapter.status is created["status"], "status was injected before the crash"
    assert created["bus"].command_handler is not None, "commands were subscribed before the crash"
    assert any("crashed" in r.getMessage() for r in caplog.records), \
        "the start() crash is logged at ERROR, never swallowed"


async def test_run_adapter_stop_error_does_not_block_bus_close(monkeypatch):
    monkeypatch.setenv("DIDA_NATS_URL", "nats://localhost:4222")
    created = _patch_collaborators(monkeypatch)

    class StopRaisesAdapter(LongAdapter):
        name = "grumpy"

        async def stop(self):
            raise RuntimeError("teardown failed")

    adapter = StopRaisesAdapter()
    await _run_until_signal(adapter)

    assert created["bus"].closed is True, \
        "a raising adapter.stop() is suppressed so the bus still closes (fault isolation)"
