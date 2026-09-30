"""Timestamps that reach a person, in the clock that person reads.

Everything DIDA stores is UTC, which is right for a database and wrong for a
sentence. The gap is two hours here for half the year and one for the other half,
so a UTC timestamp shown to the household is not slightly off — it names a
different hour, and it does so plausibly enough that nobody questions it.

The assistant is where this mattered, because it is the surface that answers in
prose. `command_log` says in its own docstring that it is the only thing that can
answer "why did the light come on at 3am" — a question about an HOUR — and it was
handing the model UTC. Measured on the house: a rule that fired at 21:15 was
described to the model as 19:15, with nothing in the prompt to say otherwise.

The zone was not a new idea. The astro adapter has carried `tz` all along and the
heating controller reads it to decide what night means; there was simply no shared
way to ask. `house_timezone` is that, resolved from the same row rather than a
second setting — a second one would be the two-lists failure again, in the place
where being wrong is least visible.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from dida_core import house_timezone, local_str

ZAGREB = ZoneInfo("Europe/Zagreb")


class _Pool:
    def __init__(self, tz=None, boom=False) -> None:
        self.tz, self.boom = tz, boom
        self.asked: list[str] = []

    async def fetchval(self, sql, *a):
        if self.boom:
            raise RuntimeError("database gone")
        self.asked.append(sql)
        return self.tz


# --- the conversion --------------------------------------------------------------


def test_summer_is_two_hours_ahead():
    """The measured case: a rule that fired at 21:15 was being described as 19:15."""
    assert local_str(datetime(2026, 8, 8, 19, 15, 19, tzinfo=UTC), ZAGREB) == "2026-08-08 21:15"


def test_winter_is_one_hour_ahead():
    """A fixed offset would be right for half the year, which is the kind of fix
    that gets confirmed in August and reported in November."""
    assert local_str(datetime(2026, 1, 15, 19, 15, tzinfo=UTC), ZAGREB) == "2026-01-15 20:15"


def test_the_hour_before_the_clocks_change_is_still_handled():
    """Both DST transitions, taken from the zone rather than assumed."""
    assert local_str(datetime(2026, 3, 29, 0, 30, tzinfo=UTC), ZAGREB) == "2026-03-29 01:30"
    assert local_str(datetime(2026, 3, 29, 1, 30, tzinfo=UTC), ZAGREB) == "2026-03-29 03:30"


def test_a_naive_timestamp_is_read_as_utc():
    """asyncpg hands back aware values, ClickHouse hands back naive ones, and both
    are UTC.

    HONEST LIMIT: this assertion alone cannot prove it. Dropping the explicit
    tzinfo makes Python fall back to the PROCESS timezone, and every DIDA container
    runs Etc/UTC — so the wrong code produces the right answer here and the test
    passes. Verified by sabotage: removing the branch left this green. It stays
    because it states the contract, and the structural check below is what actually
    holds it; on a host whose clock is not UTC this one would start failing, which
    is the case it is really for."""
    assert local_str(datetime(2026, 8, 8, 19, 15), ZAGREB) == "2026-08-08 21:15"
    assert local_str(datetime(2026, 8, 8, 19, 15), ZAGREB) == \
        local_str(datetime(2026, 8, 8, 19, 15, tzinfo=UTC), ZAGREB)


def test_the_naive_case_is_handled_explicitly_not_by_the_process_clock():
    """The assertion above passes either way inside a UTC container. What must be
    true is that the code SAYS utc, so an installation on a host with a local clock
    — or a future container that stops pinning TZ — does not silently shift by its
    own offset."""
    src = pathlib.Path(local_str.__code__.co_filename).read_text()
    body = src[src.index("def local_str"):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
    assert "tzinfo is None" in body
    assert "tzinfo=UTC" in body


def test_nothing_renders_as_an_empty_string_not_a_crash():
    assert local_str(None, ZAGREB) == ""


def test_the_time_only_form_drops_the_date():
    assert local_str(datetime(2026, 8, 8, 19, 15, tzinfo=UTC), ZAGREB, with_date=False) == "21:15"


def test_no_offset_is_appended():
    """The model is told the value is already local. A trailing +02:00 invites it to
    convert a second time, and a reader to wonder which one is theirs."""
    out = local_str(datetime(2026, 8, 8, 19, 15, tzinfo=UTC), ZAGREB)
    assert "+" not in out and "Z" not in out


# --- where the zone comes from ---------------------------------------------------


async def test_the_zone_is_read_from_the_astro_adapter():
    """The same row the heating controller reads. A second setting would be the
    two-lists failure again, in the place where a divergence is least visible."""
    pool = _Pool("Europe/Zagreb")
    assert await house_timezone(pool) == ZAGREB
    assert "adapter = 'astro'" in pool.asked[0]
    assert "'tz'" in pool.asked[0]


async def test_a_different_house_gets_its_own_zone():
    """Cabin is a second installation; an installation elsewhere is the whole reason
    this is configuration and not a constant."""
    assert await house_timezone(_Pool("Europe/Lisbon")) == ZoneInfo("Europe/Lisbon")


@pytest.mark.parametrize("stored", [None, "", "   ", "Not/AZone"])
async def test_an_unset_or_broken_zone_falls_back_to_the_declared_default(stored):
    """Never to UTC. An unconfigured installation showing UTC is the exact failure
    this exists to prevent, and it is the failure that hides best — the number
    looks like a time."""
    assert await house_timezone(_Pool(stored)) == ZAGREB


async def test_a_database_hiccup_does_not_break_the_caller():
    """This sits in the path of an assistant answer. Failing here would turn a
    wrong hour into no answer at all."""
    assert await house_timezone(_Pool(boom=True)) == ZAGREB


# --- and the assistant no longer hands out UTC -----------------------------------


ASSISTANT = pathlib.Path("/w/services/api/src/dida_api/assistant.py")


def _src() -> str:
    if not ASSISTANT.exists():
        pytest.skip("api source not mounted")
    return ASSISTANT.read_text()


def test_no_timestamp_reaches_the_model_as_raw_iso():
    """Structural, because this is a regression that reads as correct: an
    `isoformat()` here produces a perfectly well-formed timestamp naming the wrong
    hour, and nothing downstream can tell."""
    offenders = [ln.strip() for ln in _src().splitlines()
                 if "isoformat()" in ln and not ln.strip().startswith("#")]
    assert not offenders, "raw UTC handed to the assistant: " + "; ".join(offenders)


def test_all_four_time_carrying_tools_convert():
    """explain_automation, list_automations, automation_runs, command_log — the
    last of which exists to answer "why did the light come on at 3am"."""
    src = _src()
    assert len(re.findall(r"local_str\(", src)) >= 4
    assert src.count("await house_timezone(") >= 4


def test_the_prompts_tell_the_model_not_to_convert_again():
    """Given a local time and no instruction, a model that infers a UTC source
    helpfully shifts it — turning a fixed bug into a subtler one."""
    src = _src()
    assert src.count("ALREADY the household's local time") >= 2
    assert "never convert" in src
