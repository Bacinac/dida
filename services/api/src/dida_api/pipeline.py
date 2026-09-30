"""Audio signal-path ('pipeline') for a media renderer — what the now-playing
audio actually passes through, source → renderer, measured at every stage.

Every node is something DIDA MEASURES. The chain ends at the DAC's digital input.
Two renderers report what they decode: a Volumio device, whose own
`/api/v1/getState` gives the samplerate/bitdepth, and OPUS · Player's DAC, fed by
the MPD beside the player, which gives the format it opened the card at. Either
is cross-checked against the source format → a provable bit-perfect chain. The
analog tail (amp → speakers) is deliberately NOT shown: DIDA can't measure it, so
it would be static decoration.

This endpoint returns NEUTRAL data (source enum, format strings, measured/match
flags). The UI owns labels, icons and i18n.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import httpx
from dida_core import opus_base
from fastapi import APIRouter, Depends, HTTPException, Request

from dida_api import opus
from dida_api.auth import AuthUser, current_user
from dida_api.visibility import is_hidden

log = logging.getLogger("dida.api.pipeline")

router = APIRouter(prefix="/media", tags=["media"])
# Schema (media_renderer_output) lives in db/migrations (0003), applied by the
# migration runner at api boot — not self-healed here.

# How each source reaches the renderer (drives the transform node):
#   direct      → library: OPUS serves the original file untouched, the renderer
#                 fetches it over the LAN with a ticket
#   none        → external: the renderer plays its own app; DIDA didn't route it
#   radio       → NOT proxied at all: every renderer pulls the station itself, so
#                 nobody is in the audio path (no transform node, transport is the
#                 internet). The volumio adapter's mpd_uri names the codec for the
#                 one station whose address lies about it.
_TRANSFORM = {"library": "direct", "external": "none"}

# OPUS · Player's DAC: its renderer is the player's own MPD, asked through the
# player rather than probed at an address
DAC = "opus:dac"

# Renderer node label = how DIDA drives the device (its adapter), not a fixed wire
# protocol. The iFi is now the native Volumio adapter (it used to be DLNA/UPnP).
_RENDERER_PROTO = {
    "volumio": "Volumio", "dlna": "DLNA", "cast": "Cast",
    "heos": "HEOS", "denon": "Denon", "harmony": "Harmony",
}


def _nums(s: str) -> tuple[float | None, float | None]:
    """(kHz, bits) parsed from a format string — 'FLAC 44.1kHz/16bit' or
    Volumio's '44.1 kHz' / '16 bit'."""
    khz = re.search(r"([\d.]+)\s*kHz", s, re.I)
    bits = re.search(r"(\d+)\s*bit", s, re.I)
    return (float(khz[1]) if khz else None, float(bits[1]) if bits else None)


def _dsd_multiple(fmt: str) -> int | None:
    """DSD64/128/256 from what a renderer reports: MPD says 'DSD64', Volumio says
    '2.82 MHz / 1 bit' (a multiple of 44.1 kHz)."""
    named = re.search(r"DSD\s*(\d+)", fmt, re.I)
    if named:
        return int(named[1])
    mhz = re.search(r"([\d.]+)\s*MHz", fmt, re.I)
    return round(float(mhz[1]) * 1000 / 44.1) if mhz else None


def _rate_class(khz: float | None, dsd: int | None) -> str | None:
    """The colour the ZEN DAC V2's own LED shows for what it receives, as a neutral
    tier the UI paints: yellow for PCM 44.1/48, white for every higher PCM rate it
    takes (up to 384 kHz), cyan for DSD64/128, red for DSD256. MQA (green, blue)
    is left out — only the DAC knows it, by decoding the bits. A rate the DAC does
    not take has no colour."""
    if dsd:
        return "dsd256" if dsd >= 256 else "dsd"
    if not khz:
        return None
    if khz <= 48:
        return "r48"
    if khz <= 384:
        return "hires"
    return None


def _format_match(source: str, live_format: str | None) -> bool | None:
    """Whether the renderer decoded at the exact format the source states —
    the bit-perfect proof, or the disproof, whichever it turns out to be.
    Unmeasured (`None`) when either side has nothing to compare: a renderer
    not currently playing, or a source whose format nobody wrote down.

    DSD states a family (64/128/256), never a kHz/bit pair — the two can never
    both be present in the same string, so trying DSD first is unambiguous."""
    if not live_format or not source:
        return None
    src_dsd, live_dsd = _dsd_multiple(source), _dsd_multiple(live_format)
    if src_dsd or live_dsd:
        return src_dsd is not None and src_dsd == live_dsd
    a, b = _nums(source), _nums(live_format)
    return (a[0] == b[0]) and (a[1] == b[1]) if a[0] and b[0] else None


async def _probe_renderer(url: str) -> dict | None:
    """The renderer's own report of what it's decoding (Volumio getState)."""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            d = (await c.get(url)).json()
    except Exception as exc:
        log.debug("renderer probe failed %s: %s", url, exc, exc_info=True)
        return None
    fmt = " / ".join(p for p in (str(d.get("samplerate") or ""), str(d.get("bitdepth") or "")) if p)
    # Bit-perfect / Exclusive mode: Volumio bypasses the software mixer + volume,
    # so nothing scales the samples. `disableVolumeControl` is its tell; mixer None
    # and full unattenuated volume reinforce it.
    exclusive = bool(d.get("disableVolumeControl")) and not d.get("mixer") \
        and not d.get("mute") and (d.get("volume") in (None, 100))
    return {
        "format": fmt or None,
        "bitrate": str(d.get("bitrate") or "") or None,
        "channels": d.get("channels"),
        "service": str(d.get("service") or "") or None,
        "exclusive": exclusive,
        # Raw output-stage state, shown live (not as prose):
        "volume": d.get("volume"),
        "mixer": d.get("mixer"),
        "volume_control_disabled": bool(d.get("disableVolumeControl")),
        "mute": bool(d.get("mute")),
    }


def _mpd_format(audio: str) -> tuple[str | None, int | None]:
    """MPD's `rate:bits:channels` → ('44.1 kHz / 16 bit', channels).
    DSD is `dsd64:2`, and a lossy stream decodes to float: `44100:f:2`."""
    parts = (audio or "").split(":")
    if len(parts) == 2 and parts[0].lower().startswith("dsd"):
        return parts[0].upper(), int(parts[1]) if parts[1].isdigit() else None
    if len(parts) != 3 or not parts[0].isdigit():
        return None, None
    rate, bits, channels = parts
    count = int(channels) if channels.isdigit() else None
    depth = "float" if bits == "f" else f"{bits} bit"
    return f"{int(rate) / 1000:g} kHz / {depth}", count


async def _measure_dac(pool) -> dict | None:
    """The player's DAC's own account of what it is fed, from the MPD that feeds it."""
    try:
        now = await opus.dac_now(pool)
    except opus.OpusUnavailable as exc:
        log.debug("the DAC was not measured: %s", exc)
        return None
    fmt, channels = _mpd_format(str(now.get("format") or ""))
    # What in MPD could change a sample while keeping the format: a software
    # volume (left out when there is no mixer), replay gain, a crossfade. None of
    # them, and the card gets the file's samples.
    gain = now.get("replay_gain")
    crossfade = now.get("crossfade")
    exclusive = now.get("volume") in (None, -1) and gain == "off" and crossfade == 0
    return {
        "dac": now.get("name"),
        "format": fmt,
        "bitrate": f"{now['bitrate']} kbps" if now.get("bitrate") else None,
        "channels": channels,
        "service": None,
        "exclusive": exclusive,
        "volume": None,
        "mixer": None,
        "volume_control_disabled": now.get("volume") in (None, -1),
        "mute": False,
        "replay_gain": gain,
        "crossfade": crossfade,
        "health": now.get("health"),
    }


@router.get("/pipeline/{entity_id}")
async def pipeline(entity_id: str, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    pool = request.app.state.pool
    if await is_hidden(pool, user, entity_id):
        raise HTTPException(404, "unknown entity")
    rows = await pool.fetch("SELECT capability, value FROM current_state WHERE entity_id = $1", entity_id)
    if not rows:
        raise HTTPException(404, "unknown entity")
    st = {r["capability"]: r["value"] for r in rows}

    source = str(st.get("media_source") or "")
    quality = str(st.get("media_quality") or "")
    playing = str(st.get("media_transport") or "") in ("playing", "buffering")
    erow = await pool.fetchrow("SELECT name, adapter FROM entities WHERE entity_id = $1", entity_id)
    name = (erow and erow["name"]) or entity_id
    adapter = (erow and erow["adapter"]) or entity_id.split(":", 1)[0]
    is_dac = entity_id == DAC
    cfg = await pool.fetchrow("SELECT * FROM media_renderer_output WHERE entity_id = $1", entity_id)
    probe_url = cfg["probe_url"] if cfg else None

    nodes: list[dict] = []

    # 1) Source — where the audio originates + the format it's served at.
    nodes.append({"kind": "source", "source": source or "unknown",
                  "format": quality or None, "measured": bool(quality)})

    # 2) Transform — only when a file is served (the library): pass-through, so
    #    bit-perfect. Radio is never proxied — the renderer fetches the station
    #    itself, so there is nothing of ours in between.
    mode = _TRANSFORM.get(source, "none")
    if mode == "direct":
        nodes.append({"kind": "transform", "mode": mode, "bitperfect": True, "measured": True})

    # 3) Transport — a file reaches the renderer over the LAN from OPUS; a station
    #    is pulled straight off the internet by the renderer itself, so nobody is
    #    the sender and there is no LAN hop to name.
    media_host = urllib.parse.urlparse(await opus_base(pool)).hostname
    # The player's DAC is fed on the player's own host: a file never touches the
    # network, so there is no transport to draw; a station still comes in over it.
    renderer_host = media_host if is_dac else (urllib.parse.urlparse(probe_url).hostname
                                               if probe_url else None)
    if source == "library" and not is_dac:
        nodes.append({"kind": "transport", "via": "http", "from": media_host, "to": renderer_host})
    elif source == "radio":
        nodes.append({"kind": "transport", "via": "internet", "from": None, "to": renderer_host})

    # 4) Renderer — the device, plus its OWN report of what it's decoding (live),
    #    cross-checked against the source format → the bit-perfect proof.
    if not playing:
        live = None
    elif is_dac:
        live = await _measure_dac(pool)
    else:
        live = await _probe_renderer(probe_url) if probe_url else None
    # Lossy radio decoded to PCM is never proof of a lossless/bit-perfect source.
    match = _format_match(quality, (live or {}).get("format")) if source == "library" else None
    exclusive = bool(live and live.get("exclusive"))
    proto = "OPUS" if is_dac else _RENDERER_PROTO.get(adapter, adapter.upper() or "—")
    # Annotate with the device's OWN service only when it's playing its own app
    # (external); for what the house put on, the service is internal (webradio/mpd).
    if source == "external" and live and live.get("service"):
        proto = f"{proto} → {live['service'].upper()}"
    # The DAC's LED colour for what it receives (= the renderer's output). A renderer
    # that reports no rate for radio still decodes every station to 44.1 or 48 kHz
    # PCM — the yellow the LED shows for a stream.
    live_fmt = (live or {}).get("format") or ""
    rate_class = _rate_class(_nums(live_fmt)[0], _dsd_multiple(live_fmt))
    if rate_class is None and source == "radio" and playing:
        rate_class = "r48"
    # the DAC's renderer is the MPD in front of it, not the device the house names
    nodes.append({"kind": "renderer", "name": "MPD" if is_dac else name, "proto": proto,
                  "format": (live or {}).get("format"), "bitrate": (live or {}).get("bitrate"),
                  "channels": (live or {}).get("channels"), "rate_class": rate_class,
                  "measured": bool(live and live.get("format")), "match": match})

    # 5) DAC — a digital DAC fed over USB/SPDIF. The renderer outputs bit-perfect,
    #    so the DAC's digital INPUT is the format above (measured). We stop here:
    #    after the DAC it's analog (amp, speakers) — DIDA can't measure that.
    if is_dac:
        nodes.append({"kind": "dac", "name": (live or {}).get("dac") or name, "link": "USB",
                      "format": (live or {}).get("format"), "rate_class": rate_class,
                      "measured": bool(live and live.get("format"))})
    elif cfg and cfg["dac"]:
        nodes.append({"kind": "dac", "name": cfg["dac"], "link": cfg["link"],
                      "format": (live or {}).get("format"), "rate_class": rate_class,
                      "measured": bool(live and live.get("format"))})

    # "bit-perfect" is claimed ONLY when BOTH hold: the renderer decodes at the
    # exact source format (no resampling / requantisation) AND it's in Exclusive
    # mode (no software volume/mixer touching the samples). format_match alone is
    # the weaker "lossless format preserved".
    output = None
    if live:
        output = {"volume": live.get("volume"), "mixer": live.get("mixer"),
                  "volume_control_disabled": live.get("volume_control_disabled"),
                  "mute": live.get("mute"),
                  "replay_gain": live.get("replay_gain"), "crossfade": live.get("crossfade")}
    return {"entity_id": entity_id, "name": name, "playing": playing,
            "source": source or None, "quality": quality or None,
            "format_match": match, "exclusive": exclusive, "output": output,
            "verified": (match is True) and exclusive, "nodes": nodes,
            "health": (live or {}).get("health")}


# What the renderer is playing is read back off it: the streamer's getState `uri`
# is the ticketed OPUS address, which names the song.
_TRACK_IN_URI = re.compile(r"/api/play/track/(\d+)/")


def _lrc(lines: list[dict]) -> str | None:
    """OPUS hands lyrics over as timed lines; the card reads LRC."""
    out = []
    for line in lines:
        at = float(line.get("at") or 0)
        m, s = divmod(at, 60)
        out.append(f"[{int(m):02d}:{s:05.2f}] {line.get('text') or ''}")
    return "\n".join(out) or None


@router.get("/lyrics/{entity_id}")
async def lyrics(entity_id: str, request: Request, user: AuthUser = Depends(current_user)) -> dict:
    """Lyrics for whatever this renderer is playing — {text, subtitles} (LRC), nulls
    if none. A song is a row in OPUS, which keeps the words; the renderer's own
    report of what it is fetching says which row."""
    pool = request.app.state.pool
    if await is_hidden(pool, user, entity_id):
        raise HTTPException(404, "unknown entity")
    source = await pool.fetchval(
        "SELECT value FROM current_state WHERE entity_id = $1 AND capability = 'media_source'", entity_id)
    if str(source or "") != "library":
        return {"text": None, "subtitles": None}
    # the player says outright which song each of its outputs is playing
    if entity_id.startswith("opus:"):
        try:
            now = await (opus.dac_now(pool) if entity_id == DAC else opus.tv_now(pool))
            if now.get("kind") != "track" or not now.get("track_id"):
                return {"text": None, "subtitles": None}
            words = await opus.lyrics(pool, int(now["track_id"]))
        except opus.OpusUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"text": words.get("plain") or None, "subtitles": _lrc(words.get("lines") or [])}
    cfg = await pool.fetchrow("SELECT probe_url FROM media_renderer_output WHERE entity_id = $1", entity_id)
    if not cfg or not cfg["probe_url"]:
        return {"text": None, "subtitles": None}
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            uri = str((await c.get(cfg["probe_url"])).json().get("uri") or "")
    except Exception:
        log.debug("lyrics lookup failed", exc_info=True)
        return {"text": None, "subtitles": None}
    m = _TRACK_IN_URI.search(uri)
    if not m:
        return {"text": None, "subtitles": None}
    try:
        words = await opus.lyrics(pool, int(m.group(1)))
    except opus.OpusUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"text": words.get("plain") or None, "subtitles": _lrc(words.get("lines") or [])}
