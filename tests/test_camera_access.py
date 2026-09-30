"""Who may look through a camera, and what a refusal is allowed to reveal.

The camera proxy is a raw read path: it fetches a stream server-side and hands the
bytes back, so it cannot rely on the UI having hidden anything. `_guard` enforces
both axes the /command path does — the page scope (an /entry-only guest has no
business at the cameras) AND the per-user view boundary.

The status codes are a decision, not an accident. A hidden camera answers 404, not
403: 403 says "this exists and you may not see it", which confirms the camera to
someone who was only guessing. The distinction costs nothing and is the difference
between refusing and disclosing.

Also here: the URL a descriptor is allowed to send the proxy at. `_upstream` accepts
only http(s) — a descriptor is data, and data that can name `file:///etc/passwd`
turns a proxy into a file reader.
"""

from __future__ import annotations

import time

import pytest
from dida_api import camera as mod
from dida_api.camera import _snap_gc, _upstream
from fastapi import HTTPException


class _Pool:
    async def fetchval(self, *a):
        return None


class _Request:
    def __init__(self) -> None:
        self.app = type("A", (), {"state": type("S", (), {"pool": _Pool()})()})()


@pytest.fixture
def guard(monkeypatch):
    """Drive `_guard` with scripted page-scope and hide answers."""
    async def run(entity_id, *, can_see=True, hidden=False):
        monkeypatch.setattr(mod, "can_see_page", lambda _user, _page: can_see)

        async def _is_hidden(_pool, _user, _eid):
            return hidden
        monkeypatch.setattr(mod, "is_hidden", _is_hidden)
        await mod._guard(_Request(), entity_id, object())
    return run


# --- who may look -------------------------------------------------------------


async def test_a_permitted_user_passes(guard):
    await guard("baba:gate")


async def test_a_user_without_the_cameras_page_is_refused(guard):
    """An /entry-only guest reaches the entry surface and nothing else; the raw proxy
    must not be a way around the page scope."""
    with pytest.raises(HTTPException) as e:
        await guard("baba:gate", can_see=False)
    assert e.value.status_code == 403


async def test_a_HIDDEN_camera_answers_404_not_403(guard):
    """403 confirms the camera exists to somebody who was guessing. 404 does not —
    and the proxy is exactly where that leak would not be visible in the UI."""
    with pytest.raises(HTTPException) as e:
        await guard("baba:bedroom", hidden=True)
    assert e.value.status_code == 404


async def test_the_page_scope_is_checked_BEFORE_the_hide_rule(guard):
    """Otherwise a guest with no camera access learns which cameras exist by which
    id returns 404 versus 403."""
    with pytest.raises(HTTPException) as e:
        await guard("baba:bedroom", can_see=False, hidden=True)
    assert e.value.status_code == 403


@pytest.mark.parametrize("bad", [
    "", "  ", "baba:gate/../etc", "baba gate", "a" * 129, "baba:gate?x=1", "baba:gate#f",
])
async def test_a_malformed_id_is_rejected_before_anything_is_read(bad, guard):
    """The id reaches a database lookup and a URL; anything outside the character
    set is refused at the door rather than sanitised further in."""
    with pytest.raises(HTTPException) as e:
        await guard(bad)
    assert e.value.status_code == 400


async def test_a_dotted_or_dashed_id_is_still_valid(guard):
    """Real ids carry both — `baba:<uuid>` and `frigate:front-door.cam`."""
    await guard("frigate:front-door.cam")
    await guard("baba:53f95d92-ea2d-4f47-8864-04c83910f61a:zone:3")


# --- where the proxy may be pointed -------------------------------------------


def test_only_http_urls_are_followed():
    assert _upstream({"snapshot": "http://host/x.jpg"}, "snapshot") == "http://host/x.jpg"
    assert _upstream({"snapshot": "https://host/x.jpg"}, "snapshot") == "https://host/x.jpg"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://host/x", "//host/x", "/etc/passwd", "javascript:alert(1)",
])
def test_a_non_http_scheme_is_refused(url):
    """A descriptor is data. Data that can name a scheme turns the proxy into a
    file reader — and the request would come from inside the container."""
    assert _upstream({"snapshot": url}, "snapshot") is None


def test_a_missing_or_non_string_url_is_None():
    assert _upstream({}, "snapshot") is None
    assert _upstream({"snapshot": 42}, "snapshot") is None
    assert _upstream({"snapshot": None}, "snapshot") is None


# --- the snapshot cache -------------------------------------------------------


def test_expired_snapshots_are_removed_and_fresh_ones_kept(tmp_path, monkeypatch):
    """A notification's frame is served from an unauthenticated path guarded only by
    192 bits of name entropy and this TTL. A GC that never collects leaves those
    URLs valid forever."""
    monkeypatch.setattr(mod, "_SNAP_DIR", tmp_path)
    now = time.time()

    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    import os
    os.utime(old, (now - mod._SNAP_TTL_S - 60, now - mod._SNAP_TTL_S - 60))

    fresh = tmp_path / "fresh.jpg"
    fresh.write_bytes(b"x")

    _snap_gc(now)
    assert not old.exists(), "an expired snapshot stayed reachable"
    assert fresh.exists(), "a fresh snapshot was collected out from under a notification"


def test_the_gc_leaves_files_that_are_not_snapshots_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_SNAP_DIR", tmp_path)
    other = tmp_path / "keepme.txt"
    other.write_bytes(b"x")
    import os
    os.utime(other, (0, 0))
    _snap_gc(time.time())
    assert other.exists()


def test_a_file_that_vanishes_mid_sweep_does_not_break_the_sweep(tmp_path, monkeypatch):
    """Two GC passes can overlap; the loser must not take the caller down."""
    monkeypatch.setattr(mod, "_SNAP_DIR", tmp_path)
    gone = tmp_path / "gone.jpg"
    gone.write_bytes(b"x")
    import os
    os.utime(gone, (0, 0))
    gone.unlink()
    _snap_gc(time.time())  # must not raise
