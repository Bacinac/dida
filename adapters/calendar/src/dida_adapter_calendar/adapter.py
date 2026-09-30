from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dida_core import AdapterConfig, Bus, EntityInfo, StateUpdate
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.calendar")

NAMESPACE = "calendar"
RELOAD_INTERVAL = 5.0


def _parse_ymd(s) -> date | None:
    try:
        y, m, d = (int(x) for x in str(s).split("-"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        return None


def _in_yearly_window(today: date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """True if today's (month, day) is within the yearly window [start, end] —
    repeats every year (year is ignored). end < start wraps the New Year."""
    md = (today.month, today.day)
    if start <= end:
        return start <= md <= end
    return md >= start or md <= end


class CalendarAdapter:
    """Owns `calendar:*` schedule entities defined in the `schedules` table —
    DIDA's light scheduler. A source adapter (no protocol): it computes, per day,
    whether each schedule is ACTIVE and publishes a `schedule_active` boolean.
    There is no time-of-day — a beat marks WHICH DAYS match; automations add the
    time (a time trigger + this boolean as a condition).

    A schedule is active on day D when:
      * D ≥ start_date (the "from"), and
      * if end_date is set, D falls inside the yearly window from start_date's
        month-day to end_date's month-day (so "daily 05-01 → 10-01" recurs every
        year while enabled), and
      * (D − day_offset) matches the recurrence pattern:
          daily   → every day
          weekly  → the selected weekdays
          monthly → a day-of-month, or the Nth weekday (e.g. 3rd Friday)
          yearly  → start_date's month-day
          once    → exactly start_date
    `day_offset` shifts the active day off the pattern day (e.g. notify the day
    BEFORE the 3rd Friday → offset −1). Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        # tz is read live from the astro adapter's config (the single place tz is
        # set) — calendar's container has no DIDA_TZ wired. These cache the resolved
        # zone between evaluations; Europe/Zagreb is the ultimate fallback.
        self._astro_cfg: AdapterConfig | None = None
        self._tz_name = "Europe/Zagreb"
        self._tz = ZoneInfo("Europe/Zagreb")
        self._known: set[str] = set()
        self._active: dict[str, bool] = {}
        self._next: dict[str, str | None] = {}  # dedupe — republish only when the date moves

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._astro_cfg = AdapterConfig("astro", self.broker)
        log.info("calendar adapter up — owning calendar:* schedules")
        self.status.ok("0 schedules")
        while True:
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("calendar: evaluation failed")
                self.status.error(str(exc) or "evaluation error")
            await asyncio.sleep(RELOAD_INTERVAL)

    async def handle_command(self, command) -> None:
        # Read-only source: schedule state is computed, never commanded.
        return

    async def stop(self) -> None:
        self._bus = None

    async def _evaluate(self) -> None:
        rows = await self.broker.call("schedules")
        today = datetime.now(await self._resolve_tz()).date()
        live = 0
        for row in rows:
            name = row["name"]
            # Entity id is the schedule's STABLE DB id, not slug(name) — so a
            # rename never breaks an automation that references this schedule.
            eid = f"{NAMESPACE}:{row['id']}"
            # Announce even when disabled: skipping disabled rows BEFORE publishing
            # froze schedule_active at its last value forever. Fold `enabled` into
            # `active` so the disable edge publishes false through the dedupe path.
            self._announce(eid, name)
            try:
                # Parse params INSIDE the per-row try: one poison params row must
                # isolate to itself (log + skip), not abort the whole pass and
                # freeze every other schedule.
                params = row["params"]
                if isinstance(params, str):
                    params = json.loads(params or "{}")
                if not isinstance(params, dict):
                    params = {}
                active = bool(row["enabled"]) and self._beat_active(today, params)
            except Exception as exc:
                log.warning("calendar %s: bad config (%s)", name, exc, exc_info=True)
                continue
            if row["enabled"]:
                live += 1
            if self._active.get(eid) != active:
                self._active[eid] = active
                self._pub(eid, active, name)
            # "When is the next bin collection" is the question people actually ask;
            # schedule_active only answers "is it today". Disabled schedules publish
            # no date — an announced date for a rule that will not fire is a lie.
            nxt = self._next_occurrence(today, params) if row["enabled"] else None
            if self._next.get(eid) != nxt:
                self._next[eid] = nxt
                if nxt is not None:
                    self._pub_next(eid, nxt, name)
        self.status.ok(f"{live} schedule{'s' if live != 1 else ''}")

    async def _resolve_tz(self) -> ZoneInfo:
        """Timezone from the astro adapter's config (`astro.tz`) — the single place
        tz is configured (calendar's container has no DIDA_TZ). Read live so an edit
        applies without a restart; Europe/Zagreb is the ultimate fallback."""
        name = "Europe/Zagreb"
        if self._astro_cfg is not None:
            try:
                await self._astro_cfg.load()
                name = self._astro_cfg.get("tz", "Europe/Zagreb") or "Europe/Zagreb"
            except Exception as exc:
                log.warning("calendar: could not read astro.tz (%s); keeping %s", exc, self._tz_name, exc_info=True)
                return self._tz
        if name != self._tz_name:
            try:
                self._tz = ZoneInfo(name)
                self._tz_name = name
            except Exception as exc:
                log.warning("calendar: invalid tz %r (%s); keeping %s", name, exc, self._tz_name, exc_info=True)
        return self._tz

    def _next_occurrence(self, today: date, params: dict, horizon: int = 400) -> str | None:
        """ISO date of the next day this schedule is active, today included.

        A forward scan over the SAME `_beat_active` the live value uses — so the
        announced date and the day the schedule actually fires can never disagree.
        The horizon covers a yearly recurrence with room to spare; past it there is
        genuinely nothing to announce (an expired end_date), and None is the honest
        answer rather than a wrong date.
        """
        for delta in range(horizon):
            day = today + timedelta(days=delta)
            if self._beat_active(day, params):
                return day.isoformat()
        return None

    def _beat_active(self, today: date, params: dict) -> bool:
        offset = int(params.get("day_offset", 0) or 0)
        sd = _parse_ymd(params.get("start_date")) or today
        anchor = today - timedelta(days=offset)  # the recurrence day this active-day maps to
        if anchor < sd:
            return False
        ed = _parse_ymd(params.get("end_date"))
        if ed is not None and not _in_yearly_window(today, (sd.month, sd.day), (ed.month, ed.day)):
            return False
        return self._recurrence_matches(anchor, params, sd)

    def _recurrence_matches(self, d: date, params: dict, sd: date) -> bool:
        rt = str(params.get("recurrence_type", "daily"))
        if rt == "daily":
            return True
        if rt == "once":
            return d == sd
        if rt == "weekly":
            days = [int(x) for x in (params.get("weekdays") or [])]
            return d.weekday() in days  # Mon=0 … Sun=6, matches the UI
        if rt == "monthly":
            if str(params.get("monthly_mode", "day")) == "day":
                return d.day == int(params.get("monthly_day", sd.day))
            wd = int(params.get("monthly_weekday", 0))
            occ = int(params.get("week_occurrence", 1))
            return d.weekday() == wd and math.ceil(d.day / 7) == occ
        if rt == "yearly":
            return (d.month, d.day) == (sd.month, sd.day)
        return False

    def _announce(self, eid: str, name: str) -> None:
        if eid in self._known or self._bus is None:
            return
        self._known.add(eid)
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=eid, adapter=NAMESPACE,
            capabilities=["schedule_active", "next_occurrence"], name=name, device=eid,
        )), log=log, name=f"announce {eid}")

    def _pub(self, eid: str, value: bool, name: str) -> None:
        if self._bus is None:
            return
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=eid, capability="schedule_active", value=value, adapter=NAMESPACE,
            ts_ns=time.time_ns(), name=name, device=eid,
        )), log=log, name=f"publish {eid}.schedule_active")

    def _pub_next(self, eid: str, iso_date: str, name: str) -> None:
        if self._bus is None:
            return
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=eid, capability="next_occurrence", value=iso_date, adapter=NAMESPACE,
            ts_ns=time.time_ns(), name=name, device=eid,
        )), log=log, name=f"publish {eid}.next_occurrence")
