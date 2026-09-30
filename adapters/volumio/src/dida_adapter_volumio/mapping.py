"""Volumio ↔ canonical-capability translation.

The iFi Zen Stream runs Volumio; its native REST API (`/api/v1/`) exposes a
richer, more reliable now-playing snapshot than the DLNA/UPnP renderer we used
before (real sample-rate/bit-depth, correct album-art for the device's own
sources, no getState desync). This module keeps the Volumio-specific decoding
(entity_id slug, transport vocabulary, getState → capability snapshot) out of the
adapter's control flow.
"""

from __future__ import annotations

import contextlib
import re

NAMESPACE = "volumio"

_SLUG = re.compile(r"[^a-z0-9_-]+")

# Query key naming the codec a station really sends — see mpd_uri().
_HINT = "dida_codec"

# File suffix → the codec family it promises, in radio_probe's vocabulary. Only a
# suffix that names a DIFFERENT family than the stream really sends is a lie worth
# correcting: `.m4a` on AAC is the same family and already decodes right. Anything
# absent here (a query token, a path like /aac-320) tells MPD nothing, so it falls
# back to the stream's Content-Type on its own and needs no hint from us.
_SUFFIX_CODEC = {
    "mp3": "mp3", "mp2": "mp3",
    "aac": "aac", "m4a": "aac", "mp4": "aac", "aacp": "aac",
    "ogg": "ogg", "oga": "ogg", "opus": "opus",
    "flac": "flac", "wav": "wav", "wma": "wma",
}

# Volumio playback status → our canonical media_transport vocabulary.
_TRANSPORT = {
    "play": "playing",
    "pause": "paused",
    "stop": "stopped",
}


def entity_id(name: str) -> str:
    """Stable, subject-safe entity_id from the device name (e.g. 'iFi' → volumio:ifi)."""
    slug = _SLUG.sub("_", (name or "").strip().lower()).strip("_") or "player"
    return f"{NAMESPACE}:{slug}"


def normalise_transport(status: object) -> str:
    return _TRANSPORT.get(str(status or "").lower(), str(status or "idle").lower())


def _clean(text: object) -> str | None:
    if not text:
        return None
    s = str(text).strip()
    if not s or s.lower() in ("unknown", "unknown artist", "unknown album"):
        return None
    return s


# trackType values that are transport containers, not audio codecs — no quality.
_NON_CODEC = {"WEBRADIO", "HTTP", "HTTPS", "STREAMING", ""}


def _quality(state: dict) -> str:
    """Compose a human quality string from Volumio's real playback fields.
    e.g. trackType 'flac' + '44.1 kHz' + '16 bit' → 'FLAC 44.1kHz/16bit'. This is
    the DEVICE's measured format (authoritative), better than probing the URL.
    Returns '' for radio/streams (trackType is a container, not a real codec)."""
    codec = str(state.get("trackType") or "").strip().upper()
    if codec in _NON_CODEC:
        codec = ""  # container, not a codec — but sample-rate/bit-depth may be real
    sr = str(state.get("samplerate") or "").replace(" ", "")
    bd = str(state.get("bitdepth") or "").replace(" ", "")
    rate = f"{sr}/{bd}" if sr and bd else sr or bd
    parts = [p for p in (codec, rate) if p]
    return " ".join(parts)


def mpd_uri(url: str, ext: str) -> str:
    """A station's stream url as we hand it to MPD, with a codec hint when the
    station's filename lies about what it serves.

    MPD 0.20 picks its decoder from the url's file SUFFIX. Yammat's mount is named
    `yammat.mp3` but has served AAC ever since it left MP3 behind, so MPD hands the
    stream to `mad` (the MP3 decoder), decodes 11025Hz garbage and goes silent while
    still reporting `playing` — measured on the Zen, where every other station has no
    misleading suffix and correctly probes `faad`/`mad`. MPD reads the suffix as the
    text after the FINAL '.', so a `?dida_codec=.aac` tail names the codec the station
    ACTUALLY sends (`ext`, read from its Content-Type by radio_probe). Icecast ignores
    the query. Only a suffix that CONTRADICTS the measured codec is corrected: a url
    with no suffix, or an honest one, already decodes right and is left untouched.
    """
    if not ext:
        return url                       # codec unknown — nothing to correct with
    tail = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    if "." not in tail:
        return url
    promised = _SUFFIX_CODEC.get(tail.rsplit(".", 1)[-1].lower())
    if promised is None or promised == ext:
        return url
    return f"{url}{'&' if '?' in url else '?'}{_HINT}=.{ext}"


def station_url(uri: str) -> str:
    """Our codec hint stripped back off a uri → the station's own url (the inverse of
    mpd_uri). The device echoes back what we cast, so this is what turns a getState
    uri into something that matches a station's own url again."""
    for sep in ("?", "&"):
        cut = uri.find(f"{sep}{_HINT}=.")
        if cut != -1:
            return uri[:cut]
    return uri


def source_of(uri: str, service: str) -> str:
    """Classify the current stream's origin for the signal-path view. A record the
    house put on is recognised by OPUS's stream path; the device's own apps (Tidal/
    Spotify Connect, its webradio) are 'external'.

    Radio is NOT classified here: a station url is arbitrary, so no path pattern can
    identify one (stream.yammat.fm/radio/… would match a '/radio/' rule that
    stream.radioparadise.com/aac-320 misses). The adapter knows radio positively —
    it matches the uri against the configured stations — and sets the source itself.
    """
    u = uri or ""
    if "/api/play/track/" in u:
        return "library"
    svc = (service or "").lower()
    if svc in ("spop", "spotify"):
        return "external"
    if u.startswith("http"):
        return "external"
    return ""


def read_state(state: dict, host: str) -> dict[str, object]:
    """Snapshot a Volumio getState dict into {capability: value} for canonical caps.

    Only includes a capability when Volumio actually reports it (never a phantom).
    seek is milliseconds → seconds; albumart relative paths → absolute on the host.
    """
    out: dict[str, object] = {"media_transport": normalise_transport(state.get("status"))}

    vol = state.get("volume")
    if vol is not None and not state.get("disableVolumeControl"):
        with contextlib.suppress(TypeError, ValueError):
            out["volume"] = max(0, min(100, int(vol)))
    mute = state.get("mute")
    if mute is not None:
        out["mute"] = bool(mute)

    title = _clean(state.get("title"))
    if title is not None:
        out["media_title"] = title
    artist = _clean(state.get("artist"))
    if artist is not None:
        out["media_artist"] = artist
    album = _clean(state.get("album"))
    if album is not None:
        out["media_album"] = album

    art = state.get("albumart")
    if art:
        art = str(art)
        if art.startswith("/"):
            art = f"http://{host}{art}"
        out["media_art"] = art

    # duration seconds (0/absent = live stream).
    dur = state.get("duration")
    out["media_duration"] = int(dur) if dur else 0
    # media_position is intentionally NOT snapped here — the adapter publishes it
    # sparsely (baseline + drift from state["seek"]) so the UI extrapolates the
    # playhead between the ~2s polls instead of us emitting a position update every poll.

    q = _quality(state)
    if q:
        out["media_quality"] = q
    return out
