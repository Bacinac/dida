"""A ClickHouse schema that will not apply must stop looking like a healthy engine.

The old behaviour: `apply_ch_migrations` raises, the writer logs
"schema setup failed; will retry", returns False, and does that forever. The engine
kept touching its health marker, so the container stayed green, the deploy gate saw
nothing, and every historical value was discarded in silence. That is the exact
shape of failure the project's rules forbid — a silent degradation to a worse path.

What is NOT wanted is equally important: a ClickHouse that is merely DOWN must still
buffer and retry, because that is transient and the house must not report a fault
over it. The two are separated by where the failure happens, and both are pinned
here.
"""

from __future__ import annotations

import logging
import types
from contextlib import nullcontext

import pytest
from dida_engine.history import SCHEMA_FAIL_LIMIT, SCHEMA_FAIL_SECONDS, HistoryWriter


class _Client:
    async def close(self):
        pass


@pytest.fixture
def writer(monkeypatch):
    w = HistoryWriter(pool=None, access=lambda _: nullcontext())
    monkeypatch.setattr("dida_engine.history.ch_client", _ok_client)
    return w


async def _ok_client():
    return _Client()


def _fail_schema(monkeypatch, message="DB::Exception: syntax error"):
    async def _boom(_client):
        raise RuntimeError(message)
    monkeypatch.setattr("dida_engine.history.apply_ch_migrations", _boom)


def _ok_schema(monkeypatch):
    async def _fine(_client):
        return None
    async def _ret(_client, _pool):
        return None
    async def _day(_client, _tz):
        return None
    monkeypatch.setattr("dida_engine.history.apply_ch_migrations", _fine)
    monkeypatch.setattr("dida_engine.history.apply_house_day", _day)
    monkeypatch.setattr("dida_engine.history.apply_retention", _ret)


async def test_a_healthy_schema_is_not_a_fault(writer, monkeypatch):
    _ok_schema(monkeypatch)
    assert await writer._ensure() is True
    assert writer.schema_broken is False
    assert writer.stats()["schema_failures"] == 0


async def test_one_schema_failure_is_still_a_retry_not_a_fault(writer, monkeypatch):
    """A ClickHouse that accepts connections before it is ready must not trip this."""
    _fail_schema(monkeypatch)
    assert await writer._ensure() is False
    assert writer.schema_broken is False


def _age(writer, seconds):
    """Pretend the failures started `seconds` ago."""
    writer._schema_first_failure -= seconds


async def test_repeated_schema_failure_becomes_a_fault(writer, monkeypatch):
    _fail_schema(monkeypatch)
    for _ in range(SCHEMA_FAIL_LIMIT):
        await writer._ensure()
    _age(writer, SCHEMA_FAIL_SECONDS)
    assert writer.schema_broken is True, "a schema that never applies must be reported"
    assert writer.stats()["schema_broken"] is True


async def test_the_count_alone_is_not_enough(writer, monkeypatch):
    """`_ensure` runs from the flush loop about once a second, so a count-only rule
    is spent in five seconds — and would roll back a deploy over a ClickHouse that is
    simply still starting."""
    _fail_schema(monkeypatch)
    for _ in range(SCHEMA_FAIL_LIMIT * 4):
        await writer._ensure()
    assert writer.schema_broken is False, "failures within the grace window are a wait"


async def test_time_alone_is_not_enough_either(writer, monkeypatch):
    """One failure two minutes ago, then nothing, is not a broken schema."""
    _fail_schema(monkeypatch)
    await writer._ensure()
    _age(writer, SCHEMA_FAIL_SECONDS * 2)
    assert writer.schema_broken is False


async def test_a_slow_clickhouse_that_comes_good_never_reports_a_fault(writer, monkeypatch):
    """The whole reason for the grace window, end to end."""
    _fail_schema(monkeypatch)
    for _ in range(SCHEMA_FAIL_LIMIT * 3):
        await writer._ensure()
        assert writer.schema_broken is False
    _ok_schema(monkeypatch)
    assert await writer._ensure() is True
    assert writer.schema_broken is False
    assert writer.stats()["schema_failing_for_s"] == 0


async def test_the_fault_says_history_is_being_lost(writer, monkeypatch, caplog):
    """The message has to name the consequence, not just the operation."""
    _fail_schema(monkeypatch)
    for _ in range(SCHEMA_FAIL_LIMIT):
        await writer._ensure()
    _age(writer, SCHEMA_FAIL_SECONDS)
    with caplog.at_level(logging.ERROR, logger="dida.engine.history"):
        await writer._ensure()
    assert "HISTORY IS NOT BEING WRITTEN" in caplog.text


async def test_a_schema_that_recovers_clears_the_fault(writer, monkeypatch):
    """Otherwise a slow start would leave the engine permanently unhealthy."""
    _fail_schema(monkeypatch)
    for _ in range(SCHEMA_FAIL_LIMIT):
        await writer._ensure()
    _age(writer, SCHEMA_FAIL_SECONDS)
    assert writer.schema_broken is True

    _ok_schema(monkeypatch)
    assert await writer._ensure() is True
    assert writer.schema_broken is False


async def test_clickhouse_merely_being_DOWN_is_not_a_fault(monkeypatch):
    """The transient case: connect fails, so the schema is never attempted. Buffering
    through an outage is correct and must not report the engine unhealthy."""
    async def _no_connect():
        raise OSError("connection refused")
    monkeypatch.setattr("dida_engine.history.ch_client", _no_connect)

    w = HistoryWriter(pool=None, access=lambda _: nullcontext())
    for _ in range(SCHEMA_FAIL_LIMIT * 3):
        assert await w._ensure() is False
    assert w._schema_first_failure is None, "a connect failure is not a schema failure"
    assert w.schema_broken is False, "an outage is not a schema fault"


class _Marker:
    def __init__(self) -> None:
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1


class _Relay:
    def __init__(self, stuck: bool = False) -> None:
        self._stuck = stuck

    def stuck(self) -> bool:
        return self._stuck


def _gate():
    from dida_engine.__main__ import _HealthGate

    gate = _HealthGate()
    gate.health = _Marker()
    return gate


def test_the_engine_withholds_health_when_the_schema_is_broken():
    """The marker is what the deploy gate and `docker ps` read; the whole fix is that
    it stops being touched."""
    gate = _gate()
    broken = types.SimpleNamespace(schema_broken=True)
    gate.judge(broken, _Relay())
    gate.judge(broken, _Relay())
    assert gate.health.touched == 0, "health.touch() must be gated on the schema being usable"
    gate.judge(types.SimpleNamespace(schema_broken=False), _Relay())
    assert gate.health.touched == 1


def test_the_engine_keeps_projecting_state_through_the_fault(caplog):
    """Losing graphs must not take the house down — the fault reports once, and
    judging returns to the loop instead of raising out of it."""
    gate = _gate()
    broken = types.SimpleNamespace(schema_broken=True)
    with caplog.at_level(logging.ERROR, logger="dida.engine"):
        for _ in range(3):
            assert gate.judge(broken, _Relay()) is None
    assert [r.levelno for r in caplog.records].count(logging.ERROR) == 1


def test_a_stuck_outbox_withholds_health_until_it_drains():
    gate = _gate()
    fine = types.SimpleNamespace(schema_broken=False)
    gate.judge(fine, _Relay(stuck=True))
    assert gate.health.touched == 0
    gate.judge(fine, _Relay(stuck=False))
    assert gate.health.touched == 1
