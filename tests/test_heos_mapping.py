"""Regression tests for the HEOS (pyheos) <-> canonical mapping.

Run inside the heos adapter image (dida_adapter_heos installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-heos:latest \
      -c "python -m pytest tests/test_heos_mapping.py"

read_player/read_position read plain attributes off a pyheos HeosPlayer (state,
now_playing_media) — so both are exercised with duck-typed SimpleNamespace
stand-ins (no live HEOS CLI connection needed). Covers the PlayState.value unwrap,
the falsy-vs-unknown "idle" transport fallback, the song-over-station title
precedence for radio, the ms->s duration/position conversion (including the
0-is-not-None edge case), and the entity_id slug.
"""
from types import SimpleNamespace

from dida_adapter_heos.mapping import _clean, entity_id, read_player, read_position


# --- entity_id -----------------------------------------------------------------
def test_entity_id():
    assert entity_id("iFi Zen") == "heos:ifi_zen", "spaces collapse to a single underscore"
    assert entity_id("Kitchen (2)") == "heos:kitchen_2", "punctuation run collapses to one underscore, trimmed"
    assert entity_id("") == "heos:player", "empty name falls back to 'player'"
    assert entity_id(None) == "heos:player", "no name at all falls back to 'player'"


# --- _clean ----------------------------------------------------------------
def test_clean():
    assert _clean("  Coldplay  ") == "Coldplay", "surrounding whitespace stripped"
    assert _clean("") is None, "empty string -> None"
    assert _clean(None) is None, "None -> None"
    assert _clean(0) is None, "falsy non-string input -> None"


# --- read_player: transport ---------------------------------------------------
def test_read_player_transport():
    # many pyheos fields are enum-like objects exposing `.value` — unwrap it.
    p = SimpleNamespace(state=SimpleNamespace(value="play"), now_playing_media=None)
    assert read_player(p)["media_transport"] == "playing", "state.value 'play' -> playing"

    # a plain string state (no .value attr) falls back to the state itself
    p = SimpleNamespace(state="pause", now_playing_media=None)
    assert read_player(p)["media_transport"] == "paused", "plain string state also works"

    p = SimpleNamespace(state=SimpleNamespace(value="stop"), now_playing_media=None)
    assert read_player(p)["media_transport"] == "stopped", "state.value 'stop' -> stopped"

    # falsy/missing state -> idle, without ever reaching the lookup table
    assert read_player(SimpleNamespace(state=None, now_playing_media=None))["media_transport"] == "idle", \
        "falsy state -> idle"
    assert read_player(SimpleNamespace())["media_transport"] == "idle", "missing state attr entirely -> idle"

    # an unrecognised but TRUTHY state -> idle, the dict-lookup default
    p = SimpleNamespace(state=SimpleNamespace(value="buffering"), now_playing_media=None)
    assert read_player(p)["media_transport"] == "idle", "unknown truthy state falls back to idle"


# --- read_player: now-playing metadata ---------------------------------------
def test_read_player_now_playing():
    npm = SimpleNamespace(
        song="Song A", station="Ignored FM", artist="Artist A", album="Album A",
        image_url="http://art/x.jpg", duration=125000, current_position=45000,
    )
    p = SimpleNamespace(state=SimpleNamespace(value="play"), now_playing_media=npm)
    out = read_player(p)
    assert out["media_title"] == "Song A", "song wins over station when both are present"
    assert out["media_artist"] == "Artist A", "artist copied through"
    assert out["media_album"] == "Album A", "album copied through"
    assert out["media_art"] == "http://art/x.jpg", "art URL copied through"
    assert out["media_duration"] == 125, "duration ms -> whole seconds"

    # radio: no song, only a station name -> station becomes the title
    npm = SimpleNamespace(
        song=None, station="Jazz FM", artist=None, album=None, image_url=None,
        duration=0, current_position=None,
    )
    out = read_player(SimpleNamespace(state=SimpleNamespace(value="play"), now_playing_media=npm))
    assert out["media_title"] == "Jazz FM", "no song -> falls back to station (radio)"
    assert "media_artist" not in out, "blank artist is omitted, never an empty string"
    assert out["media_duration"] == 0, "0 duration (live stream) stays 0"

    # no now_playing_media object at all -> only transport is reported
    out = read_player(SimpleNamespace(state=SimpleNamespace(value="play"), now_playing_media=None))
    assert list(out.keys()) == ["media_transport"], "no now-playing metadata -> no media_* keys"


# --- read_position -------------------------------------------------------------
def test_read_position():
    npm = SimpleNamespace(current_position=45000)
    assert read_position(SimpleNamespace(now_playing_media=npm)) == 45, "position ms -> whole seconds"

    npm0 = SimpleNamespace(current_position=0)
    assert read_position(SimpleNamespace(now_playing_media=npm0)) == 0, "position 0 stays 0, not None"

    npm_none = SimpleNamespace(current_position=None)
    assert read_position(SimpleNamespace(now_playing_media=npm_none)) is None, "unknown position -> None"

    assert read_position(SimpleNamespace(now_playing_media=None)) is None, "no now-playing media -> None"


# --- reachability: the verdict from the source, edge-triggered --------------------


class _ReachBus:
    def __init__(self) -> None:
        self.reach: list = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)

    async def publish_state(self, update) -> None:
        pass

    async def publish_entity(self, info) -> None:
        pass


def _rkeys(bus):
    return [(e.device_key, e.reachable) for e in bus.reach]


class _Tracked:
    def __init__(self, device_key) -> None:
        self.device_key = device_key


async def test_the_one_sockets_verdict_fans_out_to_every_player():
    from dida_adapter_heos.adapter import HeosAdapter

    a = HeosAdapter()
    a._bus = _ReachBus()
    a._tracked = {1: _Tracked("192.168.1.40"), 2: _Tracked("192.168.1.41"),
                  3: _Tracked(None)}  # a player with no ip contributes nothing
    await a._publish_reach(False, "connection lost")
    assert _rkeys(a._bus) == [("192.168.1.40", False), ("192.168.1.41", False)]
    await a._publish_reach(False, "still lost")
    assert len(a._bus.reach) == 2, "the reconnect cycle must not spam the bus"
    await a._publish_reach(True)
    assert _rkeys(a._bus)[2:] == [("192.168.1.40", True), ("192.168.1.41", True)]
