"""DLNA (async_upnp_client DmrDevice) <-> canonical-capability mapping tests.

Run inside the dlna adapter image (dida_adapter_dlna installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-dlna:latest \
      -c "python -m pytest tests/test_dlna_mapping.py"

Covers entity_id slugging, normalise_transport (including the UPnP
TransportState vocabulary AND the pass-through-lowercased fallback for an
unrecognised state — deliberately different from a default-to-idle), the
_clean 'Unknown'/'Unknown Artist'/'Unknown Album' junk filter DLNA servers
love to stuff into empty metadata, and read_renderer's full snapshot off a
duck-typed DmrDevice stand-in (volume 0..1 -> 0..100 rescale + clamp, the
always-emit-duration-0 rule, and the image-url-then-album-art-uri fallback).

Also covers the adapter's empty-state discovery badge (adapter._discovery_badge),
which is not mapping but is the one thing standing between "bound to the wrong
NIC" and three more days of a green badge over an adapter that sees nothing.
"""
from types import SimpleNamespace

from dida_adapter_dlna.adapter import _SILENT_PASSES, DlnaAdapter
from dida_adapter_dlna.mapping import _clean, entity_id, normalise_transport, read_renderer
from dida_core.health import StatusReporter


# --- entity_id -----------------------------------------------------------
def test_entity_id():
    assert entity_id("Living Room Renderer") == "dlna:living_room_renderer", "spaces -> underscores, lower-cased"
    assert entity_id("") == "dlna:renderer", "empty name falls back to the 'renderer' default slug"
    assert entity_id(None) == "dlna:renderer", "None name falls back the same way as empty"


# --- normalise_transport -----------------------------------------------------
def test_normalise_transport():
    assert normalise_transport(None) == "idle", "no state at all -> idle"
    assert normalise_transport("PLAYING") == "playing"
    assert normalise_transport("PAUSED_PLAYBACK") == "paused"
    assert normalise_transport("PAUSED_RECORDING") == "paused", "both PAUSED_* variants collapse to paused"
    assert normalise_transport("STOPPED") == "stopped"
    assert normalise_transport("NO_MEDIA_PRESENT") == "idle"
    assert normalise_transport("TRANSITIONING") == "buffering"
    assert normalise_transport("RECORDING") == "playing", "an active recording counts as playing"

    # unlike the cast adapter, an unrecognised state is NOT forced to idle — it
    # passes through lower-cased so the UI at least shows something sane.
    assert normalise_transport("SOME_FUTURE_STATE") == "some_future_state", \
        "unrecognised state passes through lower-cased instead of defaulting to idle"

    # enum-like object exposing .value (as async_upnp_client's TransportState does)
    assert normalise_transport(SimpleNamespace(value="PLAYING")) == "playing", \
        "an enum wrapper is unwrapped via .value before lookup"


# --- _clean: the 'Unknown*' junk filter --------------------------------------
def test_clean():
    assert _clean(None) is None
    assert _clean("") is None
    assert _clean("   ") is None, "whitespace-only strips to empty -> None"
    assert _clean("Unknown") is None, "bare 'Unknown' is treated as absent metadata"
    assert _clean("unknown artist") is None, "case-insensitive match on the junk-value set"
    assert _clean("Unknown Album") is None
    assert _clean("  Real Title  ") == "Real Title", "a real value is stripped and kept"


# --- read_renderer: DmrDevice -> {capability: value} -------------------------
def test_read_renderer_full():
    dmr = SimpleNamespace(
        transport_state="PLAYING",
        volume_level=0.75,
        is_volume_muted=False,
        media_title="Song",
        media_artist="Artist",
        media_album_name="Album",
        media_image_url="http://example.com/art.jpg",
        media_album_art_uri=None,
        media_duration=245,
    )
    out = read_renderer(dmr)
    assert out == {
        "media_transport": "playing",
        "volume": 75,
        "mute": False,
        "media_title": "Song",
        "media_artist": "Artist",
        "media_album": "Album",
        "media_art": "http://example.com/art.jpg",
        "media_duration": 245,
    }, "volume rescaled 0..1 -> 0..100, all reported fields published"


def test_read_renderer_minimal():
    out = read_renderer(SimpleNamespace())
    assert out == {"media_transport": "idle", "media_duration": 0}, \
        "an empty renderer only ever emits transport + duration (no phantom capabilities)"


def test_read_renderer_volume_clamp():
    over = read_renderer(SimpleNamespace(volume_level=1.5, media_duration=None))
    assert over["volume"] == 100, "volume above 1.0 clamps to 100"
    under = read_renderer(SimpleNamespace(volume_level=-0.3, media_duration=None))
    assert under["volume"] == 0, "negative volume clamps to 0"


def test_read_renderer_unknown_metadata_filtered():
    dmr = SimpleNamespace(media_title="Unknown", media_artist="Unknown Artist", media_album_name="Unknown Album")
    out = read_renderer(dmr)
    assert "media_title" not in out and "media_artist" not in out and "media_album" not in out, \
        "'Unknown*' junk metadata never publishes a capability"


def test_read_renderer_art_fallback_and_duration_zero():
    dmr = SimpleNamespace(media_image_url=None, media_album_art_uri="http://fallback.jpg", media_duration=0)
    out = read_renderer(dmr)
    assert out["media_art"] == "http://fallback.jpg", "media_album_art_uri is used when media_image_url is absent"
    assert out["media_duration"] == 0, "duration 0 (live stream) is still explicitly emitted, not omitted"


# --- discovery badge (empty state) -------------------------------------------
# The adapter spent three days after the host move bound to the IoT VLAN, hearing
# nothing and reporting healthy, because health only means "the process is alive".
# These pin the two halves of the distinction that fixes that: SILENCE is a fault,
# but having nothing to DRIVE is not.


def _adapter(iface="203.0.113.11", renderers=None):
    a = DlnaAdapter()
    a.status = StatusReporter("dlna")  # the runner injects this in production
    a._iface_ip = iface
    a._renderers = renderers if renderers is not None else {}
    return a


def test_discovery_badge_errors_when_nothing_ever_answers():
    a = _adapter()
    for _ in range(_SILENT_PASSES):
        a._discovery_badge(replies=0, silent=_SILENT_PASSES)
    snap = a.status.snapshot()
    assert snap["state"] == "error", "an M-SEARCH nothing EVER answers means the wrong wire — fail loud"
    assert "203.0.113.11" in snap["detail"], "the detail names the interface actually searched"


def test_discovery_badge_debounces_a_single_silent_pass():
    a = _adapter()
    a._discovery_badge(replies=0, silent=_SILENT_PASSES - 1)
    assert a.status.snapshot()["state"] == "idle", \
        "SSDP is UDP — one lost round must not raise an alarm"


def test_discovery_badge_stays_calm_with_nothing_to_drive():
    # The everyday state: TVs off, iFi + Marantz answer but are excluded on purpose.
    # A red badge here every quiet evening is a badge nobody reads.
    a = _adapter()
    a._discovery_badge(replies=2, silent=0)
    snap = a.status.snapshot()
    assert snap["state"] == "idle", "devices answered — zero renderers to drive is not a fault"
    assert "2" in snap["detail"], "the detail still reports what was heard"


def test_discovery_badge_never_alarms_once_something_has_answered():
    # A healthy LAN DOES go quiet for a round: replies are UDP and devices rate-limit
    # M-SEARCH (measured live — the same leg answered 3, then 2, then 0 in minutes).
    # One reply ever must disarm the alarm for good, or this cries wolf.
    a = _adapter()
    a._discovery_badge(replies=2, silent=0)
    for _ in range(_SILENT_PASSES * 3):
        a._discovery_badge(replies=0, silent=_SILENT_PASSES)
    assert a.status.snapshot()["state"] == "idle", \
        "a leg that has answered before is not the fault this alarm is for"


def test_discovery_badge_cannot_contradict_itself():
    # Guard the contract: told that something replied, it must never simultaneously
    # claim nothing answers, whatever the silent counter says.
    a = _adapter()
    a._discovery_badge(replies=1, silent=_SILENT_PASSES * 5)
    assert a.status.snapshot()["state"] == "idle", "1 reply is not 'nothing answers'"


def test_discovery_badge_yields_once_there_is_something_to_drive():
    a = _adapter(renderers={"udn-1": object()})
    a.status.ok("1 renderer(s)")
    a._discovery_badge(replies=0, silent=_SILENT_PASSES)
    assert a.status.snapshot()["state"] == "ok", \
        "with a renderer to drive, _update_badge owns the badge — discovery must not stomp it"


# --- _ssdp_search: only replies that claim to BE a renderer count --------------
def test_ssdp_search_ignores_a_reply_that_matched_nothing_we_asked_for(monkeypatch):
    # The IoT VLAN's power meter answers ANY M-SEARCH with ST: upnp:rootdevice. It is
    # not a renderer; counting it kept the wrong-interface badge green for three days.
    from dida_adapter_dlna import adapter as mod

    replies = [
        ("203.0.113.17", b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n"
                          b"LOCATION: http://203.0.113.17/info.xml\r\n\r\n"),
        ("198.51.100.34", b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:service:AVTransport:1\r\n"
                         b"LOCATION: http://198.51.100.34:9197/dmr\r\n\r\n"),
        ("198.51.100.40", b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
                         b"LOCATION: http://198.51.100.40:60006/desc.xml\r\n\r\n"),
    ]
    monkeypatch.setattr(mod, "ssdp_msearch", lambda *a, **k: replies)
    found = mod._ssdp_search("198.51.100.100")
    assert found == {"http://198.51.100.34:9197/dmr", "http://198.51.100.40:60006/desc.xml"}, \
        "both renderer STs are kept (service-type catches the embedded ones), rootdevice is not"
    assert "http://203.0.113.17/info.xml" not in found, \
        "a device answering something we never asked for is not a renderer to probe"
