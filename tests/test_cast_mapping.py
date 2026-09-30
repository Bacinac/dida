"""Chromecast (pychromecast MediaStatus) <-> canonical-capability mapping tests.

Run inside the cast adapter image (dida_adapter_cast installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-cast:latest \
      -c "python -m pytest tests/test_cast_mapping.py"

Covers entity_id slugging, normalise_transport (including the default-to-idle
fallback for any state the vocabulary doesn't know), the _clean truthiness
filter, and read_media's now-playing snapshot off a duck-typed MediaStatus
stand-in (title/artist/album/art/duration, and what happens when they're absent).
"""
from types import SimpleNamespace

from dida_adapter_cast.mapping import _clean, entity_id, normalise_transport, read_media


# --- entity_id -----------------------------------------------------------
def test_entity_id():
    assert entity_id("Living Room Speaker") == "cast:living_room_speaker", "spaces -> underscores, lower-cased"
    assert entity_id("") == "cast:cast", "empty name falls back to the 'cast' default slug"
    assert entity_id(None) == "cast:cast", "None name falls back the same way as empty"


# --- normalise_transport ---------------------------------------------------
def test_normalise_transport():
    assert normalise_transport("PLAYING") == "playing", "already-uppercase state maps directly"
    assert normalise_transport("playing") == "playing", "lower-case input is upper-cased before lookup"
    assert normalise_transport("BUFFERING") == "buffering", "buffering maps through"
    assert normalise_transport("UNKNOWN") == "idle", "explicit UNKNOWN state maps to idle"
    assert normalise_transport(None) == "idle", "no state at all -> idle"
    assert normalise_transport("SOME_FUTURE_STATE") == "idle", \
        "an unrecognised state defaults to idle rather than raising or passing through"


# --- _clean: display-string truthiness filter -------------------------------
def test_clean():
    assert _clean(None) is None, "None -> None"
    assert _clean("") is None, "empty string -> None"
    assert _clean("   ") is None, "whitespace-only strips to empty, which is falsy -> None"
    assert _clean("  My Song  ") == "My Song", "surrounding whitespace is stripped"
    assert _clean(0) is None, "a falsy non-string value is treated as absent"


# --- read_media: MediaStatus -> {capability: value} -------------------------
def test_read_media_full():
    status = SimpleNamespace(
        player_state="PLAYING",
        title="  My Song  ",
        artist="Artist",
        album_name="Album",
        images=[SimpleNamespace(url="http://example.com/art.jpg")],
        duration=125.7,
    )
    out = read_media(status)
    assert out["media_transport"] == "playing", "player_state normalised"
    assert out["media_title"] == "My Song", "title is cleaned (stripped)"
    assert out["media_artist"] == "Artist"
    assert out["media_album"] == "Album"
    assert out["media_art"] == "http://example.com/art.jpg", "first image's url is used as art"
    assert out["media_duration"] == 125, "duration is truncated to whole seconds, not rounded"


def test_read_media_missing_fields():
    status = SimpleNamespace(
        player_state=None,
        title=None,
        artist="",
        album_name=None,
        images=[],
        duration=None,
    )
    out = read_media(status)
    assert out == {"media_transport": "idle", "media_duration": 0}, \
        "absent/empty fields never publish a phantom capability; duration falls back to 0"


def test_read_media_zero_duration_and_missing_art_url():
    # duration=0 must still emit media_duration=0 (a live stream), not be dropped.
    status = SimpleNamespace(
        player_state="PLAYING",
        title=None,
        artist=None,
        album_name=None,
        images=[SimpleNamespace(url=None)],
        duration=0,
    )
    out = read_media(status)
    assert out["media_duration"] == 0, "duration 0 is emitted explicitly, not omitted"
    assert "media_art" not in out, "an image entry with no url doesn't publish media_art"


# --- discovery callbacks: signatures must match the INSTALLED pychromecast ----
def test_discovery_callbacks_match_pychromecast():
    """The add/remove callbacks we hand to pychromecast are called from the
    zeroconf thread, so a signature mismatch surfaces as a TypeError INSIDE that
    thread — which kills the ServiceBrowser and silently ends discovery for the
    life of the process. That happened in prod on 2026-07-23: `remove_cast` passes
    (uuid, service, cast_info) and our callback took two, so after the first
    device dropped off mDNS nothing was ever discovered again. Drive the real
    SimpleCastListener rather than trusting the shape by eye."""
    import pychromecast
    from dida_adapter_cast.adapter import CastAdapter

    a = CastAdapter()
    listener = pychromecast.SimpleCastListener(a._on_add, a._on_remove)
    # _on_add with no browser registered returns early — the point is that the
    # library's call ARITY fits ours, in the thread where a mismatch is fatal.
    listener.add_cast("uuid-1", "service-1")
    listener.remove_cast("uuid-1", "service-1", SimpleNamespace(friendly_name="Gone"))


# --- device identity + reachability -----------------------------------------------


class _ReachBus:
    def __init__(self) -> None:
        self.reach: list = []
        self.entities: list = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)

    async def publish_entity(self, info) -> None:
        self.entities.append(info)

    async def publish_state(self, update) -> None:
        pass


class _Sock:
    def __init__(self, connected) -> None:
        self.is_connected = connected


class _CC:
    def __init__(self, connected) -> None:
        self.socket_client = _Sock(connected)


def test_the_device_key_is_the_entity_slug():
    from dida_adapter_cast.adapter import CastDevice

    dev = CastDevice("uuid-1", object(), "cast:living_room_tv", "Living room TV")
    assert dev.dev_key == "living_room_tv"


async def test_reconcile_publishes_each_devices_verdict_once():
    """The maintenance loop runs every ~20 s — only a real socket transition may
    reach the bus, per device, keyed by the slug its entity carries."""
    from dida_adapter_cast.adapter import CastAdapter, CastDevice
    from dida_core.health import StatusReporter

    a = CastAdapter()
    a.status = StatusReporter("cast")
    a._bus = _ReachBus()
    up = CastDevice("u1", _CC(True), "cast:nest_hub", "Nest Hub")
    down = CastDevice("u2", _CC(False), "cast:shield", "Shield")
    a._devices = {"u1": up, "u2": down}
    a._connected = set()
    await a._reconcile_connected()
    got = {(e.device_key, e.reachable) for e in a._bus.reach}
    assert got == {("nest_hub", True), ("shield", False)}
    await a._reconcile_connected()
    assert len(a._bus.reach) == 2, "an unchanged verdict is not re-published"
