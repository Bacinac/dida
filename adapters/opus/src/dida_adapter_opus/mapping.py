"""OPUS · Player's account of its two outputs ↔ canonical media capabilities.

The television is the player's own app on the living-room box; the DAC hangs off
the player's host and plays through the MPD beside it. Pure functions, so the
translation is tested without a player or a bus."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

NAMESPACE = "opus"
TV = "opus:tv"
TV_NAME = "OPUS TV"
DAC = "opus:dac"

CAPABILITIES = [
    "media_transport", "media_title", "media_artist", "media_album", "media_art",
    "media_position", "media_duration", "media_source",
]
# what makes the signal path worth opening: the file's own format
DAC_CAPABILITIES = [*CAPABILITIES, "media_quality"]

# What both outputs obey. The capability's other commands either mean something
# the adapter builds an order for (play_queue, play_media) or nothing either
# output can do.
CONTROLS = {"play", "pause", "play_pause", "stop", "next", "previous"}

# Where what is on came from, in the vocabulary the rest of the house reads. A
# song is the library's; a station is an address somebody else keeps alive and
# NOT the house's radio — "radio" would send next/previous to the house's tuner,
# which plays on another device. A film is neither: it is the player's own app
# playing its own screen.
_SOURCE = {"track": "library", "station": "url", "movie": "external", "episode": "external"}
# On the DAC a station IS the house's radio: the tuner plays on it, so next and
# previous belong to the tuner.
_DAC_SOURCE = {"track": "library", "station": "radio", "external": "external"}


def read_now(now: dict) -> dict[str, object]:
    """The player's `/api/tv/now` → {capability: value}.

    Two ways of having nothing on, told apart because the house acts on the
    difference: `idle` is a television the player is not open on — nothing can be
    sent to it — and `stopped` is the player open with nothing playing, ready to
    be given something. Either way every field is cleared, so a film that ended
    does not stand on the card. Not open is deliberately NOT unreachable: the box
    is fine, and an unreachable device raises an alert every evening somebody
    watches the television instead."""
    if not now.get("listening"):
        return _empty("idle")
    transport = str(now.get("transport") or "")
    if transport not in ("playing", "paused"):
        return _empty("stopped")
    return {
        "media_transport": transport,
        "media_title": str(now.get("title") or ""),
        "media_artist": str(now.get("artist") or ""),
        "media_album": str(now.get("album") or ""),
        "media_art": str(now.get("cover_url") or ""),
        "media_source": _SOURCE.get(str(now.get("kind") or ""), ""),
        "media_position": _seconds(now.get("position")),
        "media_duration": _seconds(now.get("duration")),
    }


def read_dac(now: dict) -> dict[str, object]:
    """The player's `/api/dac/now` → {capability: value}. The DAC has no `idle`:
    it is always there to be given something, and a player that cannot reach it
    says so by not answering."""
    transport = str(now.get("transport") or "")
    health = now.get("health") or {}
    if now.get("kind") == "station" and transport != "paused" and (
        health.get("state") in ("recovering", "failed", "unavailable")
        or (transport == "playing" and now.get("error"))
    ):
        transport = "buffering"
    if transport not in ("playing", "paused", "buffering"):
        return {**_empty("stopped"), "media_quality": ""}
    kind = str(now.get("kind") or "")
    return {
        "media_transport": transport,
        "media_title": str(now.get("title") or ""),
        "media_artist": str(now.get("artist") or ""),
        "media_album": str(now.get("album") or ""),
        "media_art": str(now.get("cover_url") or ""),
        "media_source": _DAC_SOURCE.get(kind, ""),
        "media_position": _seconds(now.get("position")),
        "media_duration": _seconds(now.get("duration")),
        "media_quality": quality(now.get("codec"), now.get("sample_rate_hz"), now.get("bit_depth"))
        if kind == "track" else stream_quality(now) if kind == "station" else "",
    }


def stream_quality(now: dict) -> str:
    """Source codec/rate from a probe; compressed bitrate measured by MPD.

    A decoded PCM depth is NOT the quality of a lossy source.
    """
    stream = now.get("stream") or {}
    parts = [quality(stream.get("codec"), stream.get("sample_rate_hz"), None)]
    bitrate = now.get("bitrate")
    if isinstance(bitrate, (int, float)) and bitrate > 0:
        parts.append(f"{bitrate:g} kb/s")
    return " · ".join(p for p in parts if p)


# the file as OPUS · Library files a DSD rip under — the codec string
# probe_file records, never the family name the search side scores by
_DSD_CODECS = ("dsf", "dff")


def quality(codec: object, rate_hz: object, bits: object) -> str:
    """The file as the Library measured it — 'FLAC 44.1kHz/16bit', or 'DSD64'
    for the one format whose rate is a multiple of the CD one rather than a
    depth/rate pair of its own."""
    if str(codec or "").lower() in _DSD_CODECS:
        try:
            family = round(int(rate_hz) / 44100) if rate_hz else None  # type: ignore[arg-type]
        except (TypeError, ValueError):
            family = None
        return f"DSD{family}" if family else "DSD"
    parts = []
    if codec:
        parts.append(str(codec).upper())
    try:
        khz = f"{int(rate_hz) / 1000:g}kHz" if rate_hz else ""  # type: ignore[arg-type]
    except (TypeError, ValueError):
        khz = ""
    depth = f"{bits}bit" if bits else ""
    if khz or depth:
        parts.append("/".join(p for p in (khz, depth) if p))
    return " ".join(parts)


def _empty(transport: str) -> dict[str, object]:
    return {
        "media_transport": transport, "media_title": "", "media_artist": "",
        "media_album": "", "media_art": "", "media_source": "",
        "media_position": 0, "media_duration": 0,
    }


def _seconds(value: object) -> int:
    try:
        return max(0, int(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def order_from_queue(args: dict) -> dict | None:
    """A `play_queue` command → the body of the player's `/play` for either output.

    The player plays records by the library's own ids, not by addresses: it hands
    out its own tickets. A row that carries no id but an address is a station."""
    try:
        rows = json.loads(str(args.get("queue") or "[]"))
    except (TypeError, ValueError):
        return None
    tracks = []
    for n, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        track = _track(row, fallback_id=-(n + 1))
        if track is not None:
            tracks.append(track)
    if not tracks:
        return None
    try:
        start = int(args.get("start") or 0)
    except (TypeError, ValueError):
        start = 0
    return {"tracks": tracks, "start": max(0, min(start, len(tracks) - 1))}


def order_from_media(args: dict) -> dict | None:
    """A `play_media` command — a station — → the body of the player's `/play`."""
    uri = str(args.get("uri") or args.get("url") or "")
    if not uri.startswith("http"):
        return None
    return {"tracks": [{"id": -1, "title": str(args.get("title") or ""), "artist": "",
                       "album": "", "cover_url": str(args.get("art") or "") or None,
                       "codec": None, "url": uri}], "start": 0}


def order_from_address(args: dict) -> tuple[str, dict] | None:
    """A `play_media` of an `opus:` address → the television's own route and its
    body. These name what only the player's television does, each asked for by
    what the house says aloud rather than by an id:

    - `opus:photos` this day through the years, `opus:photos?family` the family,
      `opus:photos?person=Ema` somebody of the family by the name the house uses;
    - `opus:continue` what was being watched there;
    - `opus:next-episode` the episode after the one on.

    None for anything that is not one of them."""
    parts = urlsplit(str(args.get("uri") or ""))
    if parts.scheme != "opus":
        return None
    asked = parse_qs(parts.query, keep_blank_values=True)
    if parts.path == "photos":
        if "person" in asked:
            return ("/show", {"person": asked["person"][0]}) if asked["person"][0] else None
        return "/show", {"family": True} if "family" in asked else {}
    if parts.path == "continue":
        return "/resume", {}
    if parts.path == "next-episode":
        return "/control", {"command": "next_episode"}
    return None


def _track(row: dict, fallback_id: int) -> dict | None:
    art = str(row.get("art") or row.get("cover_url") or "") or None
    base = {"title": str(row.get("title") or ""), "artist": str(row.get("artist") or ""),
            "album": str(row.get("album") or ""), "cover_url": art}
    raw = row.get("id")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return {"id": raw, **base, "codec": row.get("codec") or None, "url": None}
    uri = str(row.get("uri") or row.get("url") or "")
    if uri.startswith("http") and "/api/play/track/" not in uri:
        return {"id": fallback_id, **base, "codec": None, "url": uri}
    return None
