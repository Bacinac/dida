"""Regression tests for the engine HistoryWriter buffer (dida_engine.history) —
the ClickHouse firehose's degraded-path robustness the audit flagged as untested.

Pure buffer logic + the re-buffer-on-failure path over a stub CH client; no infra
(the pool + real ClickHouse are never touched — value coercion, the drop-oldest
hard cap, and 'a failed insert loses nothing' are all in-memory).

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/engine:latest \
      -c "python -m pytest tests/test_history_writer.py"
"""
from dida_engine.history import (
    BUFFER_CAP,
    COLUMNS,
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
    """A CH client whose insert always fails — to exercise the re-buffer path."""

    def __init__(self):
        self.closed = False

    async def insert(self, table, rows, column_names):
        raise RuntimeError("clickhouse down")

    async def close(self):
        self.closed = True


async def test_flush_rebuffers_on_insert_failure():
    # A ClickHouse insert failing mid-flush must RE-BUFFER the rows (order preserved),
    # never lose them, and drop the client so the next flush reconnects. This is the
    # whole reason HistoryWriter buffers instead of writing inline.
    hw = make()
    hw.enqueue("s:1", "temperature", "test", 1.0, 1_000_000_000)
    hw.enqueue("s:1", "temperature", "test", 2.0, 2_000_000_000)
    client = StubClient()
    hw._client = client
    await hw._flush_table("state_history", hw._buf, COLUMNS)
    assert len(hw._buf) == 2, "a failed insert re-buffers both rows — nothing is lost"
    assert [r[4] for r in hw._buf] == [1.0, 2.0], "re-buffer preserves original order"
    assert hw._client is None and client.closed, "the client is dropped+closed so the next flush reconnects"
