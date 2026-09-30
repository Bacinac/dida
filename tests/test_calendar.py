"""Calendar schedule recurrence-engine tests: the logic that decides WHICH DAYS a
light schedule is active, where an off-by-one silently mis-fires real automations.

Run inside the calendar adapter image (dida_adapter_calendar installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-calendar:latest \
      -c "export PYTHONPATH=/w/core/src:/w/adapters/calendar/src; \
          python -m pytest tests/test_calendar.py"

The three engine functions are deterministic — they take the day as an argument
(no now()), so every case here pins a fixed date and asserts the real output:
  * _in_yearly_window  — the year-agnostic [start, end] month-day window, including
    the end < start wrap across New Year.
  * _beat_active       — start_date gate + end_date window + day_offset shift, then
    the recurrence match. Reads params exactly as the `schedules` table stores them.
  * _recurrence_matches — daily / weekly / monthly(day) / monthly(Nth weekday,
    via math.ceil(day/7)) / yearly / once.
"""
from datetime import date

from dida_adapter_calendar.adapter import CalendarAdapter, _in_yearly_window

# _beat_active / _recurrence_matches touch no I/O (no bus, no pool) — they're pure
# methods, so a bare instance is enough to drive them.
_ADAPTER = CalendarAdapter()


def _beat(today: date, **params) -> bool:
    return _ADAPTER._beat_active(today, params)


def _matches(d: date, sd: date, **params) -> bool:
    return _ADAPTER._recurrence_matches(d, params, sd)


# --- _in_yearly_window -------------------------------------------------------
def test_yearly_window_non_wrapping():
    start, end = (5, 1), (10, 1)  # "daily 05-01 -> 10-01", recurs every summer
    assert _in_yearly_window(date(2024, 7, 15), start, end) is True, "mid-window July is inside"
    assert _in_yearly_window(date(2024, 5, 1), start, end) is True, "start boundary is inclusive"
    assert _in_yearly_window(date(2024, 10, 1), start, end) is True, "end boundary is inclusive"
    assert _in_yearly_window(date(2024, 3, 1), start, end) is False, "before the window -> out"
    assert _in_yearly_window(date(2024, 11, 1), start, end) is False, "after the window -> out"


def test_yearly_window_wraps_new_year():
    start, end = (12, 20), (1, 6)  # end < start -> the window straddles Jan 1
    assert _in_yearly_window(date(2024, 12, 25), start, end) is True, "Dec 25 is in the pre-NY leg"
    assert _in_yearly_window(date(2025, 1, 2), start, end) is True, "Jan 2 is in the post-NY leg"
    assert _in_yearly_window(date(2024, 12, 20), start, end) is True, "start boundary inclusive"
    assert _in_yearly_window(date(2025, 1, 6), start, end) is True, "end boundary inclusive"
    assert _in_yearly_window(date(2025, 1, 10), start, end) is False, "Jan 10 is past the window"
    assert _in_yearly_window(date(2024, 12, 19), start, end) is False, "Dec 19 is before the window"


# --- _beat_active: start gate + end_date window ------------------------------
def test_beat_active_before_start_is_inactive():
    cfg = dict(recurrence_type="daily", start_date="2024-06-01")
    assert _beat(date(2024, 5, 31), **cfg) is False, "the day before start_date is inactive"
    assert _beat(date(2024, 6, 1), **cfg) is True, "start_date itself is active (daily)"


def test_beat_active_summer_window_non_wrapping():
    cfg = dict(recurrence_type="daily", start_date="2024-05-01", end_date="2024-10-01")
    assert _beat(date(2024, 7, 15), **cfg) is True, "mid-summer is inside 05-01..10-01"
    assert _beat(date(2024, 12, 1), **cfg) is False, "December is outside the summer window"


def test_beat_active_end_date_window_wraps_new_year():
    # Holiday lights fenced to Dec 20 -> Jan 6: the end_date < start_date wrap must
    # keep the schedule active across the year boundary, not blank it on Jan 1.
    cfg = dict(recurrence_type="daily", start_date="2024-12-20", end_date="2025-01-06")
    assert _beat(date(2024, 12, 25), **cfg) is True, "Dec 25 is inside the holiday window"
    assert _beat(date(2025, 1, 2), **cfg) is True, "Jan 2 is inside the wrapped window"
    assert _beat(date(2025, 1, 10), **cfg) is False, "Jan 10 is past the window -> off"


# --- _beat_active: day_offset shift (both directions) ------------------------
def test_day_offset_shifts_active_day_backward():
    # "Fire the day BEFORE the pattern day" = offset -1. The engine maps an active
    # day D to the recurrence anchor D - offset = D + 1, so a monthly-day-15 pattern
    # lands its beat on the 14th.
    cfg = dict(recurrence_type="monthly", monthly_mode="day", monthly_day=15,
               start_date="2024-01-01", day_offset=-1)
    assert _beat(date(2024, 6, 14), **cfg) is True, "the 14th maps to the 15th -> active"
    assert _beat(date(2024, 6, 15), **cfg) is False, "the 15th itself maps to the 16th -> inactive"
    assert _beat(date(2024, 6, 13), **cfg) is False, "the 13th maps to the 14th -> inactive"


def test_day_offset_shifts_active_day_forward():
    # offset +1 on a one-shot: anchor = D - 1, so a once@2024-03-10 schedule fires
    # on the 11th, not on the 10th.
    cfg = dict(recurrence_type="once", start_date="2024-03-10", day_offset=1)
    assert _beat(date(2024, 3, 11), **cfg) is True, "the 11th maps back to the 10th -> active"
    assert _beat(date(2024, 3, 10), **cfg) is False, "the 10th maps to the 9th (< start) -> inactive"
    assert _beat(date(2024, 3, 12), **cfg) is False, "the 12th maps to the 11th -> inactive"


# --- _recurrence_matches: the Nth-weekday-of-month engine --------------------
def test_monthly_nth_weekday_second_tuesday():
    # July 2024 opens on a Monday, so Tuesdays fall on the 2nd/9th/16th/... The 2nd
    # Tuesday is Jul 9; week_occurrence counts via math.ceil(day/7) -> ceil(9/7)=2.
    sd = date(2024, 1, 1)
    cfg = dict(recurrence_type="monthly", monthly_mode="weekday",
               monthly_weekday=1, week_occurrence=2)  # Tuesday=1 (Mon=0), 2nd occurrence
    assert _matches(date(2024, 7, 9), sd, **cfg) is True, "Jul 9 is the 2nd Tuesday"
    assert _matches(date(2024, 7, 2), sd, **cfg) is False, "Jul 2 is the 1st Tuesday, not the 2nd"
    assert _matches(date(2024, 7, 16), sd, **cfg) is False, "Jul 16 is the 3rd Tuesday"
    assert _matches(date(2024, 7, 10), sd, **cfg) is False, "Jul 10 is a Wednesday, wrong weekday"
    # A different month proves it's occurrence-relative, not a fixed day: August
    # 2024 opens on a Thursday, so its 2nd Tuesday is Aug 13 (ceil(13/7)=2).
    assert _matches(date(2024, 8, 13), sd, **cfg) is True, "Aug 13 is the 2nd Tuesday of August"


# --- _recurrence_matches: the remaining recurrence types ---------------------
def test_daily_recurrence_always_matches():
    sd = date(2024, 1, 1)
    assert _matches(date(2024, 4, 1), sd, recurrence_type="daily") is True, "daily fires every day"


def test_weekly_recurrence():
    sd = date(2024, 1, 1)
    cfg = dict(recurrence_type="weekly", weekdays=[0, 2, 4])  # Mon/Wed/Fri (Mon=0)
    assert _matches(date(2024, 7, 8), sd, **cfg) is True, "Jul 8 2024 is a Monday (0)"
    assert _matches(date(2024, 7, 10), sd, **cfg) is True, "Jul 10 2024 is a Wednesday (2)"
    assert _matches(date(2024, 7, 9), sd, **cfg) is False, "Jul 9 2024 is a Tuesday (1), not selected"


def test_monthly_day_of_month():
    sd = date(2024, 1, 5)
    cfg = dict(recurrence_type="monthly", monthly_mode="day", monthly_day=15)
    assert _matches(date(2024, 9, 15), sd, **cfg) is True, "the 15th of any month fires"
    assert _matches(date(2024, 9, 14), sd, **cfg) is False, "the 14th does not"


def test_yearly_recurrence_matches_anniversary():
    sd = date(2024, 3, 10)
    assert _matches(date(2025, 3, 10), sd, recurrence_type="yearly") is True, \
        "same month-day a year later -> fires"
    assert _matches(date(2025, 3, 11), sd, recurrence_type="yearly") is False, \
        "one day off the anniversary -> no"


def test_once_matches_only_start_date():
    sd = date(2024, 2, 29)  # leap day, exercises the exact-date equality
    assert _matches(date(2024, 2, 29), sd, recurrence_type="once") is True, \
        "exactly start_date fires the one time"
    assert _matches(date(2024, 3, 1), sd, recurrence_type="once") is False, \
        "the day after never fires again"


def test_unknown_recurrence_type_is_inactive():
    sd = date(2024, 1, 1)
    assert _matches(date(2024, 1, 1), sd, recurrence_type="bogus") is False, \
        "an unrecognised recurrence type is never active (fail closed)"


# ── next_occurrence ──────────────────────────────────────────────────────────
# schedule_active answers "is it today"; next_occurrence answers "when", which is
# the question actually asked about a bin collection. It is a forward scan over the
# SAME _beat_active the live value uses, so the announced date and the day the
# schedule fires cannot disagree.


def _next(today: date, **params) -> str | None:
    return _ADAPTER._next_occurrence(today, params)


def test_next_occurrence_includes_today_when_active_today():
    sd = date(2024, 1, 1)
    assert _next(date(2024, 6, 5), recurrence_type="daily", start_date=sd.isoformat()) \
        == "2024-06-05", "a daily schedule's next day is today, not tomorrow"


def test_next_occurrence_finds_the_following_weekday():
    # weekdays=[1] is Tuesday. 2024-06-05 is a Wednesday -> next Tuesday is the 11th.
    sd = date(2024, 1, 1)
    assert _next(date(2024, 6, 5), recurrence_type="weekly", weekdays=[1],
                 start_date=sd.isoformat()) == "2024-06-11"


def test_next_occurrence_honours_the_day_offset():
    """A -1 offset means the schedule is active the day BEFORE the recurrence, so
    the next active day must shift with it — this is the announce-the-night-before
    pattern the bin schedules use."""
    sd = date(2024, 1, 1)
    plain = _next(date(2024, 6, 5), recurrence_type="weekly", weekdays=[1],
                  start_date=sd.isoformat())
    shifted = _next(date(2024, 6, 5), recurrence_type="weekly", weekdays=[1],
                    start_date=sd.isoformat(), day_offset=-1)
    assert plain == "2024-06-11" and shifted == "2024-06-10"


def test_next_occurrence_is_none_when_nothing_is_left():
    """A one-shot whose day has passed never fires again. None is the honest answer;
    a date would be a lie the UI would happily render."""
    assert _next(date(2026, 1, 1), recurrence_type="once", start_date="2020-06-01") is None


def test_end_date_is_a_seasonal_window_not_an_expiry():
    """start_date/end_date bound a YEAR-AGNOSTIC month-day window (that is what
    _in_yearly_window does), so "Irrigation Seazon 2026-05-01..2026-09-05" comes back
    every year rather than expiring. Pinned because it reads like an expiry and is
    not — the next date for such a schedule is inside next year's window."""
    params = dict(recurrence_type="daily", start_date="2020-05-01", end_date="2020-09-05")
    assert _next(date(2026, 1, 15), **params) == "2026-05-01", "waits for the window to open"
    assert _next(date(2026, 6, 10), **params) == "2026-06-10", "inside the window: today"


def test_next_occurrence_matches_the_live_active_value():
    """The property that matters: every day the scan calls "next" must be a day
    _beat_active agrees is active."""
    sd = date(2024, 1, 1)
    params = dict(recurrence_type="monthly", monthly_mode="weekday",
                  monthly_weekday=3, week_occurrence=3, start_date=sd.isoformat())
    for probe in (date(2024, 6, 5), date(2024, 12, 31), date(2025, 2, 14)):
        nxt = _next(probe, **params)
        assert nxt is not None
        assert _beat(date.fromisoformat(nxt), **params) is True
