"""HEOS ↔ canonical-capability translation.

A HEOS player maps onto the SAME media capability group as a DLNA renderer
(`media_transport`, `volume`, `mute`, now-playing metadata) — so a HEOS player
appears in the exact same media UI, but here volume/transport actually work
(they're the Denon/Marantz control plane, unlike UPnP RenderingControl).
"""

from __future__ import annotations

import re

NAMESPACE = "heos"

_SLUG = re.compile(r"[^a-z0-9_-]+")

# HEOS PlayState.value → our canonical media_transport vocabulary.
_TRANSPORT = {"play": "playing", "pause": "paused", "stop": "stopped"}


def entity_id(name: str) -> str:
    slug = _SLUG.sub("_", (name or "").strip().lower()).strip("_") or "player"
    return f"{NAMESPACE}:{slug}"


def _clean(text: object) -> str | None:
    if not text:
        return None
    s = str(text).strip()
    return s or None


def read_player(player) -> dict[str, object]:
    """Snapshot a pyheos HeosPlayer into {capability: value} for the media caps.

    HEOS is "just the player" here: transport + now-playing only. Volume/mute are
    deliberately NOT read — on a Denon/Marantz those belong to the AVR control
    plane (the denon adapter), so there's a single volume authority per device.
    Durations come from HEOS in milliseconds; we publish whole seconds (0 = live)."""
    out: dict[str, object] = {}

    state = getattr(player, "state", None)
    raw = getattr(state, "value", state)
    out["media_transport"] = _TRANSPORT.get(str(raw).lower(), "idle") if raw else "idle"

    npm = getattr(player, "now_playing_media", None)
    if npm is not None:
        # For radio HEOS fills `station`; for tracks `song` + `artist` + `album`.
        title = _clean(getattr(npm, "song", None)) or _clean(getattr(npm, "station", None))
        if title:
            out["media_title"] = title
        artist = _clean(getattr(npm, "artist", None))
        if artist:
            out["media_artist"] = artist
        album = _clean(getattr(npm, "album", None))
        if album:
            out["media_album"] = album
        art = getattr(npm, "image_url", None)
        if art:
            out["media_art"] = str(art)
        dur_ms = getattr(npm, "duration", None) or 0
        out["media_duration"] = int(dur_ms / 1000) if dur_ms else 0

    return out


def read_position(player) -> int | None:
    """Current playhead in whole seconds, or None if unknown."""
    npm = getattr(player, "now_playing_media", None)
    if npm is None:
        return None
    pos_ms = getattr(npm, "current_position", None)
    return int(pos_ms / 1000) if pos_ms is not None else None
