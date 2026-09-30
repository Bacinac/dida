"""A proxy that puts a caller-supplied string into someone else's path.

It exists because the browser reaches DIDA's forwarded port and nothing else —
not OPUS's — so the API serves the bytes on its behalf. That makes the API a
request builder operating with a token and network reach the caller does not
have, which is fine exactly as long as the caller cannot steer where the request
lands. httpx normalises dot segments before sending, so a path segment of `..`
walks out of the intended prefix and the proxy answers for the whole server —
the ids are constrained, and pinned here.
"""

from __future__ import annotations

import pytest
from dida_api import opus
from dida_api import photos as ph
from dida_api.auth import AuthUser
from fastapi import HTTPException

USER = AuthUser(id=2, username="gost", role="user")
CHECKSUM = "0123456789abcdef0123456789abcdef01234567"


class _Request:
    def __init__(self) -> None:
        self.app = type("A", (), {"state": type("S", (), {"pool": object()})()})()
        self.headers: dict[str, str] = {}


@pytest.fixture
def sent(monkeypatch):
    """Every path that would be asked of OPUS."""
    out: list[str] = []

    async def fetch(pool, path):
        out.append(path)
        return b"not a picture"
    monkeypatch.setattr(ph.opus, "fetch", fetch)
    return out


@pytest.mark.parametrize("bad", [
    "..", "../../api/users", "abc/def", "'; DROP", "", "x" * 200,
    CHECKSUM.upper(), CHECKSUM[:-1], CHECKSUM + "0", f"{CHECKSUM}/../wall",
])
async def test_a_photo_id_that_is_not_a_checksum_never_reaches_opus(bad, sent):
    """The proxy carries the house's OPUS token, so a steerable path is a
    token-bearing reader for everything that token opens."""
    with pytest.raises(HTTPException) as e:
        await ph.photo_preview(bad, _Request(), user=USER)
    assert e.value.status_code == 400
    assert sent == []


async def test_a_checksum_asks_opus_for_exactly_its_preview(sent):
    with pytest.raises(HTTPException):
        await ph.photo_preview(CHECKSUM, _Request(), user=USER)
    assert sent == [f"/photos/{CHECKSUM}/preview"]


async def test_bytes_that_are_no_picture_are_502_not_a_traceback(sent):
    """A photo frame on the wall panel that stops updating must say the source is
    broken, not hand the panel a 500 page it will render as a black rectangle."""
    with pytest.raises(HTTPException) as e:
        await ph.photo_preview(CHECKSUM, _Request(), user=USER)
    assert e.value.status_code == 502


async def test_an_unreachable_opus_is_502_not_a_traceback(monkeypatch):
    async def fetch(pool, path):
        raise opus.OpusUnavailable("OPUS did not answer: connection refused")
    monkeypatch.setattr(ph.opus, "fetch", fetch)
    with pytest.raises(HTTPException) as e:
        await ph.photo_preview(CHECKSUM, _Request(), user=USER)
    assert e.value.status_code == 502
