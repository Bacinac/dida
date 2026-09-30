"""Regression tests for floor-plan replay (replay.py) — the bundle a browser scrubs.

Two things here are load-bearing and easy to get silently wrong:

  * TYPING. History keeps a bool as 0/1 in `value_num`, while every surface holds
    `on_off` as a real boolean and compares it with `=== true`. Replaying the raw
    column shows a house where no light was ever on — a bug that looks like data.
  * CLASSIFICATION. Which capabilities are sampled per bucket rather than per
    change comes from the canonical model's value type, not a list maintained in
    this module. A drifting reading must never be classified as a step (the
    firehose is mostly a sensor restating itself: 24 h over ~120 plan entities is
    ~85k rows, ~9k after this split), and a state must never be averaged away.

Run inside the api image (dida_api installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_replay.py"
"""
import pytest
from dida_api.replay import (
    MAX_WINDOW_SECONDS,
    MEASUREMENT_CAPS,
    MIN_BUCKET_SECONDS,
    MIN_WINDOW_SECONDS,
    TARGET_BUCKETS,
    bucket_seconds,
    build_replay,
    clamp_window,
    typed,
)

HOUR = 3600_000


def test_bool_survives_the_numeric_column():
    # the whole point: 0/1 out of value_num must come back as a real boolean,
    # because the plan compares with identity, not truthiness
    assert typed("on_off", 1.0, None) is True, "on_off 1 → True"
    assert typed("on_off", 0.0, None) is False, "on_off 0 → False"
    assert typed("occupancy", 1.0, None) is True
    assert typed("motion", 0.0, None) is False
    assert typed("lock", 1.0, None) is True


def test_numeric_and_string_typing():
    assert typed("temperature", 21.5, None) == 21.5, "float stays float"
    assert isinstance(typed("brightness", 60.0, None), int), "int capability → int, not 60.0"
    assert typed("brightness", 60.0, None) == 60
    assert typed("mower", None, "mowing") == "mowing", "string capability reads value_str"
    assert typed("enum", None, "pir_and_radar") == "pir_and_radar"
    # a capability the model doesn't know must not be mangled — trust the column
    assert typed("not_a_capability", None, "whatever") == "whatever"
    assert typed("not_a_capability", 7.0, None) == 7.0
    # nothing recorded stays nothing: absent, not a zero
    assert typed("temperature", None, None) is None
    assert typed("on_off", None, None) is None


def test_measurement_classification_follows_the_model():
    # drifting readings are bucketed …
    for cap in ("temperature", "humidity", "illuminance", "power", "pressure", "battery"):
        assert cap in MEASUREMENT_CAPS, f"{cap} drifts → must be sampled per bucket"
    # … states are not, or their transitions would be averaged away
    for cap in ("on_off", "occupancy", "motion", "contact", "lock", "brightness",
                "person_count", "mower", "enum", "hvac_mode", "media_transport"):
        assert cap not in MEASUREMENT_CAPS, f"{cap} is a state → must keep every change"


def test_bucket_width_scales_with_the_window():
    day = 24 * 3600
    assert bucket_seconds(day) == day // TARGET_BUCKETS, "24h → span/buckets"
    assert bucket_seconds(day) == 288, "24h → 288s buckets (~300 points a track)"
    assert bucket_seconds(60) == MIN_BUCKET_SECONDS, "a tiny window can't go below the floor"
    assert bucket_seconds(7 * day) > bucket_seconds(day), "wider window → wider bucket"


def test_window_is_ordered_and_bounded():
    frm, to = clamp_window(5_000_000, 1_000_000)
    assert frm < to, "a reversed window is ordered, not rejected"
    frm, to = clamp_window(1_000_000, 1_000_000)
    assert (to - frm) // 1000 == MIN_WINDOW_SECONDS, "a zero-width window widens to the minimum"
    frm, to = clamp_window(0, MAX_WINDOW_SECONDS * 2000)
    assert (to - frm) // 1000 == MAX_WINDOW_SECONDS, "an over-wide window is trimmed from the start"
    assert to == MAX_WINDOW_SECONDS * 2000, "trimming keeps the requested END (replay runs up to now)"


class FakeCH:
    """Stands in for ClickHouse: returns rows per query by matching the query text.
    Keyed on what each statement is FOR, so a rewritten query still has to keep
    its shape (as-of snapshot / step changes / bucketed series)."""

    def __init__(self, as_of=(), steps=(), series=()):
        self.as_of, self.steps, self.series = as_of, steps, series
        self.queries = []

    async def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if "argMax(value_str" in sql:
            rows = self.as_of
        elif "lagInFrame" in sql:
            rows = self.steps
        else:
            rows = self.series
        return type("R", (), {"result_rows": list(rows)})()


@pytest.mark.asyncio
async def test_bundle_shape():
    frm, to = 10 * HOUR, 34 * HOUR
    ch = FakeCH(
        as_of=[
            ("light:kitchen", "on_off", 1.0, None),
            ("sensor:hall", "temperature", 21.0, None),
            ("mower:x", "mower", None, "docked"),
        ],
        steps=[
            ("light:kitchen", "on_off", frm + HOUR, 0.0, None),
            ("mower:x", "mower", frm + 2 * HOUR, None, "mowing"),
        ],
        series=[("sensor:hall", "temperature", frm + HOUR, 21.4)],
    )
    out = await build_replay(ch, ["light:kitchen", "sensor:hall", "mower:x"], frm, to)

    assert out["frm"] == frm and out["to"] == to
    assert out["truncated"] is False
    tracks = {(t["e"], t["c"]): t for t in out["tracks"]}

    kitchen = tracks[("light:kitchen", "on_off")]
    assert kitchen["k"] == "s", "a switch is a step track"
    assert kitchen["at"] is True, "it was ON when the window opened"
    assert kitchen["p"] == [[frm + HOUR, False]], "and went off an hour in — as a bool"

    hall = tracks[("sensor:hall", "temperature")]
    assert hall["k"] == "m", "a thermometer is a measurement track"
    assert hall["at"] == 21.0
    assert hall["p"] == [[frm + HOUR, 21.4]]

    mower = tracks[("mower:x", "mower")]
    assert mower["at"] == "docked" and mower["p"] == [[frm + 2 * HOUR, "mowing"]]


@pytest.mark.asyncio
async def test_unchanged_track_still_renders():
    # The light that stayed on all evening produces no change rows at all. It has
    # to be in the bundle anyway, or the replay opens with it dark.
    frm, to = 0, 24 * HOUR
    ch = FakeCH(as_of=[("light:porch", "on_off", 1.0, None)])
    out = await build_replay(ch, ["light:porch"], frm, to)
    assert len(out["tracks"]) == 1
    assert out["tracks"][0]["at"] is True
    assert out["tracks"][0]["p"] == [], "no changes in the window, but the track exists"


@pytest.mark.asyncio
async def test_truncation_is_reported():
    # A capped window must SAY it was capped: a silent cut reads as "nothing
    # happened after this point", which is the one lie a replay must not tell.
    from dida_api import replay as mod
    frm, to = 0, 24 * HOUR
    steps = [("e", "on_off", i, float(i % 2), None) for i in range(mod.STEP_LIMIT + 1)]
    ch = FakeCH(steps=steps)
    out = await build_replay(ch, ["e"], frm, to)
    assert out["truncated"] is True
    assert len(out["tracks"][0]["p"]) == mod.STEP_LIMIT, "cut at the cap, not beyond"


@pytest.mark.asyncio
async def test_step_and_series_queries_are_disjoint():
    # The two sampling queries must partition the capabilities between them, or a
    # capability is either counted twice or dropped.
    ch = FakeCH()
    await build_replay(ch, ["e"], 0, 24 * HOUR)
    step_sql = next(s for s, _ in ch.queries if "lagInFrame" in s)
    series_sql = next(s for s, _ in ch.queries if "toStartOfInterval" in s)
    assert "capability NOT IN {meas:Array(String)}" in step_sql
    assert "capability IN {meas:Array(String)}" in series_sql
    for _, params in ch.queries:
        if params and "meas" in params:
            assert params["meas"] == MEASUREMENT_CAPS, "both sides bind the same list"
