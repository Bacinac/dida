"""Regression tests for history tier selection (history.py) — which table + bucket
width a range maps to. The tiering is the
non-obvious bit: raw serves everything within raw retention (≤90d), the hourly
rollup serves up to ~2y, the daily rollup beyond — and the bucket width targets a
fixed point budget so a chart never pulls tens of thousands of rows.

Run inside the api image (dida_api installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_history.py"
"""
from dida_api.history import (
    HOUR_TIER_MAX_HOURS,
    RAW_MAX_HOURS,
    TARGET_POINTS,
    _bucket_seconds,
    _plan,
)


def test_tier_selection():
    # tier selection by range
    assert _plan(6) == ("state_history", "ts", 1, False), "6h → raw"
    assert _plan(24) == ("state_history", "ts", 1, False), "24h → raw"
    assert _plan(RAW_MAX_HOURS) == ("state_history", "ts", 1, False), "90d (raw retention edge) → raw"
    assert _plan(RAW_MAX_HOURS + 1) == ("state_history_1h", "bucket", 3600, True), ">90d → hourly rollup"
    assert _plan(HOUR_TIER_MAX_HOURS) == ("state_history_1h", "bucket", 3600, True), "~2y edge → hourly rollup"
    assert _plan(HOUR_TIER_MAX_HOURS + 1) == ("state_history_1d", "bucket", 86400, True), ">2y → daily rollup"

    # rollup tiers carry their own minimum bucket (never finer than the rollup grain)
    assert _plan(RAW_MAX_HOURS + 1)[2] == 3600, "hourly rollup floor = 1h"
    assert _plan(HOUR_TIER_MAX_HOURS + 1)[2] == 86400, "daily rollup floor = 1d"


def test_bucket_width():
    # bucket width: ~TARGET_POINTS across the window, floored to the tier grain
    assert _bucket_seconds(6, 1) == max(1, 6 * 3600 // TARGET_POINTS), "6h bucket = span/points, floored"
    assert _bucket_seconds(24, 1) == 24 * 3600 // TARGET_POINTS, "24h bucket = span/points"
    assert _bucket_seconds(24 * 30, 3600) == 3600, "30d over the hourly rollup can't go finer than 1h"
    assert _bucket_seconds(1, 10) == 10, "floor wins when the window is tiny (span/points < floor)"
    # more points for a wider window (monotonic in range)
    assert _bucket_seconds(48, 1) > _bucket_seconds(24, 1), "a wider window uses a wider bucket"


class _FakeCH:
    """Records the SQL it is asked for and answers from a canned list of row sets."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sql: list[str] = []

    async def query(self, sql, parameters=None):
        self.sql.append(sql)
        rows = self.answers.pop(0)
        return type("Res", (), {"result_rows": rows})()


async def _last(answers):
    from dida_api.history import last_nonempty
    ch = _FakeCH(answers)
    return await last_nonempty(ch, "baba:cam:scene:p1", "parked_vehicle"), ch


def test_last_nonempty_reports_the_value_and_when_it_ended():
    import asyncio
    out, ch = asyncio.run(_last([[(1_700_000_000_000, '{"name": "A car"}')], [(1_700_003_600_000,)]]))
    assert out == {"value": '{"name": "A car"}', "ts": 1_700_000_000_000, "until": 1_700_003_600_000}, \
        "the last value it carried, plus the moment it stopped carrying it"
    assert "value_str != ''" in ch.sql[0] and "ORDER BY ts DESC LIMIT 1" in ch.sql[0], \
        "the newest NON-empty row — a place empty right now must not answer with its own emptiness"
    assert "ts > fromUnixTimestamp64Milli" in ch.sql[1], "the end is the first empty row AFTER that value"


def test_a_nameless_row_takes_the_name_its_own_stay_earned():
    """BABA fills the name in when a read lands, and a read that lands as the car
    drives out leaves a blank row last: P2 remembered Goran's half-hour as a
    blank line while the named row of the same stay sat right behind it."""
    import asyncio
    blank = '{"name": "", "since": "2026-08-28T10:00:00+00:00"}'
    named = '{"name": "Goran", "since": "2026-08-28T10:00:00+00:00"}'
    out, ch = asyncio.run(_last([[(1_700_000_000_000, blank)], [(named,)], [(None,)]]))
    assert out["value"] == named
    assert "JSONExtractString(value_str, 'since') = {s:String}" in ch.sql[1], \
        "the name is looked up within the same stay, never across stays"


def test_an_unidentified_stay_is_remembered_as_itself():
    """15.09, P1: a 64-hour unidentified stay was skipped and the named stay
    before it remembered instead, naming a car that had left three days earlier.
    A stay whose name never landed is remembered as unidentified."""
    import asyncio
    blank = '{"name": "", "since": "2026-09-12T11:15:21+00:00"}'
    out, _ = asyncio.run(_last([[(1_700_000_000_000, blank)], [], [(1_700_003_600_000,)]]))
    assert out["value"] == blank and out["until"] == 1_700_003_600_000


def test_a_plain_string_capability_is_untouched_by_that():
    """`JSONHas` is false for anything that is not a JSON object, so a
    capability storing a bare string keeps answering as it always did."""
    import asyncio
    out, _ = asyncio.run(_last([[(1_700_000_000_000, "closed")], [(None,)]]))
    assert out["value"] == "closed"


def test_last_nonempty_without_an_end_is_still_current():
    import asyncio
    out, _ = asyncio.run(_last([[(1_700_000_000_000, "x")], [(None,)]]))
    assert out["until"] is None, "no empty row after it → the value still stands, not a memory"


def test_last_nonempty_with_no_history_answers_nothing():
    import asyncio
    out, ch = asyncio.run(_last([[]]))
    assert out == {"value": None, "ts": None, "until": None}, "never occupied → no memory to show"
    assert len(ch.sql) == 1, "and no second query: there is no value to find an end for"
