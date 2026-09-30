"""Per-entity cumulatives — the numbers a chart can't answer at a glance.

A graph shows a shape; this answers "how much" and "how often". Three kinds, each
derived from the same `state_history` firehose, none of them stored separately:

  * CONSUMED — a monotonic counter's increase over a period (kWh this month).
  * RUNTIME  — how long a boolean spent ON, integrated over its change points.
  * CYCLES   — how many times it turned on.

Derived on read, deliberately. A stored daily rollup would need backfilling, would
disagree with the raw data after any gap, and buys nothing at a house's volume: a
month of one entity's change points is hundreds of rows, not millions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

log = logging.getLogger("dida.api.stats")

MAX_DAYS = 400          # a year plus change, so year-over-year comparisons work
MAX_POINTS = 200_000    # bound the transfer for a chatty entity

#: Capabilities that are monotonic counters — their VALUE is a running total, so
#: the number worth reporting is how much it grew, never the reading itself.
COUNTER_CAPS = ("energy", "water", "gas", "rain")

#: Capabilities whose ON time is worth measuring. `on_off` is the obvious one;
#: motion/occupancy answer "how much of the day was this room occupied", which is
#: the same integral over a different meaning.
RUNTIME_CAPS = ("on_off", "motion", "occupancy")


async def _counter_growth(ch, entity_id: str, capability: str, hours: int) -> dict | None:
    """How much a monotonic counter grew, aggregated HOURLY by ClickHouse.

    Not row-by-row. A meter reporting every few seconds writes millions of rows a
    month; the first version pulled them raw under a LIMIT and, on the house's own
    import meter, hit that cap exactly — returning 23 kWh for a month whose real
    growth was 220. A silent truncation that produces a confident wrong number is
    worse than no number at all, and a counter never needed the raw stream anyway:
    its growth is the sum of per-bucket increases, and the bucket count is bounded
    by the PERIOD (≤ ~9 600 hours even at the 400-day maximum), not by how chatty
    the device is.

    Resets (a meter rebooting to zero) are skipped rather than counted as a huge
    negative — the same rule the house energy page uses, for the same reason.
    """
    res = await ch.query(
        "SELECT toUnixTimestamp(toStartOfHour(ts)) AS b, max(value_num) AS v "
        "FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND ts >= now() - toIntervalHour({h:UInt32}) AND value_num IS NOT NULL "
        "GROUP BY b ORDER BY b",
        parameters={"e": entity_id, "c": capability, "h": hours},
    )
    rows = res.result_rows
    if len(rows) < 2:
        return None
    total, prev = 0.0, None
    for _b, v in rows:
        v = float(v)
        if prev is not None and v >= prev:
            total += v - prev
        prev = v
    return {"capability": capability, "total": round(total, 3), "hours_seen": len(rows)}


async def _runtime(ch, entity_id: str, capability: str, hours: int) -> dict | None:
    """Hours spent ON and how many times it came on.

    The stream is read RAW and collapsed to change points HERE, not in ClickHouse.
    The obvious query — `lagInFrame(value_num) OVER (ORDER BY ts)` — is wrong:
    without a PARTITION, ClickHouse evaluates the window per BLOCK, so the first
    row of every block sees a phantom previous value of 0 and the last sees a
    phantom next of 0. Measured against production: the same integral came out as
    175 015 hours inside a 720-hour window. A house's volumes make the trade easy —
    the chattiest entity here is ~32 000 rows a month, which is a rounding error to
    transfer and unambiguously correct to fold in order.

    The last interval runs to NOW, not to the last row — a light that has been on
    for three hours has been on for three hours, and stopping the clock at its last
    report would under-count exactly the state you are asking about.
    """
    # Newest-first, then reversed. A boolean genuinely needs its transitions, so
    # this one IS bounded by a row cap — but if the cap bites, the slice we keep
    # must be the RECENT end (a true statement about a shorter window) rather than
    # the oldest, and the answer says so instead of quietly describing a period
    # nobody asked about.
    res = await ch.query(
        "SELECT toUnixTimestamp(ts) AS s, value_num FROM state_history "
        "WHERE entity_id = {e:String} AND capability = {c:String} "
        "  AND ts >= now() - toIntervalHour({h:UInt32}) AND value_num IS NOT NULL "
        "ORDER BY ts DESC LIMIT {lim:UInt32}",
        parameters={"e": entity_id, "c": capability, "h": hours, "lim": MAX_POINTS},
    )
    raw = res.result_rows
    truncated = len(raw) >= MAX_POINTS
    if truncated:
        log.warning("stats: %s/%s hit the %d-row cap — reporting the recent window only",
                    entity_id, capability, MAX_POINTS)
    rows: list[tuple[int, float]] = []
    for s, v in reversed(raw):
        v = float(v)
        if rows and rows[-1][1] == v:
            continue  # a repeat report of the state it is already in, not a change
        rows.append((int(s), v))
    if not rows:
        return None
    now = int(datetime.now(tz=UTC).timestamp())
    on_s, cycles = 0.0, 0
    for i, (s, v) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else now
        if v:
            on_s += end - s
            cycles += 1
    window_s = min(hours * 3600, now - rows[0][0]) or 1
    return {
        "capability": capability,
        "hours_on": round(on_s / 3600, 2),
        "cycles": cycles,
        # Share of the OBSERVED window, not of the requested one: an entity that
        # only started reporting yesterday would otherwise read as 3 % duty for a
        # month it was on the whole time.
        "duty": round(on_s / window_s, 4),
        "transitions": len(rows),
        # Hours the answer actually covers. Equal to the period asked for unless
        # the cap bit, in which case saying so is the difference between a shorter
        # true answer and a wrong one.
        "window_h": round(window_s / 3600, 1),
        "truncated": truncated,
    }


async def entity_stats(ch, entity_id: str, capabilities: list[str], days: int) -> dict:
    """Cumulatives for whichever of `capabilities` have one worth reporting.

    Silent about the rest: a temperature sensor has no total and no runtime, and
    inventing a zero for it would be a claim, not an absence."""
    days = max(1, min(days, MAX_DAYS))
    hours = days * 24
    out: dict = {"days": days, "counters": [], "runtime": []}
    for cap in capabilities:
        try:
            if cap in COUNTER_CAPS:
                row = await _counter_growth(ch, entity_id, cap, hours)
                if row:
                    out["counters"].append(row)
            elif cap in RUNTIME_CAPS:
                row = await _runtime(ch, entity_id, cap, hours)
                if row:
                    out["runtime"].append(row)
        except Exception:
            # One unhappy capability must not take the whole panel down; the rest
            # of the numbers are still true.
            log.exception("stats: %s/%s failed", entity_id, cap)
    return out
