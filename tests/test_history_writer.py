"""Regression tests for the engine HistoryWriter buffer (dida_engine.history) —
the ClickHouse firehose's degraded-path robustness the audit flagged as untested.

Pure buffer logic + immutable retry batches over a stub CH client; no infra
(the pool + real ClickHouse are never touched — value coercion, the drop-oldest
hard cap, and 'a failed insert loses nothing' are all in-memory).

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/engine:latest \
      -c "python -m pytest tests/test_history_writer.py"
"""
import asyncio

import pytest
from dida_engine import history
from dida_engine.history import (
    BUFFER_CAP,
    MIRRORED_ADAPTER,
    NON_HISTORIZED,
    HistoryWriter,
)


def make():
    return HistoryWriter(None)  # the pool is unused by the buffer paths under test


def test_value_coercion():
    hw = make()
    hw.enqueue("s:1", "temperature", "test", 21.5, 1_000_000_000)
    hw.enqueue("s:1", "on_off", "test", True, 1_000_000_000)
    hw.enqueue("s:1", "on_off", "test", False, 1_000_000_000)
    hw.enqueue("s:1", "name", "test", "hello", 1_000_000_000)
    rows = list(hw._buf)  # row = [ts, entity_id, capability, adapter, value_num, value_str]
    assert rows[0][4] == 21.5 and rows[0][5] is None, "float → value_num, no value_str"
    assert rows[1][4] == 1.0 and rows[1][5] is None, "True → value_num 1.0"
    assert rows[2][4] == 0.0, "False → value_num 0.0"
    assert rows[3][4] is None and rows[3][5] == "hello", "str → value_str, no value_num"


def test_non_historized_capability_skipped():
    hw = make()
    cap = next(iter(NON_HISTORIZED))
    hw.enqueue("s:1", cap, "test", "x", 1_000_000_000)
    assert len(hw._buf) == 0, "a NON_HISTORIZED (live-only) cap never enters the firehose"


def test_mirrored_peer_readings_are_not_archived_here():
    """A peer installation archives its own readings. Writing them here too would
    fork the other house's history into a second copy that drifts after any outage —
    and the two would disagree with no way to tell which is right."""
    hw = make()
    hw.enqueue("peer:cabin_iammeter_meter:power", "power", MIRRORED_ADAPTER, 224.0, 1_000_000_000)
    assert len(hw._buf) == 0
    hw.enqueue("iammeter:meter:power", "power", "iammeter", 224.0, 1_000_000_000)
    assert len(hw._buf) == 1, "our own adapters still write"


def test_drop_oldest_at_cap_never_ooms():
    hw = make()
    for i in range(BUFFER_CAP + 3):
        hw.enqueue("s:1", "temperature", "test", float(i), 1_000_000_000)
    assert len(hw._buf) == BUFFER_CAP, "buffer is hard-capped — never grows past BUFFER_CAP (never OOM)"
    assert hw._dropped == 3, "the 3 overflow rows were dropped"
    assert next(iter(hw._buf))[4] == 3.0, "drop-OLDEST: values 0,1,2 evicted, front is now 3"
    assert hw.stats()["dropped"] == 3 and hw.stats()["buffered"] == BUFFER_CAP, "stats() reflects the buffer"


class StubClient:
    """A CH client whose insert always fails."""

    def __init__(self):
        self.closed = False

    async def insert(self, table, rows, column_names, settings):
        raise RuntimeError("clickhouse down")

    async def close(self):
        self.closed = True


def ready(writer, client):
    writer._client = client
    writer._schema_ready = True
    writer._next_day_tz_check = float("inf")


async def test_flush_preserves_retry_on_insert_failure():
    hw = make()
    hw.enqueue("s:1", "temperature", "test", 1.0, 1_000_000_000)
    hw.enqueue("s:1", "temperature", "test", 2.0, 2_000_000_000)
    client = StubClient()
    ready(hw, client)
    await hw.flush()
    assert hw.stats()["buffered"] == 2
    assert [r[4] for r in hw._pending] == [1.0, 2.0]
    assert hw._client is None and client.closed, "the client is dropped+closed so the next flush reconnects"


class BlockedClient(StubClient):
    def __init__(self, fail=True):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.fail = fail

    async def insert(self, table, rows, column_names, settings):
        self.calls.append((rows, dict(settings)))
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("lost insert response")


async def test_rollup_reconfiguration_waits_for_the_pending_retry(monkeypatch):
    hw = make()
    hw.enqueue("s:1", "temperature", "test", 1, 1_000_000_000)
    ready(hw, StubClient())
    assert await hw.flush() is False
    hw.enqueue("s:1", "temperature", "test", 2, 2_000_000_000)
    client = BlockedClient(fail=False)
    client.release.set()
    ready(hw, client)
    hw._day_tz = "UTC"
    hw._next_day_tz_check = 0
    checks = []

    async def timezone(pool):
        from zoneinfo import ZoneInfo
        checks.append(pool)
        return ZoneInfo("UTC")

    monkeypatch.setattr(history, "house_timezone", timezone)
    assert await hw.flush() is True
    assert checks == []
    assert await hw.flush() is True
    assert checks == [None]


@pytest.mark.parametrize("batch_size", [2, 3])
async def test_slow_repeated_failures_bound_retry_and_new_rows_together(monkeypatch, batch_size):
    monkeypatch.setattr(history, "BUFFER_CAP", 3)
    monkeypatch.setattr(history, "BATCH_SIZE", batch_size)
    hw = make()
    for value in range(3):
        hw.enqueue("s:1", "temperature", "test", value, 1_000_000_000)
    batches = []
    for attempt in range(4):
        client = BlockedClient()
        ready(hw, client)
        task = asyncio.create_task(hw.flush())
        await client.started.wait()
        for value in range(3):
            hw.enqueue("s:1", "temperature", "test", 10 + attempt * 3 + value, 1_000_000_000)
            assert len(hw._buf) + len(hw._pending) == 3
            assert hw.stats()["buffered"] == 3
        client.release.set()
        assert await task is False
        batches.extend(client.calls)
    assert hw.stats()["dropped"] == 12
    assert all(batch == batches[0] for batch in batches)
    if batch_size == 2:
        assert [row[4] for row in hw._buf] == [21.0]


async def test_cancellation_keeps_the_same_batch_before_new_rows():
    hw = make()
    hw.enqueue("s:1", "temperature", "test", 1, 1_000_000_000)
    first = BlockedClient()
    ready(hw, first)
    task = asyncio.create_task(hw.flush())
    await first.started.wait()
    hw.enqueue("s:1", "temperature", "test", 2, 2_000_000_000)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    second = BlockedClient(fail=False)
    second.release.set()
    ready(hw, second)
    assert await hw.flush() is True
    assert second.calls == first.calls
    assert hw.stats()["buffered"] == 1
    assert await hw.flush() is True
    assert second.calls[-1][0][0][4] == 2.0
    assert second.calls[-1][1] != first.calls[0][1]


async def test_concurrent_flushes_and_close_drain_separate_batches(monkeypatch):
    monkeypatch.setattr(history, "BATCH_SIZE", 1)
    hw = make()
    for value in range(3):
        hw.enqueue("s:1", "temperature", "test", value, 1_000_000_000)
    client = BlockedClient(fail=False)
    ready(hw, client)
    first = asyncio.create_task(hw.flush())
    await client.started.wait()
    second = asyncio.create_task(hw.flush())
    await asyncio.sleep(0)
    assert len(client.calls) == 1
    client.release.set()
    assert await first is True and await second is True
    await hw.close()
    assert [batch[0][0][4] for batch in client.calls] == [0.0, 1.0, 2.0]
    assert hw.stats()["buffered"] == 0 and client.closed
