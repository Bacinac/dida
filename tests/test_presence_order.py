from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from dida_api import presence
from dida_api.presence_order import frame_timing, timestamp_ns
from dida_core import apply_migrations, jsonb_init, pg_pool
from fastapi import HTTPException

USER = "zzaudit_presence_order"
ENTITY = f"presence:{USER}"


class Bus:
    def __init__(self):
        self.calls = []
        self.nc = self
        self.fail = False

    async def publish_state(self, update):
        if self.fail:
            raise RuntimeError("bus unavailable")
        self.calls.append(update)

    async def flush(self):
        pass


@pytest.fixture
async def state():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "/w/db/migrations")
    await pool.execute("DELETE FROM users WHERE username = $1", USER)
    await pool.execute("DELETE FROM entities WHERE entity_id = $1", ENTITY)
    await pool.execute("INSERT INTO users (username, password_hash) VALUES ($1, 'unused')", USER)
    await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ($1, 'presence')", ENTITY)
    await pool.execute("INSERT INTO current_state (entity_id, capability, value, ts_ns) VALUES ($1, 'location', $2, 0)", ENTITY, "away")
    yield SimpleNamespace(pool=pool, bus=Bus())
    await pool.execute("DELETE FROM users WHERE username = $1", USER)
    await pool.execute("DELETE FROM entities WHERE entity_id = $1", ENTITY)
    await pool.close()


async def transition(state, event, observed, seq=None):
    return await presence.publish_transition(state, USER, event, "Audit Home", observed_ns=observed,
                                              sequence=seq, reporter="phone" if seq is not None else None)


async def test_delayed_enter_cannot_follow_an_ignored_leave_even_after_api_restart(state):
    observed = time.time_ns() - 10_000_000_000
    assert (await transition(state, "leave", observed + 1_000_000_000))["reason"] == "stale_leave"
    restarted = SimpleNamespace(pool=state.pool, bus=Bus())
    assert (await transition(restarted, "enter", observed))["reason"] == "stale_report"
    assert restarted.bus.calls == []


async def test_leave_uses_the_pending_enter_before_engine_projection(state):
    observed = time.time_ns() - 10_000_000_000
    assert (await transition(state, "enter", observed))["accepted"] is True
    assert (await transition(state, "leave", observed + 1_000_000_000))["accepted"] is True
    assert [update.value for update in state.bus.calls] == ["Audit Home", "away"]
    assert (await transition(state, "enter", observed))["reason"] == "stale_report"


async def test_newer_gps_fix_prevents_an_old_transition(state):
    observed = time.time_ns() - 10_000_000_000
    assert (await presence.publish_report(state, USER, 0, 0, accuracy=10, observed_ns=observed))["accepted"] is True
    assert (await transition(state, "enter", observed - 1))["reason"] == "stale_report"
    assert [update.capability for update in state.bus.calls] == ["location", "latitude", "longitude"]
    assert all(update.ts_ns == observed for update in state.bus.calls)


async def test_sequence_orders_edges_with_identical_capture_times_and_rejects_replays(state):
    observed = time.time_ns() - 10_000_000_000
    assert (await transition(state, "enter", observed, 1))["accepted"] is True
    assert (await transition(state, "leave", observed, 2))["accepted"] is True
    assert (await transition(state, "enter", observed, 1))["reason"] == "stale_report"
    assert state.bus.calls[1].ts_ns > state.bus.calls[0].ts_ns


async def test_concurrent_delivery_finishes_at_the_newest_observation(state):
    observed = time.time_ns() - 10_000_000_000
    await asyncio.gather(transition(state, "leave", observed + 1_000_000_000), transition(state, "enter", observed))
    location = await state.pool.fetchval(
        "SELECT location FROM presence_reports WHERE user_id = (SELECT id FROM users WHERE username = $1)", USER)
    assert location == "away"
    assert not any(update.value == "Audit Home" for update in state.bus.calls)


async def test_publication_failure_does_not_consume_the_observation(state):
    observed = time.time_ns() - 10_000_000_000
    state.bus.fail = True
    with pytest.raises(RuntimeError, match="bus unavailable"):
        await transition(state, "enter", observed)
    state.bus.fail = False
    assert (await transition(state, "enter", observed))["accepted"] is True


@pytest.mark.parametrize("accuracy", [-1, float("nan"), float("inf"), 5_000])
async def test_invalid_or_coarse_accuracy_never_publishes(state, accuracy):
    assert (await presence.publish_report(state, USER, 0, 0, accuracy=accuracy, observed_ns=time.time_ns()))["accepted"] is False
    assert state.bus.calls == []


@pytest.mark.parametrize("value", [None, True, 0, -1, "123", float("nan"), float("inf"), time.time() + 600, 1 << 4096])
def test_invalid_capture_timestamp_is_rejected(value):
    with pytest.raises(HTTPException):
        timestamp_ns(value)


def test_native_millisecond_time_and_sequence_are_preserved():
    observed = int(time.time() * 1000) - 1000
    assert frame_timing({"tst": observed // 1000, "tst_ms": observed, "seq": 7, "reporter": "phone"}) == {
        "observed_ns": observed * 1_000_000, "sequence": 7, "reporter": "phone"}
