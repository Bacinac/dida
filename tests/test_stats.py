"""Per-entity cumulatives: "how much" and "how often", derived from state_history.

The runtime integral is the part worth pinning down, because the obvious
implementation is wrong in a way that LOOKS right. Collapsing change points with
`lagInFrame(...) OVER (ORDER BY ts)` in ClickHouse evaluates the window per BLOCK
without a PARTITION, so the first row of each block sees a phantom previous value
— measured against production, the same integral came out as 175 015 hours inside
a 720-hour window. The fold happens in Python, in order, and these tests are what
keep it there.

Run inside the api image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src:/w/services/api/src python -m pytest tests/test_stats.py"
"""
from __future__ import annotations

import time

import pytest
from dida_api.stats import COUNTER_CAPS, MAX_POINTS, RUNTIME_CAPS, entity_stats

NOW = int(time.time())
H = 3600


class FakeCH:
    """Answers whatever rows the test hands it, ignoring the SQL."""

    def __init__(self, rows) -> None:
        self._rows = rows
        self.queries: list[str] = []

    async def query(self, sql, parameters=None, **_k):
        self.queries.append(sql)
        return type("R", (), {"result_rows": self._rows})()


async def runtime(rows, days=30):
    """`rows` are written chronologically here because that reads; the real query
    is ORDER BY ts DESC (so a truncated answer keeps the RECENT end), so the fake
    hands them back the way ClickHouse would."""
    return (await entity_stats(FakeCH(list(reversed(rows))), "mqtt:x", ["on_off"], days))["runtime"]


async def counter(rows, days=30):
    return (await entity_stats(FakeCH(rows), "mqtt:m", ["energy"], days))["counters"]


# --- runtime ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeat_reports_of_the_same_state_are_not_cycles():
    # A device that re-asserts "on" every 30s has turned on ONCE. Counting each
    # report would make a lamp that never moved look like it cycled 2 880 times.
    rows = [(NOW - 10 * H, 1.0)] + [(NOW - 10 * H + i * 30, 1.0) for i in range(1, 200)]
    (r,) = await runtime(rows)
    assert r["cycles"] == 1
    assert r["transitions"] == 1


@pytest.mark.asyncio
async def test_the_open_interval_runs_to_now_not_to_the_last_report():
    # A light on for three hours has been on for three hours; stopping the clock
    # at its last report under-counts exactly the state being asked about.
    (r,) = await runtime([(NOW - 3 * H, 1.0)])
    assert 2.99 <= r["hours_on"] <= 3.01


@pytest.mark.asyncio
async def test_a_closed_interval_is_measured_between_its_ends():
    (r,) = await runtime([(NOW - 5 * H, 1.0), (NOW - 3 * H, 0.0)])
    assert 1.99 <= r["hours_on"] <= 2.01, "two hours on, then off — the tail is not counted"
    assert r["cycles"] == 1


@pytest.mark.asyncio
async def test_alternating_on_off_counts_each_switch_on():
    rows = []
    for i in range(4):
        rows.append((NOW - (10 - 2 * i) * H, 1.0))
        rows.append((NOW - (10 - 2 * i) * H + H, 0.0))
    (r,) = await runtime(rows)
    assert r["cycles"] == 4
    assert 3.99 <= r["hours_on"] <= 4.01, "four one-hour stretches"


@pytest.mark.asyncio
async def test_duty_is_a_share_of_the_OBSERVED_window():
    # An entity that only started reporting yesterday must not read as 3 % duty
    # for a month it was on the whole time.
    (r,) = await runtime([(NOW - 2 * H, 1.0)], days=30)
    assert r["duty"] > 0.9, f"on for its whole observed life, got duty={r['duty']}"


@pytest.mark.asyncio
async def test_an_entity_with_no_history_reports_nothing_rather_than_zero():
    # A zero would be a claim ("it was never on"); absence is the truth ("we don't
    # know"). The panel shows the difference.
    assert await runtime([]) == []


# --- counters -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_counter_reports_its_GROWTH_not_its_reading():
    (c,) = await counter([(NOW - 5 * H, 1000.0), (NOW - H, 1012.5)])
    assert c["total"] == 12.5, "a lifetime meter reading is not this month's consumption"


@pytest.mark.asyncio
async def test_a_counter_reset_is_skipped_not_counted_as_a_huge_negative():
    # A meter rebooting to zero mid-period. Same rule the house energy page uses.
    (c,) = await counter([(NOW - 5 * H, 1000.0), (NOW - 4 * H, 1010.0),
                          (NOW - 3 * H, 0.0), (NOW - 2 * H, 4.0)])
    assert c["total"] == 14.0, "10 before the reset + 4 after; the reset itself adds nothing"


@pytest.mark.asyncio
async def test_a_single_reading_yields_no_growth():
    assert await counter([(NOW - H, 1000.0)]) == []


# --- selection ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_capability_with_no_cumulative_is_silently_absent():
    # A temperature sensor has no total and no runtime. Inventing zeros for it
    # would put two meaningless numbers on every sensor's panel.
    out = await entity_stats(FakeCH([(NOW, 21.5)]), "mqtt:t", ["temperature"], 30)
    assert out["counters"] == [] and out["runtime"] == []


@pytest.mark.asyncio
async def test_one_failing_capability_does_not_take_the_others_down():
    class HalfBroken(FakeCH):
        async def query(self, sql, parameters=None, **_k):
            if parameters and parameters.get("c") == "energy":
                raise RuntimeError("clickhouse said no")
            return await super().query(sql, parameters, **_k)

    out = await entity_stats(HalfBroken([(NOW - 2 * H, 1.0)]), "mqtt:x",
                             ["energy", "on_off"], 30)
    assert out["counters"] == []
    assert out["runtime"], "the numbers that ARE knowable are still reported"


def test_the_two_capability_sets_do_not_overlap():
    # A capability handled as both would be counted twice and reported twice.
    assert not (set(COUNTER_CAPS) & set(RUNTIME_CAPS))


# --- the cap ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_counter_is_aggregated_hourly_and_can_never_be_truncated():
    """A meter reporting every few seconds writes millions of rows a month.

    The first version pulled them raw under a LIMIT and hit that cap exactly on the
    house's own import meter — 23 kWh reported for a month whose real growth was
    220. A counter never needed the raw stream: its growth is the sum of per-bucket
    increases, and the bucket count is bounded by the PERIOD, not by how chatty the
    device is.
    """
    ch = FakeCH([(NOW - i * H, float(i)) for i in range(3)])
    await entity_stats(ch, "mqtt:m", ["energy"], 30)
    sql = ch.queries[0]
    assert "toStartOfHour" in sql and "GROUP BY" in sql, \
        "the counter must be aggregated by ClickHouse, not paged through here"
    assert "LIMIT" not in sql, "a bounded bucket count needs no row cap, and a cap could lie"


@pytest.mark.asyncio
async def test_runtime_says_so_when_the_cap_bites():
    # A silent truncation would describe a period nobody asked about while looking
    # exactly like a full answer.
    rows = [(NOW - i * 60, float(i % 2)) for i in range(MAX_POINTS)]
    (r,) = await runtime(rows, days=400)
    assert r["truncated"] is True
    assert r["window_h"] < 400 * 24, "the window reported is the one actually covered"


@pytest.mark.asyncio
async def test_an_untruncated_runtime_says_that_too():
    (r,) = await runtime([(NOW - 3 * H, 1.0)])
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_runtime_keeps_the_RECENT_end_when_truncated():
    # Newest-first + reverse. Keeping the oldest slice would answer about a window
    # that ended weeks ago while claiming to describe today.
    ch = FakeCH([(NOW - i * 60, float(i % 2)) for i in range(MAX_POINTS)])
    await entity_stats(ch, "mqtt:x", ["on_off"], 400)
    assert "ORDER BY ts DESC" in ch.queries[0]
