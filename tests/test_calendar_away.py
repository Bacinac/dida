"""calendar:away — an event in the chosen Ponistra calendar covering the moment.

A CalDAV server hands recurring events over whole, so the expansion is ours: the
tests hold the shapes Ponistra writes (all-day DATE, TZID, UNTIL in UTC on an
all-day series, EXDATE, a moved occurrence, a cancelled one).
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from dida_adapter_calendar import adapter as runtime
from dida_adapter_calendar.away import away_now
from dida_core.dav import DavError

TZ = ZoneInfo("Europe/Zagreb")


def _cal(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{e.strip()}\r\nEND:VEVENT\r\n" for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}END:VCALENDAR\r\n"


def _at(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=TZ)


def _lines(*lines: str) -> str:
    return "\r\n".join(lines)


def test_an_all_day_event_covers_its_days_and_not_the_next():
    trip = _cal(_lines("UID:t", "SUMMARY:Split", "DTSTART;VALUE=DATE:20261113",
                       "DTEND;VALUE=DATE:20261115"))
    assert away_now([trip], _at(13, 0), TZ) == ["Split"]
    assert away_now([trip], _at(14, 23), TZ) == ["Split"]
    assert away_now([trip], _at(15, 0), TZ) == []
    assert away_now([trip], _at(12, 23), TZ) == []


def test_a_timed_event_covers_its_span_in_its_own_zone():
    meeting = _cal(_lines("UID:m", "SUMMARY:Zagreb", "DTSTART;TZID=Europe/Zagreb:20261113T080000",
                          "DTEND;TZID=Europe/Zagreb:20261113T200000"))
    assert away_now([meeting], _at(13, 8), TZ) == ["Zagreb"]
    assert away_now([meeting], _at(13, 7), TZ) == []
    assert away_now([meeting], _at(13, 20), TZ) == []


def test_a_weekly_series_skips_its_exception_and_follows_a_moved_day():
    series = _cal(
        _lines("UID:w", "SUMMARY:Holiday", "DTSTART;VALUE=DATE:20261106", "DTEND;VALUE=DATE:20261107",
               "RRULE:FREQ=WEEKLY;UNTIL=20261231T225959Z", "EXDATE;VALUE=DATE:20261120"),
        _lines("UID:w", "SUMMARY:Holiday", "RECURRENCE-ID;VALUE=DATE:20261127",
               "DTSTART;VALUE=DATE:20261128", "DTEND;VALUE=DATE:20261129"),
    )
    assert away_now([series], _at(13), TZ) == ["Holiday"]
    assert away_now([series], _at(20), TZ) == [], "EXDATE"
    assert away_now([series], _at(27), TZ) == [], "moved away from the 27th"
    assert away_now([series], _at(28), TZ) == ["Holiday"], "moved to the 28th"
    assert away_now([series], _at(1, month=1).replace(year=2027), TZ) == [], "UNTIL"


def test_a_cancelled_event_and_a_cancelled_occurrence_are_not_away():
    gone = _cal(_lines("UID:c", "SUMMARY:Off", "STATUS:CANCELLED", "DTSTART;VALUE=DATE:20261113"))
    series = _cal(
        _lines("UID:d", "SUMMARY:Daily", "DTSTART;VALUE=DATE:20261110", "RRULE:FREQ=DAILY"),
        _lines("UID:d", "SUMMARY:Daily", "RECURRENCE-ID;VALUE=DATE:20261113", "STATUS:CANCELLED",
               "DTSTART;VALUE=DATE:20261113"),
    )
    assert away_now([gone], _at(13), TZ) == []
    assert away_now([series], _at(13), TZ) == []
    assert away_now([series], _at(14), TZ) == ["Daily"]


def _adapter(events, read=None):
    a = runtime.CalendarAdapter()
    a._bus = AsyncMock()
    a.status = Mock()
    a.broker = AsyncMock()
    a.broker.call.return_value = []
    a._astro_cfg = None
    a._cfg = Mock()
    a._cfg.load = AsyncMock()
    a._cfg.get = lambda key, default="": {
        "ponistra_url": "https://p.example", "ponistra_email": "ivo@example.com",
        "ponistra_password": "pw", "away_calendar": "Odsutnost"}.get(key, default)
    a._read_away = read or AsyncMock(return_value=events)
    return a


async def _published(a) -> dict:
    await asyncio.sleep(0)
    return {(u.entity_id, u.capability): u.value
            for u in (c.args[0] for c in a._bus.publish_state.await_args_list)}


async def test_the_adapter_publishes_away_and_who_from_the_calendar():
    now = datetime.now(TZ).strftime("%Y%m%d")
    a = _adapter([_cal(_lines("UID:t", "SUMMARY:Split", f"DTSTART;VALUE=DATE:{now}"))])
    await a._evaluate()
    assert await _published(a) == {("calendar:away", "binary"): True, ("calendar:away", "text"): "Split"}
    a.status.ok.assert_called_with("0 schedules, away from Ponistra")
    a._bus.publish_entity.assert_awaited()


async def test_a_failed_read_keeps_the_last_good_calendar_and_turns_the_badge_red():
    now = datetime.now(TZ).strftime("%Y%m%d")
    a = _adapter([_cal(_lines("UID:t", "SUMMARY:Split", f"DTSTART;VALUE=DATE:{now}"))])
    await a._evaluate()
    a._read_away = AsyncMock(side_effect=DavError("the server refused the sign-in"))
    a._away_tried -= runtime.AWAY_REFRESH
    await a._evaluate()
    assert a._away_events, "the last good read is kept"
    a.status.error.assert_called_with("away calendar: the server refused the sign-in")
    await a._evaluate()
    assert a._read_away.await_count == 1, "a refused sign-in is not retried every pass"


async def test_no_away_calendar_means_no_away_entity():
    a = _adapter([])
    a._cfg.get = lambda key, default="": default
    await a._evaluate()
    assert await _published(a) == {}
    a._read_away.assert_not_awaited()
