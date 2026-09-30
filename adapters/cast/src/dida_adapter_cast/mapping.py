"""Cast ↔ canonical-capability translation."""

from __future__ import annotations

import re

NAMESPACE = "cast"

_SLUG = re.compile(r"[^a-z0-9_-]+")

# Cast MediaStatus.player_state → our media_transport vocabulary.
_TRANSPORT = {
    "PLAYING": "playing",
    "PAUSED": "paused",
    "BUFFERING": "buffering",
    "IDLE": "idle",
    "UNKNOWN": "idle",
}


def entity_id(name: str) -> str:
    slug = _SLUG.sub("_", (name or "").strip().lower()).strip("_") or "cast"
    return f"{NAMESPACE}:{slug}"


def normalise_transport(player_state: object) -> str:
    return _TRANSPORT.get(str(player_state or "").upper(), "idle")


def _clean(text: object) -> str | None:
    if not text:
        return None
    s = str(text).strip()
    return s or None


def read_media(status) -> dict[str, object]:
    """Cast MediaStatus → {capability: value} (transport + now-playing)."""
    out: dict[str, object] = {"media_transport": normalise_transport(getattr(status, "player_state", None))}
    title = _clean(getattr(status, "title", None))
    if title:
        out["media_title"] = title
    artist = _clean(getattr(status, "artist", None))
    if artist:
        out["media_artist"] = artist
    album = _clean(getattr(status, "album_name", None))
    if album:
        out["media_album"] = album
    images = getattr(status, "images", None) or []
    if images:
        url = getattr(images[0], "url", None)
        if url:
            out["media_art"] = str(url)
    dur = getattr(status, "duration", None)
    out["media_duration"] = int(dur) if dur else 0
    return out
