from __future__ import annotations

import asyncio
import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from dida_netmgr import __main__ as startup
from dida_netmgr import manager as runtime
from dida_netmgr.manager import NetManager


class Manager:
    def __init__(self):
        self.started = asyncio.Event()
        self.attached = asyncio.Event()
        self.stop = None
        self.bus = None

    def set_journal_bus(self, bus):
        self.bus = bus
        if bus is not None:
            self.attached.set()

    async def run(self, stop):
        self.stop = stop
        self.started.set()
        await stop.wait()


async def test_network_startup_does_not_wait_for_a_real_refused_nats_connection(monkeypatch):
    pool = SimpleNamespace(close=AsyncMock())
    manager = Manager()
    monkeypatch.setattr(startup, "pg_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(startup, "NetManager", lambda _: manager)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    monkeypatch.delenv("DIDA_NATS_PASSWORD_FILE", raising=False)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setenv("DIDA_NATS_URL", f"nats://127.0.0.1:{reserved.getsockname()[1]}")
        task = asyncio.create_task(startup.main())
        try:
            await asyncio.wait_for(manager.started.wait(), timeout=1)
            await asyncio.sleep(0.03)
            assert manager.bus is None
            pool.close.assert_not_called()
        finally:
            if manager.stop:
                manager.stop.set()
            else:
                task.cancel()
            await asyncio.wait_for(task, timeout=1)
    pool.close.assert_awaited_once()


class JournalBus:
    def __init__(self, connected, *, fail=False):
        self.connected = connected
        self.fail = fail
        self.nc = SimpleNamespace(is_closed=False)
        self.closed = False
        self.published = []

    async def connect(self):
        if self.fail:
            raise OSError("journal unavailable")
        await self.connected.wait()

    async def close(self):
        self.closed = True

    async def publish_journal(self, event):
        self.published.append(event)


async def test_later_bus_recovery_attaches_the_journal_without_restarting_the_manager(monkeypatch):
    connected = asyncio.Event()
    manager = Manager()
    bus = JournalBus(connected)
    monkeypatch.setenv("DIDA_NATS_URL", "nats://test:4222")
    monkeypatch.setattr(startup, "Bus", lambda *args, **kwargs: bus)
    attach = Mock()
    monkeypatch.setattr(startup, "attach_log_bus", attach)
    stop = asyncio.Event()
    task = asyncio.create_task(startup.journal_connection(manager, stop))
    try:
        await asyncio.sleep(0)
        assert manager.bus is None
        connected.set()
        await asyncio.wait_for(manager.attached.wait(), timeout=1)
        attach.assert_called_once_with(bus, "netmgr")
        network = NetManager(None, manager.bus)
        network._last_status = {}
        await network._journal_status_change({"20": {"address": "192.168.20.11"}})
        assert [event.kind for event in bus.published] == ["vlan_up"]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert manager.bus is None and bus.closed


async def test_a_failed_journal_setup_retries_and_closes_each_attempt(monkeypatch):
    connected = asyncio.Event()
    connected.set()
    first, second = JournalBus(connected, fail=True), JournalBus(connected)
    buses = iter((first, second))
    manager = Manager()
    monkeypatch.setenv("DIDA_NATS_URL", "nats://test:4222")
    monkeypatch.setattr(startup, "Bus", lambda *args, **kwargs: next(buses))
    monkeypatch.setattr(startup, "attach_log_bus", Mock())
    stop = asyncio.Event()
    task = asyncio.create_task(startup.journal_connection(manager, stop))
    try:
        await asyncio.wait_for(manager.attached.wait(), timeout=3)
        assert first.closed
        assert manager.bus is second
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert second.closed


async def test_a_permanently_closed_bus_releases_its_log_handler_before_retry(monkeypatch):
    connected = asyncio.Event()
    connected.set()
    first, second = JournalBus(connected), JournalBus(connected)
    first.nc.is_closed = True
    buses = iter((first, second))
    handlers = []
    logger = logging.getLogger()
    attached = asyncio.Event()

    def attach(bus, service):
        handler = logging.NullHandler()
        handler.close = Mock()
        handlers.append(handler)
        logger.addHandler(handler)
        if bus is second:
            attached.set()
        return handler

    manager = Manager()
    monkeypatch.setenv("DIDA_NATS_URL", "nats://test:4222")
    monkeypatch.setattr(startup, "Bus", lambda *args, **kwargs: next(buses))
    monkeypatch.setattr(startup, "attach_log_bus", attach)
    stop = asyncio.Event()
    task = asyncio.create_task(startup.journal_connection(manager, stop))
    try:
        await asyncio.wait_for(attached.wait(), timeout=3)
        assert manager.bus is second
        assert first.closed
        handlers[0].close.assert_called_once()
        assert handlers[0] not in logger.handlers
        assert handlers[1] in logger.handlers
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert second.closed
    handlers[1].close.assert_called_once()
    assert handlers[1] not in logger.handlers


async def test_health_is_not_touched_until_reconciliation_completes(monkeypatch):
    health = Mock()
    started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(runtime, "HealthMarker", lambda *args: health)
    monkeypatch.setattr(runtime, "parent_nic", AsyncMock(return_value="eth0"))
    monkeypatch.setattr(runtime, "FORWARDS", [])
    network = NetManager(None)

    async def reconcile():
        started.set()
        await release.wait()
        stop.set()

    monkeypatch.setattr(network, "reconcile", reconcile)
    task = asyncio.create_task(network.run(stop))
    await asyncio.wait_for(started.wait(), timeout=1)
    health.touch.assert_not_called()
    release.set()
    await task
    health.touch.assert_called_once()


async def test_failed_reconciliation_does_not_report_healthy(monkeypatch):
    health, stop = Mock(), asyncio.Event()
    monkeypatch.setattr(runtime, "HealthMarker", lambda *args: health)
    monkeypatch.setattr(runtime, "parent_nic", AsyncMock(return_value="eth0"))
    monkeypatch.setattr(runtime, "FORWARDS", [])
    network = NetManager(None)

    async def reconcile():
        stop.set()
        raise RuntimeError("Docker reconcile failed")

    monkeypatch.setattr(network, "reconcile", reconcile)
    await network.run(stop)
    health.touch.assert_not_called()
