"""The daily rollup cuts days at the house's midnight — against a real ClickHouse.

A day cut at UTC midnight ran 02:00–02:00 here for half the year, so a meter
reading taken at 00:30 on the 21st was booked to the 20th. The re-cut is SQL that
only a real server can check (aggregate-state merges, EXCHANGE, the view), so this
runs against the gate's ephemeral ClickHouse.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from dida_core import apply_ch_migrations, apply_house_day, ch_client

ZG = "Europe/Zagreb"
MIGRATIONS = "/w/ch/migrations"
COLUMNS = ["ts", "entity_id", "capability", "adapter", "value_num", "value_str"]


@pytest.fixture
async def ch():
    client = await ch_client()
    for table in ("state_history_1d_mv", "state_history_1h_mv"):
        await client.command(f"DROP VIEW IF EXISTS {table}")
    for table in ("state_history", "state_history_1h", "state_history_1d",
                  "state_history_1d_rebucket", "ch_schema_migrations"):
        await client.command(f"DROP TABLE IF EXISTS {table}")
    await apply_ch_migrations(client, MIGRATIONS)
    yield client
    await client.close()


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def _write(ch, entity_id: str, rows: list[tuple[str, float]]) -> None:
    await ch.insert("state_history",
                    [[_at(ts), entity_id, "energy", "test", v, None] for ts, v in rows],
                    column_names=COLUMNS)


async def _days(ch, entity_id: str) -> dict[date, float]:
    res = await ch.query(
        "SELECT toDate(bucket, {tz:String}) AS d, argMaxMerge(last_v) FROM state_history_1d "
        "WHERE entity_id = {e:String} GROUP BY d ORDER BY d",
        parameters={"tz": ZG, "e": entity_id})
    return {d: v for d, v in res.result_rows}


async def _comment(ch, name: str) -> str:
    res = await ch.query("SELECT comment FROM system.tables WHERE database = currentDatabase() "
                         "AND name = {n:String}", parameters={"n": name})
    return res.result_rows[0][0]


# 22:00Z on the 19th is local midnight on the 20th (CEST); 22:30Z on the 20th is
# 00:30 on the 21st.
METER = [("2026-09-19 00:00:00", 8.0), ("2026-09-19 22:00:00", 9.0), ("2026-09-20 21:30:00", 10.0),
         ("2026-09-20 22:30:00", 11.0), ("2026-09-21 12:00:00", 15.0)]
LOCAL_DAYS = {date(2026, 9, 19): 8.0, date(2026, 9, 20): 10.0, date(2026, 9, 21): 15.0}


async def test_a_reading_after_local_midnight_belongs_to_the_next_day(ch):
    await apply_house_day(ch, ZG)
    await _write(ch, "m:new", METER)
    assert await _days(ch, "m:new") == LOCAL_DAYS


async def test_days_already_stored_in_utc_are_re_cut_from_the_hours(ch):
    await _write(ch, "m:old", METER)
    assert (await _days(ch, "m:old"))[date(2026, 9, 20)] == 11.0, "the UTC cut, before"
    await apply_house_day(ch, ZG)
    assert await _days(ch, "m:old") == LOCAL_DAYS
    assert await _comment(ch, "state_history_1d") == f"day_tz={ZG}"


async def test_a_day_whose_hours_expired_keeps_its_date(ch):
    await _write(ch, "m:gone", [("2026-06-01 10:00:00", 1.0), ("2026-06-01 23:30:00", 2.0)])
    await ch.command("TRUNCATE TABLE state_history_1h")
    await apply_house_day(ch, ZG)
    assert await _days(ch, "m:gone") == {date(2026, 6, 1): 2.0}


async def test_the_first_partial_day_comes_from_the_old_daily_row(ch):
    # The hourly tier starts at 21:00Z on the 20th, mid-way through a UTC day: that
    # day keeps its old row and the hours are re-cut from the next UTC midnight.
    await _write(ch, "m:edge", METER[2:])
    await apply_house_day(ch, ZG)
    assert await _days(ch, "m:edge") == {date(2026, 9, 20): 11.0, date(2026, 9, 21): 15.0}


async def test_it_is_idempotent_and_repairs_a_missing_view(ch):
    await _write(ch, "m:twice", METER)
    await apply_house_day(ch, ZG)
    before = await _days(ch, "m:twice")
    await apply_house_day(ch, ZG)
    assert await _days(ch, "m:twice") == before
    await ch.command("DROP VIEW state_history_1d_mv")  # a crash between the swap and the view
    await apply_house_day(ch, ZG)
    await _write(ch, "m:twice", [("2026-09-21 21:30:00", 16.0)])
    assert (await _days(ch, "m:twice"))[date(2026, 9, 21)] == 16.0


async def test_a_zone_change_re_cuts_again(ch):
    await _write(ch, "m:move", METER)
    await apply_house_day(ch, ZG)
    await apply_house_day(ch, "UTC")
    res = await ch.query("SELECT toDate(bucket, 'UTC') AS d, argMaxMerge(last_v) FROM state_history_1d "
                         "WHERE entity_id = 'm:move' GROUP BY d ORDER BY d")
    assert dict(res.result_rows) == {date(2026, 9, 19): 9.0, date(2026, 9, 20): 11.0,
                                     date(2026, 9, 21): 15.0}
