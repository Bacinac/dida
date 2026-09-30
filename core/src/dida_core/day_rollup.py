"""The daily rollup cuts days at the house's midnight, not UTC's.

`state_history_1d` is what "a day" means wherever a person reads one: the energy
balance, multi-year history. Cut at UTC midnight, a day here ran 02:00–02:00 for
half the year and a night's consumption was split across two dates.

The zone is the house's (`house_timezone`), which is configuration, so it cannot
live in a numbered migration. The data table carries the zone its buckets were cut
in as its COMMENT, which travels with the data through the EXCHANGE below: a crash
at any step leaves a table that says what it holds, and the next run finishes the
job. The materialized view is derived from it.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("dida.day_rollup")

TABLE = "state_history_1d"
VIEW = "state_history_1d_mv"
STAGING = "state_history_1d_rebucket"
_TAG = "day_tz="
_UNTAGGED = "UTC"  # what ch migration 0002 cut before the zone was recorded


def _q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


def _view_sql(tz: str) -> str:
    return (
        f"CREATE MATERIALIZED VIEW {VIEW} TO {TABLE} AS SELECT "  # noqa: S608
        f"toStartOfDay(ts, {_q(tz)}) AS bucket, entity_id, capability, "
        "avgState(assumeNotNull(value_num)) AS avg_v, "
        "minState(assumeNotNull(value_num)) AS min_v, "
        "maxState(assumeNotNull(value_num)) AS max_v, "
        "argMinState(assumeNotNull(value_num), ts) AS first_v, "
        "argMaxState(assumeNotNull(value_num), ts) AS last_v, "
        "countState() AS cnt "
        "FROM state_history WHERE value_num IS NOT NULL "
        "GROUP BY entity_id, capability, bucket "
        f"COMMENT {_q(_TAG + tz)}"
    )


def _hours_from(old: str) -> str:
    # Per series, the first OLD-zone midnight from which the hourly tier holds
    # everything: hours from there on are re-cut, old daily rows before it are kept,
    # so no instant is counted twice or dropped. Per series because hourly TTL is
    # per capability class — one global cut-off would drop the daily rows of a
    # series whose hours had already expired.
    return (
        "SELECT entity_id, capability, "  # noqa: S608
        f"toDate(min(bucket), {_q(old)}) + (toStartOfDay(min(bucket), {_q(old)}) != min(bucket)) AS since "
        "FROM state_history_1h GROUP BY entity_id, capability"
    )


async def _tag(ch, name: str) -> str | None:
    res = await ch.query(
        "SELECT comment FROM system.tables WHERE database = currentDatabase() AND name = {n:String}",
        parameters={"n": name},
    )
    if not res.result_rows:
        return None
    m = re.fullmatch(re.escape(_TAG) + r"(.+)", res.result_rows[0][0] or "")
    return m.group(1) if m else _UNTAGGED


async def _rebucket(ch, old: str, new: str) -> None:
    """Re-cut the stored days from the hourly tier where it still holds them; an
    older daily row keeps its date under the new zone (the hours it was built from
    are gone, so that is the closest it can come)."""
    await ch.command(f"DROP TABLE IF EXISTS {STAGING}")
    await ch.command(f"CREATE TABLE {STAGING} AS {TABLE}")
    await ch.command(f"ALTER TABLE {STAGING} MODIFY COMMENT {_q(_TAG + new)}")
    await ch.command(
        f"INSERT INTO {STAGING} "  # noqa: S608
        f"SELECT toStartOfDay(h.bucket, {_q(new)}) AS b, h.entity_id, h.capability, "
        "avgMergeState(h.avg_v), minMergeState(h.min_v), maxMergeState(h.max_v), "
        "argMinMergeState(h.first_v), argMaxMergeState(h.last_v), countMergeState(h.cnt) "
        f"FROM state_history_1h AS h INNER JOIN ({_hours_from(old)}) AS f USING (entity_id, capability) "
        f"WHERE h.bucket >= toDateTime(f.since, {_q(old)}) "
        "GROUP BY h.entity_id, h.capability, b"
    )
    await ch.command(
        f"INSERT INTO {STAGING} "  # noqa: S608
        f"SELECT toDateTime(toDate(d.bucket, {_q(old)}), {_q(new)}), d.entity_id, d.capability, "
        "d.avg_v, d.min_v, d.max_v, d.first_v, d.last_v, d.cnt "
        f"FROM {TABLE} AS d LEFT JOIN ({_hours_from(old)}) AS f USING (entity_id, capability) "
        f"WHERE f.since IS NULL OR toDate(d.bucket, {_q(old)}) < f.since "
        "SETTINGS join_use_nulls = 1"
    )
    # The view goes first: nothing may land in the table between the copy and the
    # swap, and a view left pointing at the table the swap renames away would feed it.
    await ch.command(f"DROP VIEW IF EXISTS {VIEW}")
    await ch.command(f"EXCHANGE TABLES {TABLE} AND {STAGING}")
    await ch.command(f"DROP TABLE {STAGING}")
    log.info("daily rollup re-cut from %s days to %s days", old, new)


async def apply_house_day(ch, tz: str) -> None:
    """Make the daily rollup cut days at `tz` midnight. Idempotent; the caller must
    be the only writer of `state_history` while it runs (the engine, before it
    flushes)."""
    held = await _tag(ch, TABLE)
    if held is None:
        raise RuntimeError(f"{TABLE} does not exist — apply the ClickHouse migrations first")
    if held != tz:
        await _rebucket(ch, held, tz)
    if await _tag(ch, VIEW) != tz:
        await ch.command(f"DROP VIEW IF EXISTS {VIEW}")
        await ch.command(_view_sql(tz))
