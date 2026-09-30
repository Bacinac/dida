"""Floor-plan replay — what every rendered entity held across a past window.

The plan is a pure projection of the device store, so replaying the house needs
no second renderer: only the values the store HELD at a chosen instant. This
builds the bundle the browser scrubs locally — the value each (entity,
capability) carried at the window's start, plus every change inside it — so
dragging the timeline costs no round-trip.

Two classes of track, because the firehose is mostly a sensor restating itself
(24 h over the ~120 plan entities is ~85k rows, and an occupancy sensor repeats
`0` three times a second):
  * step        — change points with consecutive repeats dropped, so a switch
                  keeps every transition and nothing else.
  * measurement — one value per bucket (the bucket's last), the width derived
                  from the window so a week costs about what a day does.

Which class a capability falls in comes from the canonical capability model's
value type — FLOAT is a reading that drifts, everything else is a state that
changes. An unknown capability is treated as a step, so a new adapter's data
replays with every transition intact rather than being silently averaged.

The same model types the values back on the way out. History stores a bool as
0/1 in `value_num`, while a live surface holds `on_off` as a real boolean and
compares it as one — replaying the raw column would show every light off.
"""

from __future__ import annotations

from dida_core import CAPABILITIES, CapabilityKind, ValueType

TARGET_BUCKETS = 300           # measurement points per track across the window
MIN_BUCKET_SECONDS = 30
MAX_BUCKET_SECONDS = 3600
MIN_WINDOW_SECONDS = 300
MAX_WINDOW_SECONDS = 86400 * 7  # past a week the bundle stops being scrubbable
MAX_ENTITIES = 400
STEP_LIMIT = 40000
SERIES_LIMIT = 60000

# A value fingerprint that survives NULLs: '~' can't collide with a real number
# or string because every fingerprint carries the separator.
_FP = "concat(ifNull(toString(value_num), '~'), '|', ifNull(value_str, '~'))"

MEASUREMENT_CAPS = sorted(
    kind.value for kind, spec in CAPABILITIES.items() if spec.value_type is ValueType.FLOAT
)


def _value_type(cap: str) -> ValueType | None:
    try:
        return CAPABILITIES[CapabilityKind(cap)].value_type
    except (KeyError, ValueError):
        return None


def typed(cap: str, num: float | None, text: str | None) -> bool | int | float | str | None:
    """The value as the live store holds it, not as the column stores it."""
    vt = _value_type(cap)
    if vt is ValueType.STRING:
        return text
    if text is not None and vt is None:
        return text        # unknown capability: trust whichever column carried it
    if num is None:
        return text
    if vt is ValueType.BOOL:
        return bool(num)
    if vt is ValueType.INT:
        return int(num)
    return num


def bucket_seconds(window_seconds: int) -> int:
    return max(MIN_BUCKET_SECONDS, min(MAX_BUCKET_SECONDS, window_seconds // TARGET_BUCKETS))


def clamp_window(frm_ms: int, to_ms: int) -> tuple[int, int]:
    """Order the window and hold it to a scrubbable span. Widening a too-narrow
    request rather than rejecting it keeps a double-click on the timeline from
    being an error."""
    frm, to = (frm_ms, to_ms) if frm_ms <= to_ms else (to_ms, frm_ms)
    span = (to - frm) // 1000
    if span < MIN_WINDOW_SECONDS:
        to = frm + MIN_WINDOW_SECONDS * 1000
    elif span > MAX_WINDOW_SECONDS:
        frm = to - MAX_WINDOW_SECONDS * 1000
    return frm, to


async def _as_of(ch, ids: list[str], frm_ms: int) -> dict[tuple[str, str], tuple]:
    """The value each (entity, capability) carried at the window's start. Without
    it the plan opens blank and only fills in as things happen to change."""
    res = await ch.query(
        "SELECT entity_id, capability, argMax(value_num, ts) AS n, argMax(value_str, ts) AS s "
        "FROM state_history "
        "WHERE entity_id IN {ids:Array(String)} AND ts <= fromUnixTimestamp64Milli({frm:Int64}) "
        "GROUP BY entity_id, capability",
        parameters={"ids": ids, "frm": frm_ms},
    )
    return {(e, c): (n, s) for e, c, n, s in res.result_rows}


async def _steps(ch, ids: list[str], frm_ms: int, to_ms: int) -> tuple[list, bool]:
    """Change points for the non-FLOAT capabilities, consecutive repeats dropped.

    `lagInFrame` returns the String default ('') at each partition's first row,
    which no fingerprint can equal — so the first change in the window always
    survives the filter."""
    res = await ch.query(
        f"SELECT e, c, ms, n, s FROM ("  # noqa: S608
        f"  SELECT entity_id AS e, capability AS c, toUnixTimestamp64Milli(ts) AS ms,"
        f"         value_num AS n, value_str AS s, {_FP} AS fp,"
        f"         lagInFrame({_FP}) OVER ("
        f"           PARTITION BY entity_id, capability ORDER BY ts"
        f"           ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev"
        f"  FROM state_history"
        f"  WHERE entity_id IN {{ids:Array(String)}} AND capability NOT IN {{meas:Array(String)}}"
        f"    AND ts > fromUnixTimestamp64Milli({{frm:Int64}})"
        f"    AND ts <= fromUnixTimestamp64Milli({{to:Int64}})"
        f") WHERE fp != prev ORDER BY e, c, ms LIMIT {STEP_LIMIT + 1}",
        parameters={"ids": ids, "meas": MEASUREMENT_CAPS, "frm": frm_ms, "to": to_ms},
    )
    rows = res.result_rows
    return list(rows[:STEP_LIMIT]), len(rows) > STEP_LIMIT


async def _series(ch, ids: list[str], frm_ms: int, to_ms: int, bucket: int) -> tuple[list, bool]:
    """One point per bucket for the FLOAT capabilities — the bucket's last value,
    which is what a plan shows (a reading, not an average of readings)."""
    res = await ch.query(
        f"SELECT entity_id AS e, capability AS c,"  # noqa: S608
        f"       toUnixTimestamp(toStartOfInterval(ts, INTERVAL {bucket} SECOND)) * 1000 AS ms,"
        f"       argMax(value_num, ts) AS n "
        f"FROM state_history "
        f"WHERE entity_id IN {{ids:Array(String)}} AND capability IN {{meas:Array(String)}}"
        f"  AND ts > fromUnixTimestamp64Milli({{frm:Int64}})"
        f"  AND ts <= fromUnixTimestamp64Milli({{to:Int64}}) AND value_num IS NOT NULL "
        f"GROUP BY e, c, ms ORDER BY e, c, ms LIMIT {SERIES_LIMIT + 1}",
        parameters={"ids": ids, "meas": MEASUREMENT_CAPS, "frm": frm_ms, "to": to_ms},
    )
    rows = res.result_rows
    return list(rows[:SERIES_LIMIT]), len(rows) > SERIES_LIMIT


async def build_replay(ch, ids: list[str], frm_ms: int, to_ms: int) -> dict:
    """The scrubbable bundle for `ids` over [frm, to].

    `truncated` is part of the contract: a window that hits a row cap is reported
    so the surface can say the replay is partial instead of quietly showing a
    house where nothing happened after some point.
    """
    frm, to = clamp_window(frm_ms, to_ms)
    bucket = bucket_seconds((to - frm) // 1000)
    as_of = await _as_of(ch, ids, frm)
    steps, steps_cut = await _steps(ch, ids, frm, to)
    series, series_cut = await _series(ch, ids, frm, to, bucket)

    tracks: dict[tuple[str, str], dict] = {}

    def track(entity_id: str, capability: str, kind: str) -> dict:
        key = (entity_id, capability)
        tr = tracks.get(key)
        if tr is None:
            num, text = as_of.get(key, (None, None))
            tr = {
                "e": entity_id,
                "c": capability,
                "k": kind,
                "at": typed(capability, num, text),
                "p": [],
            }
            tracks[key] = tr
        return tr

    for e, c, ms, n, s in steps:
        track(e, c, "s")["p"].append([int(ms), typed(c, n, s)])
    for e, c, ms, n in series:
        track(e, c, "m")["p"].append([int(ms), typed(c, n, None)])
    # A track that never changed inside the window still has to render — the light
    # that was on the whole evening is exactly what a replay is for.
    for e, c in as_of:
        track(e, c, "m" if c in MEASUREMENT_CAPS else "s")

    return {
        "frm": frm,
        "to": to,
        "bucket_seconds": bucket,
        "truncated": steps_cut or series_cut,
        "tracks": list(tracks.values()),
    }
