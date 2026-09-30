"""OPUS · Player, asked over HTTP.

What there is to listen to is not the house's to keep: the records, the songs
and the stations live in OPUS, and so do the bytes. DIDA holds the devices and
the rooms; when somebody in a room asks for music, this module asks the player
what exists and where its bytes are, and the adapters hand that to the device.
Nothing is copied here — a station list of ours would be the list still naming a
station a month after it was removed over there.

One credential, the household's service token. In OPUS it is nobody's, so it
reaches only what the house may: the shelf, the queue, the stations, the
television and the wall's photographs. Nothing the browser sends is passed
through as a path.
"""

from __future__ import annotations

import logging
import time

import httpx
from dida_core import host_setting

from dida_api.common import stored_api_key

log = logging.getLogger("dida.api.opus")

TOKEN_HEADER = "X-OPUS-Token"
_TIMEOUT = 10.0
_STATIONS_TTL = 60.0


class OpusUnavailable(Exception):
    """OPUS is not configured, not reachable, or refused — said out loud, never
    read as an empty shelf."""


_client: httpx.AsyncClient | None = None
_client_for: tuple[str, str] | None = None
_stations: tuple[float, list[dict]] = (0.0, [])


async def base(pool) -> str:
    return (await host_setting(pool, "opus_url")).rstrip("/")


async def configured(pool) -> bool:
    return bool(await base(pool)) and bool(await stored_api_key(pool, "opus_token"))


async def _http(pool) -> httpx.AsyncClient:
    """One connection pool per (address, token), rebuilt when either changes."""
    global _client, _client_for
    url = await base(pool)
    token = await stored_api_key(pool, "opus_token") or ""
    if not url or not token:
        raise OpusUnavailable("OPUS is not configured — set the player's address and token in Settings → Network")
    if _client is None or _client_for != (url, token):
        if _client is not None:
            await _client.aclose()
        _client = httpx.AsyncClient(base_url=f"{url}/api", headers={TOKEN_HEADER: token},
                                    timeout=_TIMEOUT)
        _client_for = (url, token)
    return _client


async def _answered(pool, path: str, params: dict) -> httpx.Response:
    http = await _http(pool)
    try:
        resp = await http.get(path, params=params or None)
    except httpx.HTTPError as exc:
        raise OpusUnavailable(f"OPUS did not answer {path}: {exc}") from exc
    if resp.status_code == 401:
        raise OpusUnavailable("OPUS refused the token — check the token in Settings → Network")
    if resp.status_code == 404:
        raise OpusUnavailable(f"OPUS has no {path}")
    if resp.status_code >= 400:
        raise OpusUnavailable(f"OPUS answered {resp.status_code} for {path}")
    return resp


async def get(pool, path: str, **params) -> dict | list:
    return (await _answered(pool, path, params)).json()


async def fetch(pool, path: str) -> bytes:
    return (await _answered(pool, path, {})).content


class OpusRefused(Exception):
    """OPUS answered, and the answer was no."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


async def post(pool, path: str, body: dict) -> dict:
    http = await _http(pool)
    try:
        resp = await http.post(path, json=body)
    except httpx.HTTPError as exc:
        raise OpusUnavailable(f"OPUS did not answer {path}: {exc}") from exc
    if resp.status_code == 401:
        raise OpusUnavailable("OPUS refused the token — check the token in Settings → Network")
    if resp.status_code >= 400:
        try:
            detail = str(resp.json().get("detail") or "")
        except ValueError:
            detail = ""
        raise OpusRefused(resp.status_code, detail or f"OPUS answered {resp.status_code} for {path}")
    return resp.json() if resp.content else {}


async def artists(pool) -> list[dict]:
    return [_artist(a) for a in await get(pool, "/library/music")]


async def artist(pool, artist_id: int) -> dict:
    return await get(pool, f"/library/artist/{artist_id}")


async def release(pool, release_id: int, prefer: str = "stereo") -> dict:
    return await get(pool, f"/library/release/{release_id}", prefer=prefer)


async def queue(pool, kind: str, item_id: int, prefer: str = "stereo") -> dict:
    """A record, a song or a station as the devices can be handed it: every row
    with the ticketed address its bytes are fetched from."""
    return await get(pool, "/play/queue", kind=kind, id=item_id, prefer=prefer)


async def stations(pool) -> list[dict]:
    """The stations, in the player's order, kept for a minute: the tuner, the
    adapters classifying what a box is playing and the browse page all ask, and
    none of them needs an answer fresher than that."""
    global _stations
    at, kept = _stations
    now = time.monotonic()
    if kept and now - at < _STATIONS_TTL:
        return kept
    try:
        found = await get(pool, "/radio/stations")
    except OpusUnavailable:
        if kept:
            return kept
        raise
    rows = [_station(s) for s in (found.get("stations") if isinstance(found, dict) else [])]
    _stations = (now, rows)
    return rows


def _artist(a: dict) -> dict:
    return {"id": a["id"], "name": a.get("title") or "", "image": a.get("image"),
            "held": a.get("held") or 0, "begin_year": a.get("year"),
            "country": a.get("country")}


def _station(s: dict) -> dict:
    return {"id": s["id"], "name": s.get("title") or "", "genre": s.get("subtitle") or "",
            "url": s.get("url") or "", "logo": s.get("image")}


async def art(pool, u: str, w: int) -> httpx.Response:
    """A picture through the player's own picture route, which is what keeps
    the house from being an open proxy: the player fetches only what its
    catalogue names."""
    http = await _http(pool)
    try:
        return await http.get("/art", params={"u": u, "w": w})
    except httpx.HTTPError as exc:
        raise OpusUnavailable(f"OPUS did not answer for a picture: {exc}") from exc


async def lyrics(pool, track_id: int) -> dict:
    return await get(pool, f"/play/track/{track_id}/lyrics")


async def tv_now(pool) -> dict:
    """What the player says its television is doing, and whether it is open at all."""
    return await get(pool, "/tv/now")


async def dac_now(pool) -> dict:
    """What the player's DAC is playing, in the format it was measured and fed."""
    return await get(pool, "/dac/now")


async def tv_links(pool) -> list[dict]:
    return (await get(pool, "/tv/links")).get("links") or []


async def tv_items(pool, key: str, lang: str) -> list[dict]:
    return (await get(pool, f"/tv/items/{key}", lang=lang)).get("items") or []


async def tv_open(pool, body: dict) -> dict:
    return await post(pool, "/tv/open", body)
