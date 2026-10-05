from __future__ import annotations

import calendar
import math
import re
from collections.abc import Iterator
from datetime import date, timedelta

RECURRENCES = {"once", "daily", "weekly", "monthly", "yearly"}
_YMD = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")


def _date(value: object) -> date | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not _YMD.fullmatch(text):
        raise ValueError("date must be YYYY-MM-DD")
    return date(*(int(part) for part in text.split("-")))


def interval_for(params: dict) -> int:
    interval = params.get("interval", 1)
    if type(interval) is not int or interval < 1:
        raise ValueError("interval must be an integer greater than zero")
    if interval > 1 and _date(params.get("start_date")) is None:
        raise ValueError("start_date is required for a repeating interval")
    return interval


def validate_pattern(params: dict) -> None:
    if params.get("recurrence_type") not in RECURRENCES:
        raise ValueError(f"recurrence_type must be one of {sorted(RECURRENCES)}")
    interval_for(params)
    for key in ("start_date", "end_date"):
        try:
            _date(params.get(key))
        except ValueError as exc:
            raise ValueError(f"{key} is not a real YYYY-MM-DD date") from exc
    if type(params.get("day_offset", 0)) is not int:
        raise ValueError("day_offset must be an integer (days)")
    weekdays = params.get("weekdays") or []
    if not isinstance(weekdays, list) or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays):
        raise ValueError("weekdays must be a list of 0..6 (Mon..Sun)")


def in_yearly_window(today: date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    md = (today.month, today.day)
    return start <= md <= end if start <= end else md >= start or md <= end


def recurrence_matches(day: date, params: dict, start: date) -> bool:
    interval = interval_for(params)
    recurrence = params.get("recurrence_type", "daily")
    if recurrence == "once":
        return day == start
    if recurrence == "daily":
        return (day - start).days % interval == 0
    if recurrence == "weekly":
        weeks = ((day - timedelta(days=day.weekday())) - (start - timedelta(days=start.weekday()))).days // 7
        return weeks % interval == 0 and day.weekday() in (params.get("weekdays") or [])
    if recurrence == "monthly":
        months = (day.year - start.year) * 12 + day.month - start.month
        if months % interval:
            return False
        if params.get("monthly_mode", "day") == "day":
            return day.day == int(params.get("monthly_day", start.day))
        return day.weekday() == int(params.get("monthly_weekday", 0)) and math.ceil(day.day / 7) == int(params.get("week_occurrence", 1))
    if recurrence == "yearly":
        return (day.year - start.year) % interval == 0 and (day.month, day.day) == (start.month, start.day)
    return False


def schedule_active(today: date, params: dict) -> bool:
    start = _date(params.get("start_date")) or today
    anchor = today - timedelta(days=int(params.get("day_offset", 0) or 0))
    if anchor < start:
        return False
    end = _date(params.get("end_date"))
    if end is not None and not in_yearly_window(today, (start.month, start.day), (end.month, end.day)):
        return False
    return recurrence_matches(anchor, params, start)


def _candidates(probe: date, start: date, params: dict) -> Iterator[date]:
    interval = interval_for(params)
    recurrence = params.get("recurrence_type", "daily")
    probe = max(probe, start)
    if recurrence == "once":
        if start >= probe:
            yield start
        return
    periods = {"daily": 146097, "weekly": 20871, "monthly": 4800, "yearly": 400}
    if recurrence not in periods:
        return
    cycles = periods[recurrence] // math.gcd(periods[recurrence], interval)
    if recurrence in ("daily", "weekly"):
        step = interval * (7 if recurrence == "weekly" else 1)
        base = start.toordinal() - (start.weekday() if recurrence == "weekly" else 0)
        first = max(0, (probe.toordinal() - base) // step)
        days = sorted(set(params.get("weekdays") or [])) if recurrence == "weekly" else [0]
        for cycle in range(first, first + cycles + 1):
            ordinal = base + cycle * step
            if ordinal > date.max.toordinal():
                return
            for weekday in days:
                candidate = ordinal + weekday
                if probe.toordinal() <= candidate <= date.max.toordinal():
                    yield date.fromordinal(candidate)
        return
    base = start.year * 12 + start.month - 1 if recurrence == "monthly" else start.year
    current = probe.year * 12 + probe.month - 1 if recurrence == "monthly" else probe.year
    first = max(0, (current - base) // interval)
    for cycle in range(first, first + cycles + 1):
        point = base + cycle * interval
        year, month = divmod(point, 12) if recurrence == "monthly" else (point, start.month - 1)
        month += 1
        if year > date.max.year:
            return
        day = start.day
        if recurrence == "monthly":
            day = int(params.get("monthly_day", start.day))
            if params.get("monthly_mode", "day") == "weekday":
                day = 1 + (int(params.get("monthly_weekday", 0)) - date(year, month, 1).weekday()) % 7
                day += 7 * (int(params.get("week_occurrence", 1)) - 1)
        if 1 <= day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= probe:
                yield candidate


def next_occurrence(today: date, params: dict) -> str | None:
    start = _date(params.get("start_date")) or today
    offset = int(params.get("day_offset", 0) or 0)
    ordinal = today.toordinal() - offset
    if ordinal > date.max.toordinal():
        return None
    probe = date.fromordinal(max(1, ordinal))
    for candidate in _candidates(probe, start, params):
        active_day = candidate.toordinal() + offset
        if active_day > date.max.toordinal():
            return None
        if active_day < today.toordinal():
            continue
        active = date.fromordinal(active_day)
        if schedule_active(active, params):
            return active.isoformat()
    return None
