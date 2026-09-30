"""Shared internet-radio helpers for the media adapters (dlna, volumio).

The now-playing song and the true codec/bitrate of a station come from the stream's
own ICY metadata and headers — read here. These are pure, stateless probes; what an
adapter DOES with them differs (volumio hands the station straight to MPD and needs
the codec to name the decoder), and the radio-mode LIFECYCLE (when to start/stop the
poll, self-heal) stays in each adapter.

The stations themselves are OPUS's. The api asks the player and answers the bus;
an adapter that needs to know whether the box is on one of them asks the api,
never a table — there is no table.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("dida.media")

STATIONS_SUBJECT = "dida.radio.stations"

# ICY / Content-Type audio codec → display label.
CODEC = {
    "audio/mpeg": "MP3", "audio/mp3": "MP3", "audio/aac": "AAC", "audio/aacp": "AAC",
    "audio/x-aac": "AAC", "audio/mp4": "AAC", "application/ogg": "OGG", "audio/ogg": "OGG",
    "audio/flac": "FLAC", "audio/x-flac": "FLAC", "audio/wav": "WAV",
}

# Content-Type → the file extension that names this codec. Both consumers need the
# codec a station REALLY sends: dlna relays only these lossy ones, and volumio tells
# MPD what to decode with it when the station's own filename disagrees.
_EXT = {
    "audio/aac": "aac", "audio/aacp": "aac", "audio/x-aac": "aac", "audio/mp4": "aac",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
}


async def radio_stations(bus, timeout_s: float = 3.0) -> list[dict] | None:
    """The stations as the api currently has them from OPUS — `[{id, name, url,
    logo, genre}]` — or None when nothing answered. None is not an empty list:
    an adapter keeps its last snapshot on a blip rather than forgetting every
    station it knew."""
    try:
        resp = await bus.nc.request(STATIONS_SUBJECT, b"", timeout=timeout_s)
        rows = json.loads(resp.data)
    except Exception:
        log.debug("radio stations not answered", exc_info=True)
        return None
    return rows if isinstance(rows, list) else None


async def opus_base(source=None) -> str:
    """Where OPUS · Player answers, as the devices reach it. A stream address under
    it is one of ours — a record the house put on, not the box's own app."""
    from dida_core.db import host_setting

    return (await host_setting(source, "opus_url")).rstrip("/")


async def radio_probe(url: str) -> tuple[str, str]:
    """One header read of a radio stream → (quality, extension), e.g.
    ('AAC 320kbps', 'aac'). Quality is what the station actually sends (a relayed
    stream is re-encoded to lossless FLAC, so its own format would be the wrong badge).
    Extension '' when the codec is unknown (→ dlna casts direct, volumio adds no hint)."""
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with (
            aiohttp.ClientSession(timeout=timeout) as s,
            s.get(url, headers={"Icy-MetaData": "1"}) as resp,
        ):
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            br = (resp.headers.get("icy-br") or "").split(",")[0].strip()
            codec = CODEC.get(ct, ct.rsplit("/", 1)[-1].upper() if ct.startswith("audio/") else "")
            quality = " ".join(p for p in (codec, f"{br}kbps" if br.isdigit() else "") if p)
            return quality, _EXT.get(ct, "")
    except Exception:
        log.debug("radio probe of %s failed", url, exc_info=True)
        return "", ""


def icy_stream_title(url: str) -> str:
    """One-shot read of a stream's current ICY StreamTitle ('' if none). Connects
    with Icy-MetaData, skips one audio block, reads the first metadata block."""
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": "DIDA"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            metaint = resp.headers.get("icy-metaint")
            if not metaint:
                return ""
            resp.read(int(metaint))                       # skip one audio block
            length = (resp.read(1) or b"\x00")[0] * 16    # metadata length byte
            if length <= 0:
                return ""
            block = resp.read(length).rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        log.debug("stream title of %s unreadable", url, exc_info=True)
        return ""
    m = re.search(r"StreamTitle='([^']*)'", block)
    return (m.group(1).strip() if m else "")
