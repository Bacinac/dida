"""Regression tests for the radio tuner's station picker (radio_tuner._pick_index).

Fresh logic (station PIN by name/id, added so a scene/automation can play a specific
station — the patio always plays Yammat FM) that shipped untested. Pure function,
extracted from the bus handler for exactly this.

Run via the pytest gate (see tests/run.sh); or directly in the api image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_radio_tuner.py"
"""
from types import SimpleNamespace

from dida_api import radio_tuner
from dida_api.radio_tuner import _pick_index
from dida_core import Command

# List order = index order (the caller sorts by sort_order,id before calling).
stations = [{"id": 1, "name": "Yammat FM"}, {"id": 9, "name": "Drone Zone"}, {"id": 3, "name": "Jazz"}]


def test_station_pin():
    # explicit station PIN — by name (case-insensitive) or id — overrides current
    assert _pick_index(stations, "play", None, "Yammat FM") == 0, "pin by exact name"
    assert _pick_index(stations, "play", None, "yammat fm") == 0, "pin by name, case-insensitive"
    assert _pick_index(stations, "play", 9, "Drone Zone") == 1, "pin overrides the current station"
    assert _pick_index(stations, "play", None, "9") == 1, "pin by id (string)"
    assert _pick_index(stations, "play", None, 3) == 2, "pin by id (int)"
    assert _pick_index(stations, "play", 9, "No Such Station") == 1, "unknown pin → current"
    assert _pick_index(stations, "play", None, "No Such Station") == 0, "unknown pin, no current → first"


def test_cycle():
    # next / previous, wrapping, no-current fallback
    assert _pick_index(stations, "next", 1, None) == 1, "next from idx0 → idx1"
    assert _pick_index(stations, "next", 3, None) == 0, "next from last wraps → first"
    assert _pick_index(stations, "next", None, None) == 0, "next with no current → first"
    assert _pick_index(stations, "previous", 1, None) == 2, "previous from idx0 wraps → last"
    assert _pick_index(stations, "previous", None, None) == 2, "previous with no current → last"


def test_play_stays_on_current():
    assert _pick_index(stations, "play", 9, None) == 1, "play stays on the current station"
    assert _pick_index(stations, "play", None, None) == 0, "play with no current → first"
    assert _pick_index(stations, "play_index", 3, None) == 2, "play_index → current"


def test_other_commands_noop():
    assert _pick_index(stations, "stop", 1, None) is None, "stop → no-op (None)"
    assert _pick_index(stations, "pause", 1, None) is None, "pause → no-op (None)"


class _Bus:
    def __init__(self):
        self.published = []
        self.handler = None
        self.nc = SimpleNamespace(subscribe=self._subscribe)

    async def _subscribe(self, subject, cb):
        pass

    async def subscribe_commands(self, handler, namespace):
        self.handler = handler

    async def publish_command(self, cmd):
        self.published.append(cmd)


async def _tuner(monkeypatch, settings):
    async def app_setting(pool, key):
        return settings.get(key)

    async def set_app_setting(pool, key, value):
        settings[key] = value

    async def opus_stations(pool):
        return [{"id": 1, "name": "Yammat FM", "url": "https://stream.example/yammat.mp3"}]

    monkeypatch.setattr(radio_tuner, "app_setting", app_setting)
    monkeypatch.setattr(radio_tuner, "set_app_setting", set_app_setting)
    monkeypatch.setattr(radio_tuner.opus, "stations", opus_stations)
    bus = _Bus()
    await radio_tuner.start(SimpleNamespace(state=SimpleNamespace(bus=bus, pool=None)))
    await bus.handler(Command(entity_id="radio:tuner", capability="media_transport",
                              command="play", ts_ns=1, args={}, source="automation:74:AUDIO-Patio"))
    return bus.published


async def test_plays_on_the_configured_player(monkeypatch):
    sent = await _tuner(monkeypatch, {"radio_player": "opus:dac"})
    assert [(c.entity_id, c.command, c.args["uri"], c.source) for c in sent] == [
        ("opus:dac", "play_media", "https://stream.example/yammat.mp3", "automation:74:AUDIO-Patio")]


async def test_no_configured_player_plays_nowhere(monkeypatch):
    # The tuner used to fall back to a Home Assistant-era player, so a house without
    # `radio_player` sent its radio to a device it may not have and nobody heard why.
    assert await _tuner(monkeypatch, {}) == []
