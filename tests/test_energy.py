"""Regression tests for the ENERGY aggregation (History → Energija).

Meter-role classification with PER-DEVICE dedup (one lifetime
counter per device, so a plug's energy + total_energy + since_boot don't triple
count), counter-delta daily kWh, the metered-window balance (consumption = import −
export + production), self-sufficiency, and the sub-meter source: a counter that
moved wins, power-integration only when it did not.

Run inside the api image (dida_api installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_energy.py"
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dida_api.energy import _effective, _suggest_roles, query_energy

TZ = ZoneInfo("Europe/Zagreb")

# ── _suggest_roles: ONE canonical lifetime counter per device, siblings ignored ──
ids = [
    "iammeter:meter:import_energy", "iammeter:meter:export_energy", "iammeter:meter:net_energy",
    "solar:inverter:energy_total", "solar:inverter:energy_today", "solar:inverter:ac", "solar:inverter:lifetime",
    "esphome:plug1:energy", "esphome:plug1:total_energy", "esphome:plug1:total_energy_since_boot",
    "smartthings:5e1970fd-dead", "smartthings:d67510ff-washer",
]
s = _suggest_roles(ids)


def test_suggest_roles():
    assert s["iammeter:meter:import_energy"] == "grid_import", "iammeter import → grid_import"
    assert s["iammeter:meter:export_energy"] == "grid_export", "iammeter export → grid_export"
    assert s["iammeter:meter:net_energy"] == "ignore", "grid meter's extra counter → ignore"
    assert s["solar:inverter:energy_total"] == "solar", "solar energy_total → solar (primary)"
    assert s["solar:inverter:energy_today"] == "ignore", "solar daily-reset counter → ignore"
    assert s["solar:inverter:ac"] == "ignore", "solar sibling → ignore (per-device dedup)"
    assert s["solar:inverter:lifetime"] == "ignore", "second solar 'lifetime' → ignore (only one primary)"
    assert s["esphome:plug1:total_energy"] == "submeter", "plug total_energy → submeter (primary)"
    assert s["esphome:plug1:energy"] == "ignore", "plug raw energy → ignore (dedup)"
    assert s["esphome:plug1:total_energy_since_boot"] == "ignore", "since_boot → ignore (reset counter)"

    assert s["smartthings:5e1970fd-dead"] == "submeter", "one-entity device is its own device"
    assert s["smartthings:d67510ff-washer"] == "submeter", "…not a sibling of every other smartthings device"

    counted = [e for e, r in s.items() if r != "ignore"]
    assert sum(e.startswith("solar:") for e in counted) == 1, "exactly ONE solar counter counts (no double-count)"
    assert sum(e.startswith("esphome:plug1") for e in counted) == 1, "exactly ONE plug counter counts"


def test_effective():
    # _effective: a saved role overrides the suggestion; garbage falls back
    assert _effective("solar:inverter:ac", {"solar:inverter:ac": "submeter"}, s) == "submeter", "saved override wins"
    assert _effective("solar:inverter:ac", {}, s) == "ignore", "no override → suggestion"
    assert _effective("solar:inverter:ac", {"solar:inverter:ac": "bogus"}, s) == "ignore", "invalid saved role → suggestion"


# ── query_energy: end-to-end balance over a fake ClickHouse ──
class _Res:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCh:
    """Dispatches on the SQL: distinct energy/power entities, the counters' last reading
    before the window, the 1d energy rollup, the 1h power rollup (for power-integration).
    Ignores the parameter filters — query_energy classifies by role, so returning the
    full canned set is fine."""

    def __init__(self, energy_ents, power_ents, daily, power_daily, seed=()):
        self._e, self._p, self._d, self._pd, self._s = energy_ents, power_ents, daily, power_daily, list(seed)
        self.params: list[dict] = []

    async def query(self, sql, parameters=None):
        self.params.append(parameters or {})
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


def test_query_energy():
    async def _run_query():
        d2 = datetime.now(TZ).date()
        d1 = d2 - timedelta(days=1)
        d0 = d2 - timedelta(days=2)  # baseline (no delta of its own)
        # Cumulative lifetime counters → daily delta = last − prior-day's last.
        counters = {
            "iammeter:meter:import_energy": {d0: 100.0, d1: 110.0, d2: 122.0},   # deltas 10, 12
            "iammeter:meter:export_energy": {d0: 50.0, d1: 55.0, d2: 61.0},      # deltas 5, 6
            "solar:inverter:energy_total": {d0: 1000.0, d1: 1008.0, d2: 1017.0},  # deltas 8, 9
        }
        daily = [(d, e, v) for e, vals in counters.items() for d, v in vals.items()]
        subs = {
            "esphome:dryer:total_energy": {d0: 20.0, d1: 21.0, d2: 21.5},  # read counter, Δ 1 + 0.5
            "smartthings:washer": {d0: 1300.0, d1: 1300.4, d2: 1300.4},    # one entity = one device
            "esphome:ups:total_energy": {d0: 100.0, d2: 500.0},            # unread d1, then weeks at once
            "esphome:idle:total_energy": {d2: 30.8},                       # last read long before the window
        }
        daily += [(d, e, v) for e, vals in subs.items() for d, v in vals.items()]
        energy_ents = [*counters, *subs, "esphome:plug1:total_energy"]  # plug1's counter never moved
        power_daily = [  # (entity, day, kWh, peak W)
            ("esphome:plug1:power", d1, 2.0, 120.0), ("esphome:plug1:power", d2, 2.5, 130.0),
            ("esphome:dryer:power", d1, 3.0, 2400.0), ("esphome:dryer:power", d2, 2.0, 2400.0),  # overstated
            ("smartthings:washer", d1, 0.0, 0.0),                                                    # flat zero
            ("esphome:ups:power", d1, 5.0, 250.0), ("esphome:ups:power", d2, 5.2, 250.0),
        ]
        power_ents = ["esphome:plug1:power", "esphome:dryer:power", "smartthings:washer", "esphome:ups:power"]
        seed = [("esphome:idle:total_energy", 30.0)]
        ch = FakeCh(energy_ents, power_ents, daily, power_daily, seed)
        return ch, await query_energy(ch, 7, {}, TZ)

    ch, res = asyncio.run(_run_query())
    windows = [p for p in ch.params if "frm" in p]
    assert len(windows) == 2 and all(p["tz"] == "Europe/Zagreb" for p in windows)
    assert all(datetime.fromtimestamp(p["frm"], TZ).time().hour == 0 for p in windows), \
        "days start at the house's midnight, not UTC's"
    t = res["totals"]
    assert t["import"] == 22.0, "import summed over metered days (10+12)"
    assert t["export"] == 11.0, "export summed (5+6)"
    assert t["production"] == 17.0, "production over metered days (8+9)"
    assert t["consumption"] == 28.0, "consumption = import − export + production (13+15)"
    assert t["self_consumed"] == 6.0, "self-consumed = production − export, ≥0 (3+3)"
    assert t["self_sufficiency"] == 21.4, "self-sufficiency = 6/28 = 21.4%"
    assert t["metered_days"] == 2, "two metered days (the baseline day has no delta)"
    subs = {x["device"]: x["kwh"] for x in res["submeters"]}
    assert subs.get("esphome:plug1") == 4.5, "frozen counter → power-integrated over metered days (2.0+2.5)"
    assert subs.get("esphome:dryer") == 1.5, "a moving counter wins over its power sensor (not 3.0+2.0)"
    assert subs.get("smartthings:washer") == 0.4, "a flat-zero power sensor does not hide a moving counter"
    assert subs.get("esphome:ups") == 10.2, \
        "unread day → power (5.0); 400 kWh in a day at a 250 W peak is a pause, not a day → power (5.2)"
    assert subs.get("esphome:idle") == 0.8, "the first reading diffs against the last one before the window"
    assert len(res["submeters"]) == 5
