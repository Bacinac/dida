"""Integration test — the ENERGY paths test_energy.py leaves uncovered:
  * load_roles / save_roles round-trip against a REAL Postgres (empty, valid-only
    filter, invalid-JSON and non-dict-JSON fallbacks);
  * list_meters (suggested + saved-override effective role, registry filter,
    switched-off flag);
  * query_energy's empty-store short-circuit and the COUNTER-fallback sub-meter
    (a device with no power sensor);
  * query_energy_hourly end to end (empty short-circuit, the per-hour balance,
    the non-metered context hour, self-sufficiency, and BOTH sub-meter paths —
    the counter first, power-window integration when it did not move).

The Postgres-bound part uses the runner's ephemeral pg (see tests/run.sh); the
ClickHouse-bound part uses a canned fake dispatching on the SQL, as in
test_energy.py. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dida_api.energy import (
    SETTING_KEY,
    list_meters,
    load_roles,
    query_energy,
    query_energy_hourly,
    save_roles,
)
from dida_core import apply_migrations, jsonb_init, pg_pool, set_app_setting

TZ = ZoneInfo("Europe/Zagreb")


class _Res:
    def __init__(self, rows):
        self.result_rows = rows


class FakeChDaily:
    """Distinct energy/power entities, the counters' last reading before the window,
    the 1d energy rollup, the 1h power rollup."""

    def __init__(self, energy_ents, power_ents, daily, power_daily, seed=()):
        self._e, self._p, self._d, self._pd, self._s = energy_ents, power_ents, daily, power_daily, list(seed)

    async def query(self, sql, parameters=None):
        low = sql.lower()
        if "distinct entity_id" in low and "'energy'" in low:
            return _Res([(x,) for x in self._e])
        if "distinct entity_id" in low and "'power'" in low:
            return _Res([(x,) for x in self._p])
        if "state_history_1d" in low and "bucket <" in low:
            return _Res(self._s)
        if "state_history_1d" in low:
            return _Res(self._d)
        if "state_history_1h" in low:
            return _Res(self._pd)
        return _Res([])


class FakeChHourly:
    """Hourly variant — the two `state_history_1h` reads (energy balance vs the
    power window) are told apart by the capability literal in the SQL; the day rollup
    is read only for the counters' last reading before the day."""

    def __init__(self, energy_ents, power_ents, energy_1h, power_window, seed=()):
        self._e, self._p, self._e1h, self._pw, self._s = energy_ents, power_ents, energy_1h, power_window, list(seed)

    async def query(self, sql, parameters=None):
        low = sql.lower()
        if "distinct entity_id" in low and "'energy'" in low:
            return _Res([(x,) for x in self._e])
        if "distinct entity_id" in low and "'power'" in low:
            return _Res([(x,) for x in self._p])
        if "state_history_1d" in low:
            return _Res(self._s)
        if "state_history_1h" in low and "'power'" in low:
            return _Res(self._pw)
        if "state_history_1h" in low and "'energy'" in low:
            return _Res(self._e1h)
        return _Res([])


async def test_energy_roles_persistence():
    """load_roles/save_roles against a real Postgres — the app_settings round-trip
    plus every guard (empty, valid-only, bad JSON, non-dict JSON)."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM app_settings WHERE key = $1", SETTING_KEY)
    try:
        # nothing saved yet → {}
        assert await load_roles(pool) == {}, "no config → empty map"

        # save filters out invalid roles + non-string values; only valid pairs land
        await save_roles(pool, {"iammeter:m:import_energy": "grid_import", "x": "bogus", "n": 123})
        assert await load_roles(pool) == {"iammeter:m:import_energy": "grid_import"}, "save keeps only valid roles"

        # a stored map with a bad role is filtered on READ too
        await set_app_setting(pool, SETTING_KEY, json.dumps({"a": "solar", "b": "nope", "c": "submeter"}))
        assert await load_roles(pool) == {"a": "solar", "c": "submeter"}, "read drops invalid roles"

        # unparseable JSON → {}
        await set_app_setting(pool, SETTING_KEY, "definitely-not-json{{{")
        assert await load_roles(pool) == {}, "invalid JSON → empty map"

        # valid JSON that isn't an object → {}
        await set_app_setting(pool, SETTING_KEY, "[1, 2, 3]")
        assert await load_roles(pool) == {}, "non-dict JSON → empty map"
    finally:
        await pool.execute("DELETE FROM app_settings WHERE key = $1", SETTING_KEY)
        await pool.close()


async def test_list_meters_effective_role():
    """Every REGISTERED metering entity carries its heuristic suggestion, the
    effective role (a saved override wins) and whether its field is switched on.
    A counter the registry no longer has (renamed/removed device) only carries
    history, so it is left out."""
    ids = [
        "iammeter:m:import_energy", "iammeter:m:export_energy",
        "solar:inv:energy_total", "esphome:plug:total_energy", "esphome:plug:energy",
        "esphome:gone:total_energy",
    ]
    registered = {
        "iammeter:m:import_energy": True, "iammeter:m:export_energy": True,
        "solar:inv:energy_total": True, "esphome:plug:total_energy": False,
        "esphome:plug:energy": False,
    }
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1::text[])", ids)
    try:
        await pool.executemany(
            "INSERT INTO entities (entity_id, adapter, exposed) VALUES ($1, split_part($1, ':', 1), $2)",
            list(registered.items()),
        )
        ch = FakeChDaily(ids, [], [], [])
        meters = await list_meters(ch, pool, {"solar:inv:energy_total": "ignore"})  # admin override
        m = {x["entity_id"]: x for x in meters}
        assert set(m) == set(registered), "one row per registered energy entity, removed ones left out"
        assert m["iammeter:m:import_energy"]["suggested"] == "grid_import"
        assert m["iammeter:m:import_energy"]["role"] == "grid_import", "no override → suggestion is effective"
        assert m["solar:inv:energy_total"]["suggested"] == "solar", "heuristic still suggests solar"
        assert m["solar:inv:energy_total"]["role"] == "ignore", "saved override wins over the suggestion"
        assert m["esphome:plug:total_energy"]["role"] == "submeter", "the switched-off primary keeps its role"
        assert m["esphome:plug:total_energy"]["exposed"] is False, "a switched-off field is flagged, not hidden"
        assert m["iammeter:m:import_energy"]["exposed"] is True
    finally:
        await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1::text[])", ids)
        await pool.close()


async def test_query_energy_empty_and_counter_submeter():
    # empty store → the short-circuit shape
    empty = await query_energy(FakeChDaily([], [], [], []), 7, {}, TZ)
    assert empty == {"days": [], "totals": {}, "submeters": [], "meters": {}}, "no meters → empty result"

    # a sub-meter with NO power sensor falls back to its cumulative energy counter
    d2 = datetime.now(TZ).date()
    d1 = d2 - timedelta(days=1)
    d0 = d2 - timedelta(days=2)  # baseline day (prior reading, no delta of its own)
    counters = {
        "iammeter:m:import_energy": {d0: 100.0, d1: 110.0, d2: 122.0},  # metered days d1,d2 (Δ 10,12)
        "shelly:sw:energy": {d0: 10.0, d1: 12.0, d2: 15.0},            # submeter, no power (Δ 2,3)
    }
    daily = [(d, e, v) for e, vals in counters.items() for d, v in vals.items()]
    ch = FakeChDaily(list(counters), [], daily, [])  # power_ents empty → _power_daily short-circuits
    res = await query_energy(ch, 7, {}, TZ)
    assert res["totals"]["import"] == 22.0, "import over metered days (10+12)"
    assert res["totals"]["metered_days"] == 2
    subs = {x["device"]: x["kwh"] for x in res["submeters"]}
    assert subs == {"shelly:sw": 5.0}, "sub-meter kWh from its counter over metered days (2+3)"
    assert res["meters"]["submeters"] == ["shelly:sw:energy"], "the counter is classified submeter"


async def test_query_energy_hourly_empty():
    eh = await query_energy_hourly(FakeChHourly([], [], [], []), {}, 1_700_000_000, 1_700_010_800)
    assert eh == {"hours": [], "totals": {}, "submeters": []}, "no meters → empty hourly result"


async def test_query_energy_hourly_balance():
    frm = 1_700_000_000
    to = frm + 4 * 3600
    hb, h0, h1, h2, h3 = frm - 3600, frm, frm + 3600, frm + 2 * 3600, frm + 3 * 3600

    # cumulative lifetime counters, hour by hour — rows are (hs, entity_id, lv)
    # (the leading hb reading seeds the first delta)
    energy_1h = [
        # import: Δ 5,6,7 over h0,h1,h2
        (hb, "iammeter:m:import_energy", 100.0), (h0, "iammeter:m:import_energy", 105.0),
        (h1, "iammeter:m:import_energy", 111.0), (h2, "iammeter:m:import_energy", 118.0),
        # export: Δ 2,3,4
        (hb, "iammeter:m:export_energy", 50.0), (h0, "iammeter:m:export_energy", 52.0),
        (h1, "iammeter:m:export_energy", 55.0), (h2, "iammeter:m:export_energy", 59.0),
        # solar: Δ 4,5,6 (+ an extra h3 reading → a NON-metered context hour)
        (hb, "solar:inv:energy_total", 1000.0), (h0, "solar:inv:energy_total", 1004.0),
        (h1, "solar:inv:energy_total", 1009.0), (h2, "solar:inv:energy_total", 1015.0),
        (h3, "solar:inv:energy_total", 1022.0),
        # counter-only sub-meter (no power sensor): Δ 1,2,3 from its last reading before the day
        (hb, "shelly:sw:energy", 10.0), (h0, "shelly:sw:energy", 11.0),
        (h1, "shelly:sw:energy", 13.0), (h2, "shelly:sw:energy", 16.0),
        # a read counter beside a power sensor: Δ 0.5 at h1 wins over the power window
        (h1, "esphome:dryer:total_energy", 20.5),
        # a counter read again after weeks: 400 kWh in one day at a 250 W peak
        (h2, "esphome:ups:total_energy", 500.0),
    ]
    seed = [("shelly:sw:energy", 10.0), ("esphome:dryer:total_energy", 20.0), ("esphome:ups:total_energy", 100.0)]
    energy_ents = [
        "iammeter:m:import_energy", "iammeter:m:export_energy", "solar:inv:energy_total",
        "esphome:plug:total_energy",  # a power sensor beside a counter that never moved
        "esphome:dryer:total_energy",
        "esphome:ups:total_energy",
        "shelly:sw:energy",
    ]
    power_ents = ["esphome:plug:power", "esphome:dryer:power", "esphome:ups:power"]
    power_window = [  # _power_window rows: (entity, kWh, peak W)
        ("esphome:plug:power", 3.3, 180.0), ("esphome:dryer:power", 2.0, 2400.0), ("esphome:ups:power", 5.2, 250.0),
    ]

    eh = await query_energy_hourly(
        FakeChHourly(energy_ents, power_ents, energy_1h, power_window, seed), {}, frm, to
    )

    t = eh["totals"]
    assert t["import"] == 18.0, "import summed over metered hours (5+6+7)"
    assert t["export"] == 9.0, "export (2+3+4)"
    assert t["production"] == 15.0, "production over metered hours only (4+5+6); h3 is context, not counted"
    assert t["consumption"] == 24.0, "consumption = import − export + production (7+8+9)"
    assert t["self_consumed"] == 6.0, "self-consumed = production − export, ≥0 (2+2+2)"
    assert t["self_sufficiency"] == 25.0, "self-sufficiency = 6/24 = 25%"

    assert len(eh["hours"]) == 4, "three metered hours + one solar-only context hour"
    first = eh["hours"][0]
    assert first["ts"] == h0 * 1000, "hour timestamps are ms"
    assert first["consumption"] == 7.0 and first["self_consumed"] == 2.0, "first metered hour balance"
    last = eh["hours"][-1]
    assert last["production"] == 7.0, "context hour still carries its solar production"
    assert last["consumption"] is None and last["self_consumed"] is None, "non-metered hour has a null balance"

    subs = {x["device"]: x["kwh"] for x in eh["submeters"]}
    assert subs["esphome:plug"] == 3.3, "frozen counter → power-window-integrated sub-meter"
    assert subs["esphome:dryer"] == 0.5, "a moving counter wins over its power window"
    assert subs["esphome:ups"] == 5.2, "an increase no day could hold falls back to power"
    assert subs["shelly:sw"] == 6.0, "counter sub-meter over the window, first hour included (1+2+3)"
    assert eh["submeters"][0]["device"] == "shelly:sw", "sub-meters sorted by kWh descending"


async def test_query_energy_hourly_no_power_meters():
    """With no power meters at all, the power-window integration short-circuits and
    every sub-meter falls back to its energy counter."""
    frm = 1_700_000_000
    to = frm + 2 * 3600
    hb, h0, h1 = frm - 3600, frm, frm + 3600
    energy_1h = [
        (hb, "iammeter:m:import_energy", 100.0), (h0, "iammeter:m:import_energy", 104.0),
        (h1, "iammeter:m:import_energy", 109.0),
        (hb, "shelly:sw:energy", 10.0), (h0, "shelly:sw:energy", 11.0), (h1, "shelly:sw:energy", 14.0),
    ]
    eh = await query_energy_hourly(
        FakeChHourly(["iammeter:m:import_energy", "shelly:sw:energy"], [], energy_1h, [],
                     [("shelly:sw:energy", 10.0)]), {}, frm, to
    )
    subs = {x["device"]: x["kwh"] for x in eh["submeters"]}
    assert subs == {"shelly:sw": 4.0}, "counter-only sub-meter, power window empty (14−10 over the window)"
