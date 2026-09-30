"""Cumulative ENERGY aggregation over the ClickHouse rollups — the House view.

Turns the per-meter kWh counters into the numbers a homeowner actually cares
about: consumption, solar production, self-sufficiency and grid import/export,
day by day.

Each `energy` entity plays a ROLE. The admin assigns roles in the UI (persisted
to `app_settings.energy_config`); until then a heuristic on the entity id fills in
a sensible default (`_suggest_role`), so the dashboard works out of the box:
  * grid_import — kWh drawn from the grid          (`…import_energy`)
  * grid_export — solar surplus fed back           (`…export_energy`)
  * solar       — solar production, lifetime counter (`solar:…`)
  * submeter    — a smart plug / appliance          → the breakdown
  * ignore      — not counted (a redundant counter on a meter, daily-reset counters)

Daily kWh = the counter's increase across the day (last − prior day's last), read
from `state_history_1d` (argMax `last_v`), clamped ≥0 so a meter reboot / counter
reset never shows as a negative day. A day is the house's local day: the rollup is
cut at its midnight (`dida_core.day_rollup`), and "today" is read on its clock. `consumption = import − export + production`
(what the house used = grid draw plus the solar it self-consumed).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dida_core import set_app_setting

SETTING_KEY = "energy_config"
MAX_DAYS = 366 * 3
ROLES = ("grid_import", "grid_export", "solar", "submeter", "ignore")


# A device commonly exposes SEVERAL energy sensors (a lifetime `total_energy`, a
# `total_daily_energy`, a `*_since_boot`, a raw `energy`…). Counting them all would
# double- or triple-count that device, so the default picks ONE canonical lifetime
# counter per device and marks the siblings `ignore`. The admin overrides any of it
# in the config UI. Grouping is by the entity id minus its last segment
# (`esphome:plug:total_energy` → device `esphome:plug`), except that an adapter
# modelling a device as ONE entity (`smartthings:<uuid>`, z2m `mqtt:<name>`) has no
# field segment — there the id is the device, or all of that adapter's devices would
# collapse into one and share a single primary counter.
_RESET = ("daily", "today", "since_boot")  # never a lifetime counter → ignore
_PRIMARY_ORDER = ("energy_total", "total_energy", "lifetime", "energy")
PEER_ADAPTER = "peer"  # entity-id namespace of another DIDA installation's mirror


def _dev(entity_id: str) -> str:
    head = entity_id.rpartition(":")[0]
    return head if ":" in head else entity_id


def _leaf(entity_id: str) -> str:
    return entity_id.rsplit(":", 1)[-1].lower()


def _primary_rank(entity_id: str) -> int:
    lf = _leaf(entity_id)
    if any(w in lf for w in _RESET):
        return 99  # a reset counter can never be the device's primary
    return _PRIMARY_ORDER.index(lf) if lf in _PRIMARY_ORDER else 50


def _suggest_roles(ids: list[str]) -> dict[str, str]:
    """Heuristic entity→role map, de-duplicated per device (one counter per device)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for e in ids:
        groups[_dev(e)].append(e)

    out: dict[str, str] = {}
    for dev, ents in groups.items():
        # A counter mirrored from another house measures THAT house. Defaulting it
        # to a role would fold a second building's grid draw into this one's totals,
        # so the peer's meters start ignored and the admin opts in deliberately.
        if dev.startswith(f"{PEER_ADAPTER}:"):
            out.update({e: "ignore" for e in ents})
            continue
        # A grid meter carries BOTH an import and an export counter (each its own
        # role); any other counter on it is redundant.
        if any("import_energy" in _leaf(e) or "export_energy" in _leaf(e) for e in ents):
            for e in ents:
                lf = _leaf(e)
                out[e] = ("grid_import" if "import_energy" in lf
                          else "grid_export" if "export_energy" in lf else "ignore")
            continue
        role = "solar" if dev.lower().startswith("solar:") else "submeter"
        primary = min(ents, key=_primary_rank)
        for e in ents:
            out[e] = role if (e == primary and _primary_rank(e) < 99) else "ignore"
    return out


def _effective(entity_id: str, saved: dict, sugg: dict) -> str:
    r = saved.get(entity_id)
    return r if r in ROLES else sugg.get(entity_id, "ignore")


async def load_roles(pool) -> dict:
    """Saved entity→role map from app_settings (only valid roles kept)."""
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", SETTING_KEY)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return {k: v for k, v in d.items() if v in ROLES} if isinstance(d, dict) else {}


async def save_roles(pool, roles: dict) -> None:
    clean = {str(k): v for k, v in roles.items() if v in ROLES}
    await set_app_setting(pool, SETTING_KEY, json.dumps(clean))


async def _energy_entities(ch) -> list[str]:
    res = await ch.query(
        "SELECT DISTINCT entity_id FROM state_history WHERE capability = 'energy' ORDER BY entity_id"
    )
    return [r[0] for r in res.result_rows]


async def _power_entities(ch) -> list[str]:
    res = await ch.query(
        "SELECT DISTINCT entity_id FROM state_history WHERE capability = 'power'"
    )
    return [r[0] for r in res.result_rows]


def _local_day_start(day: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, datetime.min.time(), tz).timestamp())


# A counter's increase is trusted as ONE day's consumption only if the device could
# have drawn it in a day. A counter read again after a pause (a field switched back
# on, an adapter down for weeks) moves by the whole pause at its first reading, and
# diffed against the last reading before it that pause would land on a single day.
# The bound is the device's peak power that day over 24 h, doubled: the increase may
# start just before midnight and sampled peaks miss inrush, while a pause of weeks
# overshoots it many times over. No power evidence (no sensor, or one stuck at 0 W
# like the SmartThings washer) → no bound; the counter is all there is.
_DAY_BOUND_HOURS = 48


def _device_day_kwh(counter: float | None, power_kwh: float, peak_w: float) -> float:
    """One sub-meter device's kWh for one day: its counter's increase when it was read
    that day and the increase is plausible, else its integrated power."""
    if counter is not None and (peak_w <= 0 or counter <= _DAY_BOUND_HOURS * peak_w / 1000):
        return counter
    return power_kwh


async def _counter_seed(ch, ids: list[str], before: int) -> dict[str, float]:
    """Each counter's last reading before `before` (unix s), however long ago — the
    baseline its first reading in a window is diffed against. A counter that reports
    only on change (an idle dryer) may have no reading in the preceding hour or day."""
    if not ids:
        return {}
    res = await ch.query(
        "SELECT entity_id, argMax(lv, bucket) FROM ("
        "  SELECT entity_id, bucket, argMaxMerge(last_v) AS lv FROM state_history_1d "
        "  WHERE capability = 'energy' AND entity_id IN {ids:Array(String)} "
        "    AND bucket < fromUnixTimestamp({before:Int64}) "
        "  GROUP BY entity_id, bucket) "
        "GROUP BY entity_id",
        parameters={"ids": ids, "before": before},
    )
    return {e: float(v) for e, v in res.result_rows}


def _increase(seed: float | None, readings: list[float]) -> float | None:
    """Sum of a counter's non-negative steps from `seed` through `readings`; None when
    no step was measured (never read, or only a reset) — "unknown", not "zero"."""
    total: float | None = None
    prev = seed
    for lv in readings:
        if prev is not None and lv >= prev:
            total = (total or 0.0) + lv - prev
        prev = lv
    return total


async def _power_daily(ch, pids: list[str], frm: int, tz: ZoneInfo) -> dict[str, dict[date, tuple[float, float]]]:
    """Per-entity, per-day (kWh, peak W) from the hourly power rollup. kWh(day) = Σ over
    the day's hourly buckets of avg_power_W / 1000 (each bucket is one hour, so the
    hour's average watt-value IS its watt-hours)."""
    if not pids:
        return {}
    res = await ch.query(
        "SELECT entity_id, toDate(bucket, {tz:String}) AS d, sum(avg_w) / 1000 AS kwh, max(peak_w) FROM ("
        "  SELECT entity_id, bucket, avgMerge(avg_v) AS avg_w, maxMerge(max_v) AS peak_w FROM state_history_1h "
        "  WHERE capability = 'power' AND entity_id IN {ids:Array(String)} "
        "    AND bucket >= fromUnixTimestamp({frm:Int64}) "
        "  GROUP BY entity_id, bucket) "
        "GROUP BY entity_id, d",
        parameters={"ids": pids, "frm": frm, "tz": tz.key},
    )
    out: dict[str, dict[date, tuple[float, float]]] = defaultdict(dict)
    for e, d, kwh, peak in res.result_rows:
        out[e][d] = (float(kwh), float(peak))
    return out


async def list_meters(ch, pool, saved: dict) -> list[dict]:
    """Every registered energy-metering entity + its suggested and effective role —
    the config UI. A counter no longer in the registry only carries history, so it is
    left out (roles are still suggested over ALL ids, as `query_energy` does). A
    switched-off field is flagged: an adapter may stop reading it (esphome does),
    and a role on it would then count nothing without a word."""
    ids = await _energy_entities(ch)
    exposed = {
        r["entity_id"]: r["exposed"]
        for r in await pool.fetch(
            "SELECT entity_id, exposed FROM entities WHERE entity_id = ANY($1::text[])", ids
        )
    }
    sugg = _suggest_roles(ids)
    return [
        {"entity_id": e, "suggested": sugg.get(e, "ignore"), "role": _effective(e, saved, sugg),
         "exposed": exposed[e]}
        for e in ids
        if e in exposed
    ]


async def _power_window(ch, pids: list[str], frm: int, to: int) -> dict[str, tuple[float, float]]:
    """(kWh, peak W) from the hourly power rollup over the [frm, to) unix-second window."""
    if not pids:
        return {}
    res = await ch.query(
        "SELECT entity_id, sum(avg_w) / 1000 AS kwh, max(peak_w) FROM ("
        "  SELECT entity_id, bucket, avgMerge(avg_v) AS avg_w, maxMerge(max_v) AS peak_w FROM state_history_1h "
        "  WHERE capability = 'power' AND entity_id IN {ids:Array(String)} "
        "    AND bucket >= fromUnixTimestamp({frm:Int64}) AND bucket < fromUnixTimestamp({to:Int64}) "
        "  GROUP BY entity_id, bucket) "
        "GROUP BY entity_id",
        parameters={"ids": pids, "frm": frm, "to": to},
    )
    return {e: (float(k), float(pk)) for e, k, pk in res.result_rows}


async def query_energy_hourly(ch, saved: dict, frm: int, to: int) -> dict:
    """Per-HOUR energy balance over the [frm, to) unix-second window (one local day).
    Consumption is drawn UP, production DOWN on the client; time axis is hourly."""
    ids = await _energy_entities(ch)
    sugg = _suggest_roles(ids)
    by_role: dict[str, list[str]] = defaultdict(list)
    for e in ids:
        by_role[_effective(e, saved, sugg)].append(e)
    imp, exp, sol, sub = (by_role[k] for k in ("grid_import", "grid_export", "solar", "submeter"))
    all_ids = imp + exp + sol + sub
    if not all_ids:
        return {"hours": [], "totals": {}, "submeters": []}

    res = await ch.query(
        "SELECT toUnixTimestamp(bucket) AS hs, entity_id, argMaxMerge(last_v) AS lv "
        "FROM state_history_1h "
        "WHERE capability = 'energy' AND entity_id IN {ids:Array(String)} "
        "  AND bucket >= fromUnixTimestamp({frm:Int64}) - INTERVAL 1 HOUR "
        "  AND bucket < fromUnixTimestamp({to:Int64}) "
        "GROUP BY hs, entity_id ORDER BY entity_id, hs",
        parameters={"ids": all_ids, "frm": frm, "to": to},
    )
    per: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for hs, e, lv in res.result_rows:
        per[e].append((int(hs), float(lv)))

    def hourly(entities: list[str]) -> dict[int, float]:
        agg: dict[int, float] = defaultdict(float)
        for e in entities:
            prev: float | None = None
            for hs, lv in per.get(e, []):
                if prev is not None and lv - prev >= 0:
                    agg[hs] += lv - prev
                prev = lv
        return agg

    imp_h, exp_h, sol_h = hourly(imp), hourly(exp), hourly(sol)
    hour_rows: list[dict] = []
    tot = {"import": 0.0, "export": 0.0, "production": 0.0, "consumption": 0.0, "self_consumed": 0.0}
    for hs in sorted({h for m in (imp_h, exp_h, sol_h) for h in m if h >= frm}):
        i = round(imp_h.get(hs, 0.0), 3)
        x = round(exp_h.get(hs, 0.0), 3)
        p = round(sol_h.get(hs, 0.0), 3)
        metered = hs in imp_h
        self_c = round(max(0.0, p - x), 3) if metered else None
        c = round(i - x + p, 3) if metered else None
        hour_rows.append({"ts": hs * 1000, "import": i, "export": x, "production": p,
                          "self_consumed": self_c, "consumption": c})
        if metered:
            tot["import"] += i
            tot["export"] += x
            tot["production"] += p
            tot["consumption"] += c
            tot["self_consumed"] += self_c

    totals = {k: round(v, 2) for k, v in tot.items()}
    totals["self_sufficiency"] = (
        round(100 * tot["self_consumed"] / tot["consumption"], 1) if tot["consumption"] > 0 else None
    )

    # Per-device sub-meter breakdown for the day (counter, else power — see query_energy).
    sub_devs = {_dev(e) for e in sub}
    power_by_dev: dict[str, list[str]] = defaultdict(list)
    for pe in await _power_entities(ch):
        if _dev(pe) in sub_devs:
            power_by_dev[_dev(pe)].append(pe)
    pw = await _power_window(ch, [p for ps in power_by_dev.values() for p in ps], frm, to)
    seed = await _counter_seed(ch, sub, frm)
    submeters = []
    for dev in sub_devs:
        reads = [x for e in sub if _dev(e) == dev
                 if (x := _increase(seed.get(e), [lv for hs, lv in per.get(e, []) if hs >= frm])) is not None]
        pids = power_by_dev.get(dev, [])
        kwh = _device_day_kwh(
            sum(reads) if reads else None,
            sum(pw[p][0] for p in pids if p in pw),
            max((pw[p][1] for p in pids if p in pw), default=0.0),
        )
        kwh = round(kwh, 2)
        if kwh > 0.01:
            submeters.append({"device": dev, "kwh": kwh})
    submeters.sort(key=lambda s: s["kwh"], reverse=True)

    return {"hours": hour_rows, "totals": totals, "submeters": submeters}


def _daily(per: dict[str, list[tuple[date, float]]], entities: list[str]) -> dict[date, float]:
    """day → summed kWh drawn by `entities` that day (counter increase)."""
    agg: dict[date, float] = defaultdict(float)
    for e in entities:
        prev: float | None = None
        for d, lv in per.get(e, []):
            if prev is not None and lv - prev >= 0:
                agg[d] += lv - prev
            prev = lv
    return agg


def _balance(imp_d: dict[date, float], exp_d: dict[date, float], sol_d: dict[date, float],
             start: date) -> tuple[list[dict], dict, set[date]]:
    day_rows: list[dict] = []
    tot = {"import": 0.0, "export": 0.0, "production": 0.0, "consumption": 0.0, "self_consumed": 0.0}
    metered_set: set[date] = set()
    for d in sorted({dd for m in (imp_d, exp_d, sol_d) for dd in m if dd >= start}):
        i = round(imp_d.get(d, 0.0), 2)
        x = round(exp_d.get(d, 0.0), 2)
        p = round(sol_d.get(d, 0.0), 2)
        # The BALANCE (consumption = import − export + production) is only knowable on
        # days the grid meter was reporting. Every total — production included — is
        # summed over those metered days ONLY, so production ≈ consumption ± grid and
        # the headline reads coherently (before the grid meter existed we'd have solar
        # with no consumption to compare it to). Per-day production is still emitted
        # for every day so the chart can draw the full solar trend as context.
        metered = d in imp_d
        self_c = round(max(0.0, p - x), 2) if metered else None
        c = round(i - x + p, 2) if metered else None
        day_rows.append(
            {"date": d.isoformat(), "import": i, "export": x, "production": p,
             "self_consumed": self_c, "consumption": c}
        )
        if metered:
            metered_set.add(d)
            tot["import"] += i
            tot["export"] += x
            tot["production"] += p
            tot["consumption"] += c
            tot["self_consumed"] += self_c

    totals = {k: round(v, 1) for k, v in tot.items()}
    totals["self_sufficiency"] = (
        round(100 * tot["self_consumed"] / tot["consumption"], 1) if tot["consumption"] > 0 else None
    )
    metered_dates = sorted(d.isoformat() for d in metered_set)
    totals["balance_from"] = metered_dates[0] if metered_dates else None
    totals["metered_days"] = len(metered_dates)
    return day_rows, totals, metered_set


async def _submeters(ch, sub: list[str], per: dict[str, list[tuple[date, float]]],
                     metered_set: set[date], start: date, tz: ZoneInfo) -> list[dict]:
    """Sub-meter breakdown, per DEVICE, over the same metered window so shares are
    comparable to consumption. Day by day, a counter read that day is the device's
    own integration and wins: the power sensor reports only on change, so its hourly
    average of samples overstates an intermittent load (a dryer's minutes at 2 kW
    fill the hour) and a flat-zero sensor (a SmartThings washer) reads nothing. Power
    covers a day the counter was not read (frozen, switched off, absent) or claims
    more than the device could draw in a day (`_DAY_BOUND_HOURS`).
    The remainder (consumption − Σ) is the unmetered house load (client)."""
    sub_devs = {_dev(e) for e in sub}
    power_by_dev: dict[str, list[str]] = defaultdict(list)
    for p in await _power_entities(ch):
        if _dev(p) in sub_devs:
            power_by_dev[_dev(p)].append(p)
    pw = await _power_daily(ch, [p for ps in power_by_dev.values() for p in ps],
                            _local_day_start(start, tz), tz)
    seed = await _counter_seed(ch, sub, _local_day_start(start - timedelta(days=1), tz))

    def counter_days(e: str) -> dict[date, float]:
        out: dict[date, float] = {}
        prev = seed.get(e)
        for d, lv in per.get(e, []):
            if (x := _increase(prev, [lv])) is not None:
                out[d] = x
            prev = lv
        return out

    cnt = {e: counter_days(e) for e in sub}
    submeters = []
    for dev in sub_devs:
        ents = [e for e in sub if _dev(e) == dev]
        pids = power_by_dev.get(dev, [])
        kwh = 0.0
        for d in metered_set:
            reads = [cnt[e][d] for e in ents if d in cnt[e]]
            day_pw = [pw[p][d] for p in pids if d in pw.get(p, {})]
            kwh += _device_day_kwh(sum(reads) if reads else None,
                                   sum(k for k, _ in day_pw), max((pk for _, pk in day_pw), default=0.0))
        kwh = round(kwh, 2)
        if kwh > 0.01:
            submeters.append({"device": dev, "kwh": kwh})
    submeters.sort(key=lambda s: s["kwh"], reverse=True)
    return submeters


async def query_energy(ch, days: int, saved: dict, tz: ZoneInfo) -> dict:
    """Per-day energy balance for the house over the last `days` of its local days."""
    days = max(1, min(days, MAX_DAYS))
    start = datetime.now(tz).date() - timedelta(days=days - 1)

    ids = await _energy_entities(ch)
    sugg = _suggest_roles(ids)
    by_role: dict[str, list[str]] = defaultdict(list)
    for e in ids:
        by_role[_effective(e, saved, sugg)].append(e)
    imp, exp, sol, sub = (by_role[k] for k in ("grid_import", "grid_export", "solar", "submeter"))
    all_ids = imp + exp + sol + sub
    if not all_ids:
        return {"days": [], "totals": {}, "submeters": [], "meters": {}}

    res = await ch.query(
        "SELECT toDate(bucket, {tz:String}) AS d, entity_id, argMaxMerge(last_v) AS lv "
        "FROM state_history_1d "
        "WHERE capability = 'energy' AND entity_id IN {ids:Array(String)} "
        "  AND bucket >= fromUnixTimestamp({frm:Int64}) "
        "GROUP BY d, entity_id ORDER BY entity_id, d",
        parameters={"ids": all_ids, "frm": _local_day_start(start - timedelta(days=1), tz), "tz": tz.key},
    )
    # per-entity ordered [(day, lifetime kWh)] — one extra leading day so the first
    # reported day still has a prior reading to diff against.
    per: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for d, e, lv in res.result_rows:
        per[e].append((d, float(lv)))

    day_rows, totals, metered_set = _balance(_daily(per, imp), _daily(per, exp), _daily(per, sol), start)
    return {
        "days": day_rows,
        "totals": totals,
        "submeters": await _submeters(ch, sub, per, metered_set, start, tz),
        "meters": {"import": imp, "export": exp, "solar": sol, "submeters": sub},
    }
