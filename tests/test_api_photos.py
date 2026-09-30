"""Integration test — the wall panel's photographs (dida_api.photos), asked of
OPUS · Player with the house token: an unconfigured OPUS fails loud with a clean
404 the panel can distinguish; the draw goes to the player's one wall route with
the token attached server-side, malformed ids dropped, place and country carried
through; a preview comes back as a JPEG under the panel's texture cap whatever
OPUS keeps; an upstream outage is 502.

Runs in the api image against the runner's ephemeral Postgres (tests/run.sh);
bypasses the app lifespan by wiring app.state directly. The player is a stubbed
httpx.AsyncClient — no network.
"""
import io
import struct
from typing import ClassVar

import dida_api.app as appmod
import dida_api.opus as opus
import dida_api.photos as photos
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient, ConnectError
from PIL import Image


def _avif(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (100, 140, 180)).save(buf, format="AVIF")
    return buf.getvalue()


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a baseline JPEG's SOF0 marker."""
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 1
    raise ValueError("no SOF marker")


# OPUS keeps its previews at 2048px AVIF — at the Nest Hub's texture cap, in a
# format its Chromium may not draw.
BIG_AVIF = _avif(2048, 1152)

PHOTO_A = "0123456789abcdef0123456789abcdef01234567"
PHOTO_B = "fedcba9876543210fedcba9876543210fedcba98"
TOKEN = "opus-house-token-xyz"


class StubResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class StubPlayer:
    """Stands in for the httpx.AsyncClient dida_api.opus builds — records every
    request with the address and headers it was built with."""

    calls: ClassVar[list[tuple[str, dict | None, dict]]] = []
    fail = False

    def __init__(self, base_url="", headers=None, **kw):
        self.base_url = base_url
        self.headers = headers or {}

    async def aclose(self):
        return None

    async def get(self, path, params=None):
        if StubPlayer.fail:
            raise ConnectError("OPUS down")
        StubPlayer.calls.append((f"{self.base_url}{path}", params, self.headers))
        if path == "/photos/wall":
            return StubResponse(json_data={"photos": [
                {"id": PHOTO_A, "taken_at": "2024-10-06T11:12:27+00:00", "place": "Rovinj", "country": "HR"},
                {"id": "../evil"}, {"id": PHOTO_A.upper()}, {"not": "an id"},
                {"id": PHOTO_B, "taken_at": None, "place": None, "country": None},
            ]})
        if path == f"/photos/{PHOTO_A}/preview":
            return StubResponse(content=BIG_AVIF, headers={"content-type": "image/avif"})
        return StubResponse(status_code=404)


async def _cleanup(pool):
    await pool.execute(
        "DELETE FROM app_settings WHERE key IN ('opus_url', 'opus_token', 'panel_config')"
    )
    await pool.execute("DELETE FROM users WHERE username IN ('photoadmin', 'photouser')")


async def test_photos_come_from_opus(monkeypatch):
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('photoadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('photouser', $1, 'user')",
        await hash_password("userpw12"),
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "photos-test-secret-0123456789abcdef"

    monkeypatch.setattr(opus.httpx, "AsyncClient", StubPlayer)
    monkeypatch.setattr(opus, "_client", None)
    monkeypatch.setattr(opus, "_client_for", None)
    StubPlayer.calls = []
    StubPlayer.fail = False

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "photoadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        r = await c.get("/photos/random")
        assert r.status_code == 404 and r.json()["detail"] == "photos_not_configured"
        assert StubPlayer.calls == []

        r = await c.put("/settings", json={"opus_url": "http://opus.test:8098/", "opus_token": TOKEN})
        assert r.status_code == 204

        r = await c.get("/photos/random?count=5")
        assert r.status_code == 200
        items = r.json()["photos"]
        assert [p["id"] for p in items] == [PHOTO_A, PHOTO_B], "anything but a checksum dropped"
        assert items[0] == {"id": PHOTO_A, "taken_at": "2024-10-06T11:12:27+00:00",
                            "place": "Rovinj", "country": "HR"}
        assert items[1]["place"] is None and items[1]["country"] is None
        assert StubPlayer.calls[-1] == ("http://opus.test:8098/api/photos/wall", {"count": 5},
                                        {opus.TOKEN_HEADER: TOKEN})

        r = await c.get(f"/photos/{PHOTO_A}/preview")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert "private" in r.headers["cache-control"]
        w, h = _jpeg_size(r.content)
        assert w == photos._MAX_EDGE, f"not fitted under the panel's cap ({w}x{h})"
        assert abs(w / h - 2048 / 1152) < 0.01, "aspect ratio preserved"
        assert StubPlayer.calls[-1][0] == f"http://opus.test:8098/api/photos/{PHOTO_A}/preview"

        r = await c.get(f"/photos/{PHOTO_B}/preview")
        assert r.status_code == 502, "a preview OPUS does not have is said, not drawn blank"
        r = await c.get("/photos/..%2Fevil/preview")
        assert r.status_code in (400, 404), "a non-checksum never reaches OPUS"

        r = await c.get("/panel/config")
        assert not {"album_id", "person_ids"} & set(r.json())

        StubPlayer.fail = True
        r = await c.get("/photos/random")
        assert r.status_code == 502
        StubPlayer.fail = False

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "photouser", "password": "userpw12"})
        assert r.status_code == 200
        r = await c.get("/photos/random")
        assert r.status_code == 200, "any signed-in user (the wall panel) gets the slideshow"

    await _cleanup(pool)
    await pool.close()
