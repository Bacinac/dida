"""The contacts adapter: reading Google's shape, and deciding when to read at all.

The People API half is a translation layer like any adapter's — a person with no
birthday is not a person this cares about, and pagination that stops early would
look, to the pruning caller, exactly like people leaving the book.
"""
import datetime
import json

import httpx
import pytest
from conftest import FakeBroker
from dida_adapter_contacts.adapter import ContactsAdapter
from dida_adapter_contacts.google import read_book
from dida_core.broker import BrokerError


def _transport(pages):
    """A People API that hands back `pages` in order, and a token endpoint."""
    seen = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        page = pages[seen["calls"]]
        seen["calls"] += 1
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler), seen


async def _patched_read(monkeypatch, pages):
    transport, seen = _transport(pages)
    real = httpx.AsyncClient

    def factory(*a, **k):
        return real(*a, **{**k, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return await read_book("cid", "csecret", "rtok"), seen


async def test_the_book_is_read_to_the_last_page(monkeypatch):
    pages = [
        {"connections": [
            {"resourceName": "people/c1", "names": [{"displayName": "Ana Horvat"}],
             "birthdays": [{"date": {"year": 1992, "month": 6, "day": 15}}]},
            {"resourceName": "people/c2", "names": [{"displayName": "Bez Rodjendana"}]},
        ], "nextPageToken": "p2"},
        {"connections": [
            {"resourceName": "people/c3", "names": [{"displayName": "Marko Marić"}],
             "birthdays": [{"date": {"month": 6, "day": 16}, "text": "--06-16"}]},
        ]},
    ]
    book, seen = await _patched_read(monkeypatch, pages)
    assert seen["calls"] == 2, "a book read only to page one would prune the rest away"
    assert [p["name"] for p in book] == ["Ana Horvat", "Marko Marić"]
    assert book[0]["born"] == datetime.date(1992, 6, 15)
    assert book[0]["id"] == "people/c1"
    # A birthday with no year survives the read as a REPORT, not as a date.
    assert book[1]["born"] is None and book[1]["raw"] == "--06-16"


async def test_a_page_that_fails_takes_the_whole_read_with_it(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1"})
        return httpx.Response(503, text="backend error")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    with pytest.raises(httpx.HTTPStatusError):
        await read_book("cid", "csecret", "rtok")


async def test_a_refused_refresh_token_says_what_google_said(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error": "invalid_grant"}')

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    with pytest.raises(RuntimeError, match="invalid_grant"):
        await read_book("cid", "csecret", "dead-token")


class _Cfg:
    def __init__(self, values):
        self._v = values

    def int(self, key, default=0):
        return int(self._v.get(key, default))

    def get(self, key, default=""):
        return self._v.get(key, default)


def _at(hour, day=15):
    return datetime.datetime(2026, 6, day, hour, 0)


def test_the_daily_read_happens_once_a_day_and_not_before_its_hour():
    a = ContactsAdapter()
    a._cfg = _Cfg({"sync_hour": 4})
    assert a._due(_at(3)) is False, "before the configured hour"
    assert a._due(_at(4)) is True
    a._synced_on = "2026-06-15"
    assert a._due(_at(9)) is False, "already read today"
    # A restart at 09:00 must not wait for tomorrow's 04:00: the day is what
    # matters, not the exact hour it comes back up.
    assert a._due(_at(9, day=16)) is True
    a._syncing = True
    assert a._due(_at(9, day=16)) is False, "a read already running is not due again"


def test_the_report_survives_being_written_as_json():
    # store_book hands back real dates in `changed`/`no_year`; app_settings takes
    # text, and a TypeError here would lose the whole sync report silently.
    report = {"read": 2, "added": 1, "no_year": [{"name": "X", "says": "--06-16"}],
              "at": datetime.datetime(2026, 6, 15, 4, 0).isoformat()}
    assert json.loads(json.dumps(report, default=str))["at"].startswith("2026-06-15")


class _Bus:
    def __init__(self):
        self.states = []
        self.entities = []

    async def publish_state(self, update):
        self.states.append((update.capability, update.value))

    async def publish_entity(self, info):
        self.entities.append(info.entity_id)


def _people(*rows):
    """The api's answer to `people`: who is announced (dates as ISO, the wire form)."""
    return FakeBroker(people={"announced": [{"name": n, "born_on": b.isoformat()} for n, b in rows],
                              "total": len(rows)})


async def test_only_the_opted_in_reach_the_speaker_and_only_once():
    a = ContactsAdapter()
    a._cfg = _Cfg({"announce_days": 1})
    a._bus = _Bus()
    # The api answers with the announced only, so opting out is expressed by simply
    # not being in these rows — its SQL is what filters.
    a.broker = _people(
        ("Ana Horvat", datetime.date(1992, 6, 15)),
        ("Marko Marić", datetime.date(2014, 6, 16)),
    )
    now = datetime.datetime(2026, 6, 15, 9, 0)

    assert await a._publish_birthdays(now) == 2
    assert a._bus.entities == ["contacts:birthdays"], "the catalog entry, once"
    assert a._bus.states == [
        ("text", "0=Ana Horvat:34;1=Marko Marić:12"),
        ("binary", True),
    ]

    # Same day, same facts: nothing is republished. Without this the bus and the
    # history carry a duplicate every minute, for ever.
    a._bus.states.clear()
    assert await a._publish_birthdays(now) == 2
    assert a._bus.states == []


async def test_a_day_with_nobody_in_it_says_so_rather_than_going_quiet():
    a = ContactsAdapter()
    a._cfg = _Cfg({"announce_days": 1})
    a._bus = _Bus()
    a.broker = _people(("Ana Horvat", datetime.date(1992, 6, 15)))

    assert await a._publish_birthdays(datetime.datetime(2026, 6, 15, 9, 0)) == 1
    a._bus.states.clear()
    # The day after: the flag has to fall, or a rule holding on it never releases.
    assert await a._publish_birthdays(datetime.datetime(2026, 6, 17, 9, 0)) == 0
    assert a._bus.states == [("text", ""), ("binary", False)]


async def test_a_token_that_cannot_be_read_is_louder_than_no_token():
    a = ContactsAdapter()
    a.broker = FakeBroker(stored=None)
    assert await a._refresh_token() == "", "no row = an installation reading export files"

    # A row that will not decrypt is a connection that exists and does not work.
    # Reported as 'not connected' it looks like a deliberate choice, and the daily
    # read stops for good with a green badge above it.
    a.broker = FakeBroker(stored=BrokerError("stored: contacts._oauth: undecryptable"))
    with pytest.raises(RuntimeError, match="DIDA_SECRET_KEY"):
        await a._refresh_token()


async def test_connecting_at_noon_reads_today_not_tomorrow():
    """The first read has to follow the consent, not the clock.

    A tick with no token must not tick the day off: an installation connected in
    the afternoon would otherwise wait until the next 04:00 while its card said
    "Nobody here yet" — with a live Google grant sitting right above the message.
    """
    a = ContactsAdapter()
    a._cfg = _Cfg({"sync_hour": 4})
    a.broker = FakeBroker(stored=None)  # not connected yet
    noon = _at(12)

    assert a._due(noon) is True
    await a._sync(noon)  # nothing to read from — and nothing claimed as read
    assert a._synced_on == "", "a day with no token is not a day that has been read"
    assert a._due(noon) is True, "the moment a token appears, the read is still due"
