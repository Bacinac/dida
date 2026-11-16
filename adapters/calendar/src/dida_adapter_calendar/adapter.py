from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from time import monotonic, time_ns
from zoneinfo import ZoneInfo

from dida_core import AdapterConfig, Bus, EntityInfo, StateUpdate
from dida_core.dav import Dav, DavError
from dida_core.schedules import next_occurrence, schedule_active
from home_core.tasks import spawn

from dida_adapter_calendar.away import away_now

log = logging.getLogger("dida.adapter.calendar")

NAMESPACE = "calendar"
AWAY = f"{NAMESPACE}:away"
RELOAD_INTERVAL = 5.0
AWAY_REFRESH = 300.0


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
          daily   → every interval days
          weekly  → the selected weekdays every interval weeks
          monthly → a day-of-month or Nth weekday every interval months
          yearly  → start_date's month-day every interval years
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
        self._next: dict[str, str] = {}  # dedupe — republish only when the date moves
        self._cfg: AdapterConfig | None = None
        self._away_events: list[str] = []
        self._away_tried = -AWAY_REFRESH  # monotonic time of the last read, good or not
        self._away_with: tuple = ()  # the settings that read was made with
        self._away_error = ""
        self._away_last: dict[str, object] = {}

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._astro_cfg = AdapterConfig("astro", self.broker)
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
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
                active = bool(row["enabled"]) and schedule_active(today, params)
                nxt = (next_occurrence(today, params) or "") if row["enabled"] else ""
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
            if self._next.get(eid) != nxt:
                self._next[eid] = nxt
                self._pub_next(eid, nxt, name)
        detail = f"{live} schedule{'s' if live != 1 else ''}"
        if await self._away(await self._resolve_tz()):
            detail += ", away from Ponistra"
        if self._away_error:
            self.status.error(f"away calendar: {self._away_error}")
        else:
            self.status.ok(detail)

    async def _away(self, tz: ZoneInfo) -> bool:
        """`calendar:away` from the calendar named in the settings. The calendar is read
        every few minutes and the moment judged on every pass, so an event starting
        between two reads still flips on time. A failed read keeps the last good one
        and turns the badge red."""
        if self._cfg is None:
            return False
        await self._cfg.load()
        if not self._cfg.get("away_calendar"):
            self._away_error = ""
            return False
        settings = (self._cfg.get("ponistra_url"), self._cfg.get("ponistra_email"),
                    self._cfg.get("ponistra_password"), self._cfg.get("away_calendar"))
        now = datetime.now(tz)
        if settings != self._away_with or monotonic() - self._away_tried >= AWAY_REFRESH:
            if settings != self._away_with:
                self._away_events = []
            self._away_with, self._away_tried = settings, monotonic()
            try:
                self._away_events = await self._read_away(settings, now, tz)
                self._away_error = ""
            except DavError as exc:
                log.warning("calendar: away calendar not read: %s", exc)
                self._away_error = str(exc)
        names = away_now(self._away_events, now, tz)
        self._pub_away("binary", bool(names))
        self._pub_away("text", "; ".join(n for n in names if n))
        return True

    @staticmethod
    async def _read_away(settings: tuple, now: datetime, tz: ZoneInfo) -> list[str]:
        url, email, password, wanted = settings
        async with Dav(url, email, password) as dav:
            calendars = await dav.collections("caldav")
            found = next((c for c in calendars if c.name == wanted), None)
            if found is None:
                raise DavError(f"no calendar named {wanted!r}; the sign-in reaches "
                               + ", ".join(repr(c.name) for c in calendars))
            day = datetime.combine(now.date(), time(), tz)
            return await dav.events(found, day - timedelta(days=1), day + timedelta(days=2))

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

    def _announce(self, eid: str, name: str) -> None:
        if eid in self._known or self._bus is None:
            return
        self._known.add(eid)
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=eid, adapter=NAMESPACE,
            capabilities=["schedule_active", "next_occurrence"], name=name, device=eid,
        )), log=log, name=f"announce {eid}")

    def _pub_away(self, capability: str, value) -> None:
        if self._bus is None or self._away_last.get(capability) == value:
            return
        if not self._away_last:
            self._announce_away()
        self._away_last[capability] = value
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=AWAY, capability=capability, value=value, adapter=NAMESPACE,
            ts_ns=time_ns(), name="Away", device=AWAY,
        )), log=log, name=f"publish {AWAY}.{capability}")

    def _announce_away(self) -> None:
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=AWAY, adapter=NAMESPACE, capabilities=["binary", "text"], name="Away",
            device=AWAY, device_name="Away", device_type="sensor",
        )), log=log, name=f"announce {AWAY}")

    def _pub(self, eid: str, value: bool, name: str) -> None:
        if self._bus is None:
            return
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=eid, capability="schedule_active", value=value, adapter=NAMESPACE,
            ts_ns=time_ns(), name=name, device=eid,
        )), log=log, name=f"publish {eid}.schedule_active")

    def _pub_next(self, eid: str, iso_date: str, name: str) -> None:
        if self._bus is None:
            return
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=eid, capability="next_occurrence", value=iso_date, adapter=NAMESPACE,
            ts_ns=time_ns(), name=name, device=eid,
        )), log=log, name=f"publish {eid}.next_occurrence")
