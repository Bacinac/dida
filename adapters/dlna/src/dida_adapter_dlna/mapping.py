"""DLNA ↔ canonical-capability translation.

Keeps the protocol-specific bits (slugging an entity_id, normalising the UPnP
transport-state vocabulary, pulling now-playing fields off a DmrDevice) out of
the adapter's control flow.
"""

from __future__ import annotations

import re

NAMESPACE = "dlna"

# NATS subjects (dida.state.<entity_id>) can't contain spaces/dots/wildcards.
_SLUG = re.compile(r"[^a-z0-9_-]+")


def entity_id(friendly_name: str) -> str:
    """Stable, subject-safe entity_id from a renderer's friendly name."""
    slug = _SLUG.sub("_", (friendly_name or "").strip().lower()).strip("_") or "renderer"
    return f"{NAMESPACE}:{slug}"


# UPnP AVTransport TransportState → our canonical media_transport vocabulary.
# We keep a small, stable set the UI knows how to render; anything unexpected
# passes through lower-cased rather than being dropped.
_TRANSPORT = {
    "PLAYING": "playing",
    "PAUSED_PLAYBACK": "paused",
    "PAUSED_RECORDING": "paused",
    "STOPPED": "stopped",
    "NO_MEDIA_PRESENT": "idle",
    "TRANSITIONING": "buffering",
    "RECORDING": "playing",
}


def normalise_transport(state: object) -> str:
    """Map a DmrDevice.transport_state (enum or str) to our vocabulary."""
    if state is None:
        return "idle"
    raw = getattr(state, "value", state)
    return _TRANSPORT.get(str(raw).upper(), str(raw).lower())


def _clean(text: object) -> str | None:
    """A non-empty display string, or None. DLNA servers love to stuff
    'Unknown'/'unknown' into empty metadata — treat those as absent."""
    if not text:
        return None
    s = str(text).strip()
    if not s or s.lower() in ("unknown", "unknown artist", "unknown album"):
        return None
    return s


def read_renderer(dmr) -> dict[str, object]:
    """Snapshot a DmrDevice into {capability: value} for the canonical caps.

    Only includes a capability when the renderer actually reports it, so we
    never publish a phantom value. Volume is rescaled 0..1 → 0..100; durations
    are whole seconds.
    """
    out: dict[str, object] = {"media_transport": normalise_transport(getattr(dmr, "transport_state", None))}

    vol = getattr(dmr, "volume_level", None)
    if vol is not None:
        out["volume"] = max(0, min(100, round(float(vol) * 100)))
    muted = getattr(dmr, "is_volume_muted", None)
    if muted is not None:
        out["mute"] = bool(muted)

    title = _clean(getattr(dmr, "media_title", None))
    if title is not None:
        out["media_title"] = title
    artist = _clean(getattr(dmr, "media_artist", None))
    if artist is not None:
        out["media_artist"] = artist
    album = _clean(getattr(dmr, "media_album_name", None))
    if album is not None:
        out["media_album"] = album
    art = getattr(dmr, "media_image_url", None) or getattr(dmr, "media_album_art_uri", None)
    if art:
        out["media_art"] = str(art)

    # Always emit duration (0 = live stream / no fixed length) so switching from
    # a finite track to internet radio clears the previous track's duration.
    dur = getattr(dmr, "media_duration", None)
    out["media_duration"] = int(dur) if dur else 0
    return out
