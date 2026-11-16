"""Who is away right now, read from a calendar kept for it.

An event in that calendar covering the moment means the household is away: an
all-day event covers its local days, a timed one its span. A CalDAV server hands a
recurring event over whole (master plus the occurrences it moved or cancelled), so
the recurrence is expanded here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar, vRecur

__all__ = ["away_now"]

Moment = date | datetime


def _local(value: Moment, tz: ZoneInfo) -> Moment:
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return value


def _end(event, start: Moment, tz: ZoneInfo) -> Moment:
    if (given := event.get("DTEND")) is not None:
        return _local(given.dt, tz)
    if (duration := event.get("DURATION")) is not None:
        return start + duration.dt
    return start + timedelta(days=1) if not isinstance(start, datetime) else start


def _moments(prop, tz: ZoneInfo) -> list[Moment]:
    if prop is None:
        return []
    out = []
    for each in prop if isinstance(prop, list) else [prop]:
        out += [_local(v.dt, tz) for v in each.dts if isinstance(v.dt, date)]
    return out


def _rule(recur: vRecur, start: Moment, tz: ZoneInfo):
    """The RRULE as dateutil reads it. UNTIL has to match DTSTART in kind (dateutil
    refuses a UTC UNTIL on a floating start and the other way round), and an all-day
    series runs on naive midnights."""
    rule = vRecur(recur)
    if "UNTIL" in rule:
        until = rule["UNTIL"][0]
        if isinstance(start, datetime):
            if not isinstance(until, datetime):
                until = datetime.combine(until, time.max, tz)
            rule["UNTIL"] = [_local(until, tz).astimezone(ZoneInfo("UTC"))]
        else:
            if isinstance(until, datetime):
                until = _local(until, tz).date()
            rule["UNTIL"] = [datetime.combine(until, time.max).replace(microsecond=0)]
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time())
    return rrulestr(rule.to_ical().decode(), dtstart=anchor)


def _starts(event, start: Moment, length: timedelta, now: datetime, tz: ZoneInfo) -> list[Moment]:
    """The occurrence starts that could still be running at `now`."""
    timed = isinstance(start, datetime)
    point = now if timed else datetime.combine(now.date(), time())
    found: list[Moment] = [start]
    if (recur := event.get("RRULE")) is not None:
        hits = _rule(recur, start, tz).between(point - length, point, inc=True)
        found = [hit if timed else hit.date() for hit in hits]
    found += _moments(event.get("RDATE"), tz)
    gone = set(_moments(event.get("EXDATE"), tz))
    return [each for each in found if each not in gone]


def _covers(start: Moment, end: Moment, now: datetime) -> bool:
    if isinstance(start, datetime):
        end = end if isinstance(end, datetime) else datetime.combine(end, time(), now.tzinfo)
        return start <= now < end
    end = end.date() if isinstance(end, datetime) else end
    return start <= now.date() < max(end, start + timedelta(days=1))


def away_now(objects: list[str], now: datetime, tz: ZoneInfo) -> list[str]:
    """The summaries of the events covering `now`, sorted, each once."""
    now = now.astimezone(tz)
    found: set[str] = set()
    for text in objects:
        masters, moved = {}, {}
        for event in Calendar.from_ical(text).walk("VEVENT"):
            uid = str(event.get("UID", ""))
            if event.get("RECURRENCE-ID") is not None:
                moved.setdefault(uid, []).append(event)
            else:
                masters[uid] = event
        for uid, event in masters.items():
            if str(event.get("STATUS", "")).upper() == "CANCELLED" or event.get("DTSTART") is None:
                continue
            start = _local(event["DTSTART"].dt, tz)
            end = _end(event, start, tz)
            length = (end - start) if type(end) is type(start) else timedelta(days=1)
            replaced = {_local(each["RECURRENCE-ID"].dt, tz) for each in moved.get(uid, [])}
            for each in _starts(event, start, length, now, tz):
                if each not in replaced and _covers(each, each + length, now):
                    found.add(str(event.get("SUMMARY", "")).strip())
        for event in (each for group in moved.values() for each in group):
            if str(event.get("STATUS", "")).upper() == "CANCELLED" or event.get("DTSTART") is None:
                continue
            start = _local(event["DTSTART"].dt, tz)
            if _covers(start, _end(event, start, tz), now):
                found.add(str(event.get("SUMMARY", "")).strip())
    return sorted(found)
