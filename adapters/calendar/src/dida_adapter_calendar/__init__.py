"""DIDA calendar adapter.

DIDA's light "beat" scheduler — the native take on Home Assistant's
local_calendar. A schedule is NOT a special subsystem: it is an ordinary entity
whose owner is DIDA itself. The adapter reads user-created definitions from the
`schedules` table and turns each into a first-class `schedule_active` boolean
entity on the bus.

A beat marks WHICH DAYS match — there is no time-of-day. A schedule is active on
day D when D ≥ start_date, and (if end_date is set) D falls inside the yearly
window from start_date's month-day to end_date's month-day, and (D − day_offset)
matches the recurrence pattern: daily · weekly (selected weekdays) · monthly
(day-of-month or Nth weekday) · yearly (start_date's month-day) · once (exactly
start_date). `day_offset` shifts the active day off the pattern day (e.g. fire
the day BEFORE the 3rd Friday → offset −1).

Definitions live in the table; the live value lives in current_state (single
source of truth). Usable in automations as a condition, or as a trigger on the
edge when it flips — automations add the time (a time trigger + this boolean as
a condition), exactly like any device.
"""

from __future__ import annotations

from dida_adapter_calendar.adapter import CalendarAdapter

__all__ = ["CalendarAdapter"]
