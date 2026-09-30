"""Regression tests for the Volumio REST (iFi Zen Stream) <-> canonical mapping.

Run inside the volumio adapter image (dida_adapter_volumio installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-volumio:latest \
      -c "python -m pytest tests/test_volumio_mapping.py"

Covers the entity_id slug, normalise_transport (including the quirk that an
UNKNOWN but truthy status is echoed back lower-cased rather than mapped to
"idle" — only a falsy status maps to "idle"), _clean (Volumio's literal
"Unknown"/"Unknown Artist"/"Unknown Album" placeholders filtered out), _quality
(codec + samplerate/bitdepth composition, container trackTypes contributing no
codec), source_of (path/service classification for the signal-path view), and
read_state end to end (volume clamp + disableVolumeControl + non-numeric volume,
mute 0-is-not-None, relative vs absolute albumart, duration fallback).
"""
import time

from dida_adapter_volumio.adapter import _STALL_AFTER, Player, VolumioAdapter
from dida_adapter_volumio.mapping import (
    _clean,
    _quality,
    entity_id,
    mpd_uri,
    normalise_transport,
    read_state,
    source_of,
    station_url,
)


# --- entity_id -----------------------------------------------------------------
def test_entity_id():
    assert entity_id("iFi") == "volumio:ifi", "device name lower-cased into the slug"
    assert entity_id("Zen Stream #1") == "volumio:zen_stream_1", "punctuation run collapses to one underscore"
    assert entity_id("") == "volumio:player", "empty name falls back to 'player'"
    assert entity_id(None) == "volumio:player", "no name at all falls back to 'player'"


# --- normalise_transport ------------------------------------------------------
def test_normalise_transport():
    assert normalise_transport("play") == "playing", "play -> playing"
    assert normalise_transport("PAUSE") == "paused", "case-insensitive"
    assert normalise_transport("stop") == "stopped", "stop -> stopped"
    assert normalise_transport(None) == "idle", "falsy status -> idle"
    assert normalise_transport("") == "idle", "empty status -> idle"
    # an unrecognised but TRUTHY status is NOT normalised to "idle" — the dict
    # .get's default is the (lower-cased) value itself, only a falsy status -> idle.
    assert normalise_transport("buffering") == "buffering", "unknown truthy status passes through unchanged"
    assert normalise_transport("BUFFERING") == "buffering", "unknown status is still lower-cased"


# --- _clean --------------------------------------------------------------------
def test_clean():
    assert _clean("  Coldplay  ") == "Coldplay", "surrounding whitespace stripped"
    assert _clean("") is None, "empty string -> None"
    assert _clean(None) is None, "None -> None"
    assert _clean("Unknown") is None, "'Unknown' is a Volumio placeholder, not real data"
    assert _clean("Unknown Artist") is None, "'Unknown Artist' placeholder filtered"
    assert _clean("unknown album") is None, "placeholder match is case-insensitive"
    assert _clean("Radiohead") == "Radiohead", "real value passes through"


# --- _quality --------------------------------------------------------------
def test_quality():
    # the exact composition documented in the module: codec + samplerate/bitdepth
    assert _quality({"trackType": "flac", "samplerate": "44.1 kHz", "bitdepth": "16 bit"}) == "FLAC 44.1kHz/16bit", \
        "flac + samplerate + bitdepth composes 'FLAC 44.1kHz/16bit'"
    # webradio/http/streaming trackTypes are containers, not codecs -> dropped
    assert _quality({"trackType": "webradio", "samplerate": "44.1 kHz", "bitdepth": ""}) == "44.1kHz", \
        "container trackType contributes no codec, but a real samplerate still shows"
    assert _quality({"trackType": "", "samplerate": "", "bitdepth": ""}) == "", "nothing known -> empty string"
    assert _quality({"trackType": "mp3", "samplerate": "", "bitdepth": ""}) == "MP3", \
        "codec alone (no rate/depth known) still shows"


# --- source_of ---------------------------------------------------------------
def test_source_of():
    assert source_of("http://192.0.2.102:8098/api/play/track/55/stream?ticket=x&fmt=.flac", "") == "library", \
        "an OPUS stream path -> library"
    assert source_of("/track/55.flac", "") == "", "a bare path of no known shape is nothing"
    assert source_of("", "spop") == "external", "Spotify Connect service -> external, even with no uri"
    assert source_of("", "spotify") == "external", "'spotify' service name also recognised"
    assert source_of("http://1.2.3.4/stream.mp3", "") == "external", "the device's own http stream -> external"
    # Radio is deliberately NOT classified by path: a station url has no recognisable
    # shape, so any '/radio/' rule both false-fires and misses. The adapter matches the
    # uri against the configured stations instead and sets the source itself.
    assert source_of("https://stream.yammat.fm/radio/8000/yammat.mp3", "") == "external", \
        "a station url that happens to contain '/radio/' is not classified here"
    assert source_of("https://stream.radioparadise.com/aac-320", "") == "external", \
        "...and neither is one that does not — no path rule can tell radio apart"


# --- mpd_uri / station_url ----------------------------------------------------
def test_mpd_uri_corrects_a_lying_suffix():
    # Yammat: mount named .mp3, serves AAC -> MPD probes `mad` and decodes garbage.
    # The hint's FINAL dot is what MPD reads as the suffix.
    assert mpd_uri("https://stream.yammat.fm/radio/8000/yammat.mp3", "aac") == \
        "https://stream.yammat.fm/radio/8000/yammat.mp3?dida_codec=.aac", \
        "suffix contradicts the measured codec -> hint appended"
    assert mpd_uri("http://host/s.mp3?x=1", "aac") == "http://host/s.mp3?x=1&dida_codec=.aac", \
        "an existing query string is extended, not replaced"


def test_mpd_uri_leaves_honest_urls_alone():
    assert mpd_uri("https://stream.radioparadise.com/aac-320", "aac") == \
        "https://stream.radioparadise.com/aac-320", "no suffix at all -> nothing to correct"
    assert mpd_uri("https://ice1.somafm.com/groovesalad-128-mp3", "mp3") == \
        "https://ice1.somafm.com/groovesalad-128-mp3", "'-mp3' is not a suffix (no dot)"
    assert mpd_uri("http://host/stream.mp3", "mp3") == "http://host/stream.mp3", \
        "suffix agrees with the codec -> already decodes right"
    assert mpd_uri("http://host/stream.MP3", "mp3") == "http://host/stream.MP3", \
        "suffix match is case-insensitive"
    assert mpd_uri("http://host/stream.m4a", "aac") == "http://host/stream.m4a", \
        "same codec FAMILY (.m4a is AAC) is not a lie — hinting '.aac' could break it"
    assert mpd_uri("http://host/stream.bin", "aac") == "http://host/stream.bin", \
        "a suffix MPD maps to no decoder needs no correcting"
    assert mpd_uri("http://host/stream.mp3", "") == "http://host/stream.mp3", \
        "codec unknown -> no hint invented"
    # A dot inside a query token (streamtheworld's JWTs) is not a file suffix, and the
    # host's dots are not either — neither may trigger a rewrite.
    assert mpd_uri("https://23623.live.streamtheworld.com/SLJEMEAAC_SC?tok=ey.J9.x", "aac") == \
        "https://23623.live.streamtheworld.com/SLJEMEAAC_SC?tok=ey.J9.x", \
        "dots in the query/host are not the file suffix"


def test_station_url_is_the_inverse_of_mpd_uri():
    for url in ("https://stream.yammat.fm/radio/8000/yammat.mp3", "http://host/s.mp3?x=1"):
        assert station_url(mpd_uri(url, "aac")) == url, "the hint strips back off cleanly"
    assert station_url("https://stream.radioparadise.com/aac-320") == \
        "https://stream.radioparadise.com/aac-320", "an unhinted url passes through untouched"
    assert source_of("", "") == "", "nothing recognisable -> empty string"


# --- read_state: happy path ---------------------------------------------------
def test_read_state():
    state = {
        "status": "play",
        "volume": 50,
        "mute": False,
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "albumart": "/albumart/x.jpg",
        "duration": 180,
        "trackType": "flac",
        "samplerate": "44.1 kHz",
        "bitdepth": "16 bit",
    }
    out = read_state(state, "198.51.100.50")
    assert out["media_transport"] == "playing", "status play -> playing"
    assert out["volume"] == 50, "volume copied through"
    assert out["mute"] is False, "mute copied through"
    assert out["media_title"] == "Song", "title copied through"
    assert out["media_artist"] == "Artist", "artist copied through"
    assert out["media_album"] == "Album", "album copied through"
    assert out["media_art"] == "http://198.51.100.50/albumart/x.jpg", "relative albumart resolved against the host"
    assert out["media_duration"] == 180, "duration copied through"
    assert out["media_quality"] == "FLAC 44.1kHz/16bit", "quality composed from trackType+samplerate+bitdepth"


# --- read_state: edge cases -----------------------------------------------
def test_read_state_edge_cases():
    # volume clamps to [0, 100]
    assert read_state({"status": "stop", "volume": 150}, "h")["volume"] == 100, "volume clamps at 100"
    assert read_state({"status": "stop", "volume": -10}, "h")["volume"] == 0, "volume clamps at 0"
    # disableVolumeControl suppresses the volume reading even when a value is present
    out = read_state({"status": "stop", "volume": 40, "disableVolumeControl": True}, "h")
    assert "volume" not in out, "disableVolumeControl hides the volume capability entirely"
    # a non-numeric volume is swallowed, not raised
    out = read_state({"status": "stop", "volume": "not-a-number"}, "h")
    assert "volume" not in out, "unparsable volume is silently omitted, never raises"
    # mute:0 is falsy but explicitly present -> still reported (an "is not None" check)
    out = read_state({"status": "stop", "mute": 0}, "h")
    assert out["mute"] is False, "mute 0 -> False, distinguished from mute absent"
    assert "mute" not in read_state({"status": "stop"}, "h"), "mute omitted entirely when absent"
    # an already-absolute art URL is passed through unmodified
    out = read_state({"status": "stop", "albumart": "http://cdn.example/art.jpg"}, "h")
    assert out["media_art"] == "http://cdn.example/art.jpg", "absolute art URL is not re-prefixed"
    # no duration -> 0 (live stream), never a missing key
    assert read_state({"status": "stop"}, "h")["media_duration"] == 0, "absent duration -> 0"
    # placeholder title/artist are filtered by _clean, never leak as literal 'Unknown'
    out = read_state({"status": "stop", "title": "Unknown", "artist": "Unknown Artist"}, "h")
    assert "media_title" not in out, "Volumio 'Unknown' title placeholder never surfaces"
    assert "media_artist" not in out, "Volumio 'Unknown Artist' placeholder never surfaces"


# --- radio wedge watchdog ------------------------------------------------------
# The silent-wedge incident (2026-07-19): a short upstream drop leaves MPD claiming
# state=play with elapsed frozen forever, Volumio's getState mirrors the lie, and
# the idempotent-play guard then swallows the very re-cast that would heal it.
# These tests drive _radio_watchdog and the guard directly with a stubbed MPD.

class _Cfg:
    """AdapterConfig stand-in: default-valued reads, no DB."""
    def int(self, key, default=0):
        return default

    def get(self, key, default=None):
        return default


def _wired(mpd_replies):
    """(adapter, player, calls) with _mpd_status fed from a list and every
    side-effecting surface recorded instead of executed."""
    a = VolumioAdapter()
    a._cfg = _Cfg()
    p = Player("volumio:ifi", "iFi", "192.0.2.41")
    p.radio = {"url": "http://radio.example/live", "station": "Test FM"}
    calls = {"recast": 0, "cast": 0, "set": [], "icy": 0}

    async def mpd_status(_p):
        reply = mpd_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def recast(_p):
        calls["recast"] += 1

    a._mpd_status = mpd_status
    a._recast_radio = recast
    return a, p, calls


async def test_watchdog_advancing_elapsed_is_alive():
    a, p, calls = _wired([{"state": "play", "elapsed": "10.000"},
                          {"state": "play", "elapsed": "12.000"}])
    await a._radio_watchdog(p)
    assert p.live_elapsed == 10.0, "first sample seeds the baseline"
    p.live_at -= _STALL_AFTER + 5  # even an ancient baseline is fine when elapsed moves
    await a._radio_watchdog(p)
    assert p.live_elapsed == 12.0 and not p.stalled, "a moving playhead is proof of decode"
    assert calls["recast"] == 0, "no re-cast while the stream demonstrably decodes"


async def test_watchdog_frozen_elapsed_recasts_once_per_window():
    a, p, calls = _wired([{"state": "play", "elapsed": "10.000"}] +
                         [{"state": "play", "elapsed": "10.000"}] * 3)
    await a._radio_watchdog(p)
    p.live_at = time.monotonic() - (_STALL_AFTER + 1)  # simulate the frozen window
    await a._radio_watchdog(p)
    assert calls["recast"] == 1 and p.stalled, "frozen elapsed past the window ⇒ wedge ⇒ re-cast"
    await a._radio_watchdog(p)
    assert calls["recast"] == 1, "retry is paced by recast_at, not fired every poll"
    p.recast_at = time.monotonic() - (_STALL_AFTER + 1)
    p.live_at = time.monotonic() - (_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    assert calls["recast"] == 2, "still frozen a full window later ⇒ the re-cast IS the retry loop"


async def test_watchdog_recovery_clears_stall():
    a, p, _calls = _wired([{"state": "play", "elapsed": "10.000"},
                          {"state": "play", "elapsed": "0.500"}])
    await a._radio_watchdog(p)
    p.stalled = True  # as left behind by a fired wedge
    await a._radio_watchdog(p)
    assert not p.stalled and p.live_elapsed == 0.5, \
        "a re-cast resets elapsed near 0 — any CHANGE (not only growth) proves decode"


async def test_watchdog_ignores_pause():
    a, p, calls = _wired([{"state": "pause", "elapsed": "10.000"}] * 2)
    p.live_elapsed, p.live_at = 10.0, time.monotonic() - (_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    await a._radio_watchdog(p)
    assert calls["recast"] == 0, "a pause may be deliberate — never auto-resume it"


async def test_watchdog_stopped_mpd_under_claimed_play_recasts():
    """The 2026-07-30 incident: the box lost DNS, every reopen died before the
    handshake, MPD parked in 'stop' — and Volumio's webradio service claimed
    play for HOURS. Stop has no elapsed, so the frozen-elapsed check never
    fires; the stop-state path must catch it instead."""
    a, p, calls = _wired([{"state": "stop"}] * 3 + [{"state": "play", "elapsed": "1.000"}])
    await a._radio_watchdog(p)
    assert calls["recast"] == 0 and p.stopped_at is not None, \
        "first stop sample only seeds the debounce — a cast transition is not a wedge"
    await a._radio_watchdog(p)
    assert calls["recast"] == 0, "stop inside the debounce window is still not a wedge"
    p.stopped_at = time.monotonic() - (_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    assert calls["recast"] == 1 and p.stalled, \
        "MPD stopped past the window while Volumio claims play ⇒ wedge ⇒ re-cast"
    await a._radio_watchdog(p)
    assert p.stopped_at is None, "a decoding sample clears the stop debounce"


async def test_watchdog_probe_failure_is_loud_but_inert():
    a, p, calls = _wired([OSError("refused"), OSError("refused"),
                          {"state": "play", "elapsed": "1.000"}])
    await a._radio_watchdog(p)
    assert p.probe_warned, "first probe failure warns (the watchdog going blind is loud)"
    await a._radio_watchdog(p)  # second failure must not warn again (no per-poll spam)
    assert calls["recast"] == 0, "an unreachable MPD is not evidence of a wedge"
    await a._radio_watchdog(p)
    assert not p.probe_warned, "a successful probe re-arms the single warning"


async def test_play_media_guard_requires_proven_decode():
    """The idempotent skip must key on PROVEN decode freshness, not getState's
    'play' — the wedged device reports play forever, and trusting it swallowed
    the user's own heal attempt in the live incident."""
    url = "http://radio.example/live"

    async def run(fresh):
        a, p, calls = _wired([])
        a._get = _returning({"status": "play", "uri": url})
        a._radio_cast = _returning((None, "AAC 320kbps", url))

        async def cast_one(_p, _t, radio=False):
            calls["cast"] += 1

        async def set_(_p, cap, value):
            calls["set"].append((cap, value))

        a._cast_one = cast_one
        a._set = set_
        a._start_icy = lambda _p, _r: calls.__setitem__("icy", calls["icy"] + 1)
        p.live_elapsed = 100.0
        p.live_at = time.monotonic() - (1.0 if fresh else 30.0)
        await a._play_media(p, {"uri": url, "title": "Test FM"})
        return calls

    live = await run(fresh=True)
    assert live["cast"] == 0 and live["icy"] == 1, \
        "verifiably-decoding same-station play is skipped (no audible gap), ICY re-armed"
    wedged = await run(fresh=False)
    assert wedged["cast"] == 1, "stale liveness ⇒ the re-cast goes through — that IS the heal"


def _returning(value):
    async def call(*_a, **_k):
        return value
    return call


async def test_sudo_batch_keeps_the_password_out_of_argv():
    """The SSH password used to be piped in as `echo <pw> | sudo -S …`, which put it
    in the appliance's OWN process list (any `ps` on the box could read it) and
    repeated it once per command. One sudo wraps the whole batch and takes the
    password on stdin instead."""
    import shlex
    import subprocess

    adapter = VolumioAdapter.__new__(VolumioAdapter)
    adapter._cfg = type("C", (), {"get": staticmethod(lambda k, d=None: "s3cret" if k == "ssh_password" else d)})()

    seen: dict = {}

    async def fake_ssh_run(cmd, *, stdin=None):
        seen["cmd"], seen["stdin"] = cmd, stdin
        return ""

    adapter._ssh_run = fake_ssh_run
    await VolumioAdapter._ssh_sudo(adapter, ["sed -i 's#^a.*#a \"1\"#' /etc/mpd.conf", "systemctl restart mpd"])

    assert "s3cret" not in seen["cmd"], "the password must never appear in the command line"
    assert seen["stdin"] == "s3cret\n", "…it goes on stdin"
    assert seen["cmd"].count("sudo") == 1, "one sudo for the whole batch, not one per command"

    # And the quoting must survive a real shell: both commands run, inner quotes intact.
    script = seen["cmd"].split("sh -c ", 1)[1]
    probe = " && ".join(["echo FIRST-'inner'", "echo SECOND"])
    out = subprocess.run(["sh", "-c", f"sh -c {shlex.quote(probe)}"], capture_output=True, text=True)
    assert out.returncode == 0 and "FIRST-inner" in out.stdout and "SECOND" in out.stdout
    assert script.startswith("'") and script.endswith("'"), "the batch is passed as one quoted argument"
