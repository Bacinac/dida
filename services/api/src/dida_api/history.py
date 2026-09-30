"""History query helpers over the ClickHouse firehose (+ rollups).

Tier selection by range:
  * ≤ 90 days  → raw `state_history`, bucketed at read time. For a home the raw
                 firehose is cheap to read even for a 90-day window, so raw serves
                 every range within its retention (and is populated for existing
                 data — no rollup backfill needed).
  * ≤ ~2 years → `state_history_1h` (hourly rollup).
  * beyond     → `state_history_1d` (daily rollup) — year-over-year.

The bucket width is picked to target ~TARGET_POINTS so a chart never pulls tens of
thousands of rows. Numeric capabilities return avg/min/max/last per bucket; a
purely non-numeric (enum/string) capability returns raw change points instead.
"""

from __future__ import annotations

import json

RAW_MAX_HOURS = 24 * 90          # raw retention window (days_raw default)
HOUR_TIER_MAX_HOURS = 24 * 366 * 2
MAX_HOURS = 24 * 366 * 11        # clamp: ~11y so year-over-year (10y solar) works
TARGET_POINTS = 1500


def _plan(hours: int) -> tuple[str, str, int, bool]:
    """(table, ts column, min bucket seconds, is_rollup)."""
    if hours <= RAW_MAX_HOURS:
        return "state_history", "ts", 1, False
    if hours <= HOUR_TIER_MAX_HOURS:
        return "state_history_1h", "bucket", 3600, True
    return "state_history_1d", "bucket", 86400, True


def _bucket_seconds(hours: int, floor: int) -> int:
    return max(floor, (hours * 3600) // TARGET_POINTS)


async def query_series(ch, entity_id: str, capability: str, hours: int, tz: str) -> dict:
    """One capability's series for the given window. `bucket` is inlined (a safe
    int derived from the clamped hours); entity/capability are bound parameters.
    Daily buckets are whole house-local days, as the daily rollup is cut."""
    table, tscol, floor, rollup = _plan(hours)
    bucket = _bucket_seconds(hours, floor)
    params = {"e": entity_id, "c": capability, "h": hours, "tz": tz}

    if rollup:
        if floor == 86400:
            bucket = -(-bucket // 86400) * 86400
            interval = f"INTERVAL {bucket // 86400} DAY, {{tz:String}}"
        else:
            interval = f"INTERVAL {bucket} SECOND"
        sql = (
            f"SELECT toUnixTimestamp(toStartOfInterval({tscol}, {interval})) AS s, "  # noqa: S608
            f"       avgMerge(avg_v) AS av, minMerge(min_v) AS mn, maxMerge(max_v) AS mx, "
            f"       argMaxMerge(last_v) AS lv "
            f"FROM {table} "
            f"WHERE entity_id = {{e:String}} AND capability = {{c:String}} "
            f"  AND {tscol} >= now() - toIntervalHour({{h:UInt32}}) "
            f"GROUP BY s ORDER BY s LIMIT 20000"
        )
    else:
        sql = (
            f"SELECT toUnixTimestamp(toStartOfInterval(ts, INTERVAL {bucket} SECOND)) AS s, "  # noqa: S608
            f"       avg(value_num) AS av, min(value_num) AS mn, max(value_num) AS mx, "
            f"       argMax(value_num, ts) AS lv "
            f"FROM state_history "
            f"WHERE entity_id = {{e:String}} AND capability = {{c:String}} "
            f"  AND ts >= now() - toIntervalHour({{h:UInt32}}) AND value_num IS NOT NULL "
            f"GROUP BY s ORDER BY s LIMIT 20000"
        )
    res = await ch.query(sql, parameters=params)
    points = [
        {"ts": int(s) * 1000, "v": av, "min": mn, "max": mx, "last": lv}
        for s, av, mn, mx, lv in res.result_rows
    ]
    if points:
        return {"capability": capability, "numeric": True, "bucket_seconds": bucket, "points": points}

    # No numeric data — fall back to raw string changes (enums), so an enum/text
    # capability still has a (step) history. Only raw carries value_str.
    res = await ch.query(
        "SELECT toUnixTimestamp64Milli(ts) AS ms, value_str FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND ts >= now() - toIntervalHour({h:UInt32}) AND value_str IS NOT NULL "
        "ORDER BY ts LIMIT 5000",
        parameters={"e": entity_id, "c": capability, "h": min(hours, RAW_MAX_HOURS)},
    )
    str_points = [{"ts": int(ms), "s": s} for ms, s in res.result_rows]
    return {
        "capability": capability,
        "numeric": not str_points,   # empty → treat as numeric-empty
        "bucket_seconds": bucket,
        "points": str_points,
    }


async def last_nonempty(ch, entity_id: str, capability: str) -> dict:
    """The last value this capability CARRIED, and when it stopped carrying it.

    A place that stands empty says nothing about itself; its last occupancy still
    does ("Ana's Car, three hours ago"). The newest row that carried something,
    then the first empty row after it. `until` is null while the value still
    stands — the caller is then reading a live state, not a memory, and should
    show the live one.

    A parked-vehicle descriptor is one stay per `since`. BABA publishes a blank
    `name` for a car it has not identified and fills the name in when a read lands,
    so the same stay can hold both — and a read that lands as the car drives out
    leaves the blank row last (28.08, P2 remembered Goran's half-hour as a blank
    line). So the name is looked up within that stay. A different `since` is a
    different car: 15.09, P1 skipped a 64-hour unidentified stay and remembered the
    named one before it, naming a car that had left three days earlier.
    """
    res = await ch.query(
        "SELECT toUnixTimestamp64Milli(ts) AS ms, value_str FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND value_str IS NOT NULL AND value_str != '' "
        "ORDER BY ts DESC LIMIT 1",
        parameters={"e": entity_id, "c": capability},
    )
    if not res.result_rows:
        return {"value": None, "ts": None, "until": None}
    ms, value = res.result_rows[0]
    stay = _blank_named_stay(value)
    if stay is not None:
        named = await ch.query(
            "SELECT value_str FROM state_history "
            "WHERE entity_id = {e:String} AND capability = {c:String} "
            "  AND JSONHas(value_str, 'name') AND JSONExtractString(value_str, 'name') != '' "
            "  AND JSONExtractString(value_str, 'since') = {s:String} "
            "ORDER BY ts DESC LIMIT 1",
            parameters={"e": entity_id, "c": capability, "s": stay},
        )
        if named.result_rows:
            value = named.result_rows[0][0]
    ended = await ch.query(
        "SELECT toUnixTimestamp64Milli(min(ts)) FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND ts > fromUnixTimestamp64Milli({t:Int64}) AND (value_str IS NULL OR value_str = '')",
        parameters={"e": entity_id, "c": capability, "t": int(ms)},
    )
    until = ended.result_rows[0][0] if ended.result_rows else None
    return {"value": value, "ts": int(ms), "until": int(until) if until else None}


def _blank_named_stay(value: str) -> str | None:
    """The `since` of a descriptor whose name is blank, else None."""
    try:
        v = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(v, dict) or v.get("name") != "" or not v.get("since"):
        return None
    return str(v["since"])


# Sparklines: the shape of the last day, for MANY entities at once.
#
# A device card wants a thumbnail of its main reading, and a page renders dozens of
# cards — one request each would be dozens of ClickHouse round trips on every load
# of the busiest page in the app. This answers them in ONE query and returns only
# what a 40-pixel-wide chart can actually use.
SPARK_POINTS = 24          # a day at hourly resolution — more is invisible at this size
SPARK_MAX_ENTITIES = 200   # bound the fan-out; a house has tens of devices, not thousands


async def query_sparklines(ch, pairs: list[tuple[str, str]], hours: int = 24) -> dict:
    """{entity_id: [v, …]} — bucketed averages, oldest first, for each (entity,
    capability) asked for. Only numeric readings: a sparkline of a text state is a
    line at a constant height, which says nothing.

    Values are RAW numbers, not normalised — the caller draws each line against its
    own min/max, so a battery at 64 % and a power reading at 300 W both fill their
    own box."""
    if not pairs:
        return {}
    pairs = pairs[:SPARK_MAX_ENTITIES]
    hours = max(1, min(hours, RAW_MAX_HOURS))
    bucket = max(60, (hours * 3600) // SPARK_POINTS)
    # One IN-list over the pairs. entity/capability are bound; bucket is a derived
    # int, never caller text.
    conds, params = [], {"h": hours}
    for i, (e, c) in enumerate(pairs):
        conds.append(f"(entity_id = {{e{i}:String}} AND capability = {{c{i}:String}})")
        params[f"e{i}"], params[f"c{i}"] = e, c
    sql = (
        f"SELECT entity_id, toUnixTimestamp(toStartOfInterval(ts, INTERVAL {bucket} SECOND)) AS s, "  # noqa: S608
        f"       avg(value_num) AS av "
        f"FROM state_history "
        f"WHERE ({' OR '.join(conds)}) "
        f"  AND ts >= now() - toIntervalHour({{h:UInt32}}) AND value_num IS NOT NULL "
        f"GROUP BY entity_id, s ORDER BY entity_id, s LIMIT 100000"
    )
    res = await ch.query(sql, parameters=params)
    out: dict[str, list[float]] = {}
    for entity_id, _s, av in res.result_rows:
        out.setdefault(entity_id, []).append(float(av))
    return out

