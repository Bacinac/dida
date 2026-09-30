"""The one URL in DIDA that is deliberately unauthenticated.

A push notification carries a picture, and the phone's OS fetches that picture
before anyone unlocks the screen — with no cookie, because the notification is
rendered outside the browser session. So `/api/snap/<name>` cannot be gated by a
login, and the NAME has to be the capability instead: 24 random bytes from
`secrets`, valid for a day, gone afterwards.

That makes three properties load-bearing rather than incidental. The name must be
unguessable, so it comes from a CSPRNG and not from a counter or a uuid1. It must
be unable to name anything except a frame, because `_SNAP_DIR / name` with a `..`
in it is a file reader for whatever the api container can see. And the expiry has
to be enforced when the frame is READ, not only when the sweeper happens to run —
otherwise a link keeps working for as long as nothing else triggers a sweep, and
"valid for a day" quietly means "valid until someone notices".

Minting is the other half, and it fails soft on purpose: a doorbell notification
without its picture still has to go out, so every failure path returns None
rather than raising into the notify chain.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from dida_api import camera as mod
from fastapi import HTTPException


class _Pool:
    def __init__(self, rows=None) -> None:
        self.rows = rows or {}
        self.asked: list[str] = []

    async def fetchval(self, sql, *a):
        self.asked.append(a[0])
        return self.rows.get(a[0])


class _App:
    def __init__(self, pool) -> None:
        self.state = type("S", (), {"pool": pool})()


DESC = {"snapshot": "http://cam.local/snap.jpg"}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """The camera's location admits its host; confinement itself is pinned in
    test_api_camera_auth."""
    async def _bases(pool, entity_id, desc, *, refresh=False):
        return ("http://cam.local",)
    monkeypatch.setattr(mod, "_allowed_bases", _bases)


@pytest.fixture
def snapdir(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_SNAP_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def upstream(monkeypatch):
    """A camera that answers with one frame."""
    async def _fetch(desc, url, pool, timeout_s=8.0):
        return type("R", (), {"status_code": 200, "content": b"\xff\xd8jpegbytes"})()
    monkeypatch.setattr(mod, "_fetch_upstream", _fetch)


# --- the name is the capability -------------------------------------------------


def test_the_name_cannot_reach_outside_the_snapshot_directory():
    """`_SNAP_DIR / name` is a path join. Anything that escapes it turns an
    unauthenticated route into a file reader for the api container."""
    for name in ("../../etc/passwd", "../secrets.jpg", "/etc/passwd",
                 "a/../../b.jpg", "..jpg", "....jpg"):
        assert not mod._SNAP_NAME_RE.match(name), f"{name!r} would be joined onto /state"


def test_a_name_without_the_extension_is_refused():
    for name in ("abcdefghijklmnop", "abcdefghijklmnop.png", "abcdefghijklmnop.jpg.php"):
        assert not mod._SNAP_NAME_RE.match(name)


def test_a_name_that_is_too_short_to_be_random_is_refused():
    """A 4-character name is guessable by hand."""
    assert not mod._SNAP_NAME_RE.match("ab12.jpg")


def test_the_minted_name_matches_what_the_reader_accepts():
    """Writer and reader have to agree, or the frames exist and 404."""
    import secrets as pysecrets
    for _ in range(20):
        assert mod._SNAP_NAME_RE.match(f"{pysecrets.token_urlsafe(24)}.jpg")


def test_the_name_is_minted_from_a_cryptographic_source():
    """The URL is the only thing protecting the frame. A counter or a timestamp
    would make yesterday's doorbell picture enumerable."""
    import pathlib
    src = pathlib.Path(mod.__file__).read_text()
    mint = src[src.index("async def mint_snapshot"):src.index("SNAP_CTL")]
    assert "secrets.token_urlsafe" in mint
    assert re.search(r"token_urlsafe\((\d+)\)", mint)
    assert int(re.search(r"token_urlsafe\((\d+)\)", mint).group(1)) >= 16


# --- serving ---------------------------------------------------------------------


async def test_a_real_frame_is_served(snapdir):
    (snapdir / "abcdefghijklmnopqrst.jpg").write_bytes(b"jpeg")
    r = await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")
    assert r.body == b"jpeg"
    assert r.media_type == "image/jpeg"


async def test_the_frame_is_not_cached_publicly(snapdir):
    """It is a picture of the inside of someone's house, fetched over a link with
    no login. A shared cache must not keep it."""
    (snapdir / "abcdefghijklmnopqrst.jpg").write_bytes(b"jpeg")
    r = await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")
    assert "private" in r.headers["cache-control"]


@pytest.mark.parametrize("name", ["../../etc/passwd", "nope.png", "x.jpg", ""])
async def test_a_name_that_is_not_a_frame_name_is_a_404(name, snapdir):
    with pytest.raises(HTTPException) as e:
        await mod.serve_snapshot(name)
    assert e.value.status_code == 404


async def test_a_missing_frame_is_a_404_not_a_crash(snapdir):
    with pytest.raises(HTTPException) as e:
        await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")
    assert e.value.status_code == 404


async def test_expiry_is_enforced_on_READ_not_only_by_the_sweeper(snapdir):
    """The sweeper runs when a frame is minted. On a quiet day nothing mints, so a
    link that should have died at 24 hours keeps working — "valid for a day"
    silently becomes "valid until someone takes a picture"."""
    import os
    f = snapdir / "abcdefghijklmnopqrst.jpg"
    f.write_bytes(b"jpeg")
    old = time.time() - mod._SNAP_TTL_S - 60
    os.utime(f, (old, old))
    with pytest.raises(HTTPException) as e:
        await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")
    assert e.value.status_code == 404


async def test_an_expired_frame_is_deleted_when_it_is_asked_for(snapdir):
    """Refusing to serve it while leaving it on disk keeps a picture of the house
    in /state indefinitely."""
    import os
    f = snapdir / "abcdefghijklmnopqrst.jpg"
    f.write_bytes(b"jpeg")
    old = time.time() - mod._SNAP_TTL_S - 60
    os.utime(f, (old, old))
    with pytest.raises(HTTPException):
        await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")
    assert not f.exists()


async def test_a_frame_inside_its_window_survives(snapdir):
    """The guard must not be so eager that a notification opened an hour later
    shows a broken image."""
    import os
    f = snapdir / "abcdefghijklmnopqrst.jpg"
    f.write_bytes(b"jpeg")
    recent = time.time() - 3600
    os.utime(f, (recent, recent))
    assert (await mod.serve_snapshot("abcdefghijklmnopqrst.jpg")).body == b"jpeg"


# --- the sweeper -----------------------------------------------------------------


def test_the_sweeper_removes_only_what_has_expired(snapdir):
    import os
    keep = snapdir / "keep0123456789abcdef.jpg"
    drop = snapdir / "drop0123456789abcdef.jpg"
    for f in (keep, drop):
        f.write_bytes(b"x")
    old = time.time() - mod._SNAP_TTL_S - 60
    os.utime(drop, (old, old))
    mod._snap_gc(time.time())
    assert keep.exists() and not drop.exists()


def test_the_sweeper_touches_nothing_that_is_not_a_frame(snapdir):
    """`/state` is a mount an operator can repoint."""
    import os
    other = snapdir / "notes.txt"
    other.write_bytes(b"x")
    old = time.time() - mod._SNAP_TTL_S - 60
    os.utime(other, (old, old))
    mod._snap_gc(time.time())
    assert other.exists()


def test_the_sweeper_survives_a_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_SNAP_DIR", tmp_path / "gone")
    mod._snap_gc(time.time())


# --- minting fails soft ----------------------------------------------------------


async def test_a_frame_is_minted_and_named_by_its_path(snapdir, upstream):
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    url = await mod.mint_snapshot(_App(pool), "baba:gate")
    assert url.startswith("/api/snap/")
    assert mod._SNAP_NAME_RE.match(url.rsplit("/", 1)[1])
    assert (snapdir / url.rsplit("/", 1)[1]).read_bytes() == b"\xff\xd8jpegbytes"


async def test_a_child_entity_resolves_to_its_camera(snapdir, upstream):
    """An automation's trigger holds `baba:<cam>:zone:<id>`, not the camera. If
    that didn't resolve, every notification from a zone rule would lose its
    picture — and nothing would say why."""
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    url = await mod.mint_snapshot(_App(pool), "baba:gate:zone:3")
    assert url is not None
    assert pool.asked == ["baba:gate:zone:3", "baba:gate"]


@pytest.mark.parametrize("eid", ["", "   ", "../../etc/passwd", "a b", "x" * 200])
async def test_a_name_that_is_not_an_entity_mints_nothing(eid, snapdir, upstream):
    assert await mod.mint_snapshot(_App(_Pool()), eid) is None


async def test_a_camera_with_no_snapshot_url_mints_nothing(snapdir, upstream):
    pool = _Pool({"baba:gate": json.dumps({"stream": "rtsp://x"})})
    assert await mod.mint_snapshot(_App(pool), "baba:gate") is None


async def test_a_descriptor_that_is_not_json_mints_nothing(snapdir, upstream):
    pool = _Pool({"baba:gate": "{not json"})
    assert await mod.mint_snapshot(_App(pool), "baba:gate") is None


async def test_an_unreachable_camera_does_not_stop_the_notification(snapdir, monkeypatch):
    """The whole reason every path here returns None: a doorbell notification
    without its picture still has to go out."""
    async def _boom(desc, url, pool, timeout_s=8.0):
        raise HTTPException(502, "camera unreachable")
    monkeypatch.setattr(mod, "_fetch_upstream", _boom)
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    assert await mod.mint_snapshot(_App(pool), "baba:gate") is None


async def test_an_empty_response_body_mints_nothing(snapdir, monkeypatch):
    """A zero-byte frame written to disk becomes a notification with a broken
    image, which reads as a broken camera rather than a slow one."""
    async def _empty(desc, url, pool, timeout_s=8.0):
        return type("R", (), {"status_code": 200, "content": b""})()
    monkeypatch.setattr(mod, "_fetch_upstream", _empty)
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    assert await mod.mint_snapshot(_App(pool), "baba:gate") is None


async def test_an_error_status_mints_nothing(snapdir, monkeypatch):
    async def _err(desc, url, pool, timeout_s=8.0):
        return type("R", (), {"status_code": 401, "content": b"nope"})()
    monkeypatch.setattr(mod, "_fetch_upstream", _err)
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    assert await mod.mint_snapshot(_App(pool), "baba:gate") is None


async def test_every_mint_gives_a_different_url(snapdir, upstream):
    """Reusing a name would let an old notification's link show a new frame — and
    make the link enumerable across notifications."""
    pool = _Pool({"baba:gate": json.dumps(DESC)})
    urls = {await mod.mint_snapshot(_App(pool), "baba:gate") for _ in range(10)}
    assert len(urls) == 10
