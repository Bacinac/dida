"""Live fan-out of device events to open browsers (the api's Hub).

Its own suite because it runs in the API image: the journal suite runs in the
journal image, which has no FastAPI — importing dida_api there would fail the
gate for a reason that has nothing to do with the code under test.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src:/w/services/api/src python -m pytest tests/test_ws_events.py"
"""
from __future__ import annotations

import json

import pytest
from dida_api.app import Hub
from dida_core.events import JournalEvent


def _client(hub, hidden: set[str]) -> list[str]:
    """Attach a fake client and return the list its queue receives."""
    sent: list[str] = []

    class FakeWS:
        pass

    hub._clients[FakeWS()] = type("C", (), {
        "hidden": hidden,
        "allowed": None,
        "queue": type("Q", (), {"put_nowait": staticmethod(sent.append)})(),
    })()
    return sent


@pytest.mark.asyncio
async def test_journal_events_are_tagged_so_the_store_can_tell_them_apart():
    # State deltas carry no `type`; the store routes on its presence. Without the
    # tag a journal event would be applied as a state update and corrupt a device.
    hub = Hub()
    sent = _client(hub, set())
    await hub.on_journal(JournalEvent(
        ts_ns=1_700_000_000_000_000_000, kind="offline", entity_id="mqtt:x",
        source="adapter:mqtt", severity="error", message="broker gone"))
    payload = json.loads(sent[0])
    assert payload["type"] == "event"
    assert payload["ms"] == 1_700_000_000_000, "nanoseconds are converted for the browser"
    assert payload["kind"] == "offline" and payload["severity"] == "error"


@pytest.mark.asyncio
async def test_no_event_about_an_entity_the_user_cannot_see():
    # The rule state already follows: a user who may not see a device may not see
    # why it went quiet either.
    hub = Hub()
    sent = _client(hub, {"mqtt:secret"})
    await hub.on_journal(JournalEvent(ts_ns=1, kind="offline", entity_id="mqtt:secret"))
    assert sent == []


@pytest.mark.asyncio
async def test_a_service_level_event_names_no_entity_and_stays_visible():
    # An adapter going offline is one event for the whole protocol — hiding it
    # because it carries no entity_id would silence exactly the useful case.
    hub = Hub()
    sent = _client(hub, {"mqtt:secret"})
    await hub.on_journal(JournalEvent(ts_ns=2, kind="online", source="adapter:mqtt"))
    assert len(sent) == 1
