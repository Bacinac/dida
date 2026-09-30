"""The Shield's poll — where python-androidtv is corrected, not trusted.

`mapping.py` is at 99%; `adapter.py` sits at 13.5% and holds the poll, which exists
in its current shape because the library it wraps is unreliable on Android 11+:
`state` can read "off" on a TV that is plainly on, and `current_app` comes back None
even with something in the foreground. Both are corrected with direct dumpsys
probes, and both corrections are invisible when wrong — the tile just shows the
device off, or stuck on the app it was running an hour ago.

The other rule worth pinning: in ambient/screensaver mode the Shield reports no
foreground app, so now-playing is CLEARED. Without that a photo frame keeps
advertising the film that finished last night, and an automation conditioned on
`source` acts on it.

Everything here runs against a stub `_atv`, which is the honest boundary: the value
is in what the adapter DECIDES from the library's answers, not in re-implementing
ADB.
"""

from __future__ import annotations

import asyncio

import pytest
from adb_shell.exceptions import TcpTimeoutException
from dida_adapter_androidtv.adapter import AndroidTVAdapter
from dida_core import Command, CommandRejected
from dida_core.health import StatusReporter


class _Atv:
    """Stands in for python-androidtv: a fixed update() tuple plus scripted shell
    replies, keyed by a fragment of the dumpsys query."""

    def __init__(self, update, shell=None) -> None:
        self._update = update
        self._shell = shell or {}
        self.queries: list[str] = []

    async def update(self, **kw):
        return self._update

    async def adb_shell(self, query: str):
        self.queries.append(query)
        for frag, reply in self._shell.items():
            if frag in query:
                return reply
        return ""


class _Bus:
    def __init__(self) -> None:
        self.states: list[tuple[str, object]] = []
        self.entities: list[object] = []

    async def publish_state(self, update) -> None:
        self.states.append((update.capability, update.value))

    async def publish_entity(self, info) -> None:
        self.entities.append(info)


def _rig(update, shell=None, apps=None):
    # `_apps` is a LIST of {"link","name"} rows (the configured app catalog), not a
    # package->label mapping. Passing a dict here typechecked fine and blew up inside
    # app_friendly — the shape of a fixture is part of what a test asserts.
    a = AndroidTVAdapter()
    a.status = StatusReporter("androidtv")
    a._bus = _Bus()
    a._entity = "androidtv:shield"
    a._name = "Shield"
    a._host = "192.168.1.30"
    a._apps = apps or []
    a._atv = _Atv(update, shell)
    return a


def _pub(a, cap):
    return [v for c, v in a._bus.states if c == cap]


# (state, current_app, running, audio_out, muted, volume, hdmi)
def _upd(state="playing", app="com.plex", muted=False, vol=None):
    return (state, app, None, None, muted, vol, None)


async def test_a_dumpsys_screen_probe_OVERRIDES_the_librarys_state():
    """python-androidtv reads 'off' on an awake Shield (Android 11+). Trusting it
    shows the TV as off while it is playing a film."""
    a = _rig(_upd(state="off"), shell={"dumpsys power": "mWakefulness=Awake"})
    await a._poll_once()
    assert _pub(a, "on_off") == [True]


async def test_the_probe_can_also_correct_the_other_way():
    a = _rig(_upd(state="playing"), shell={"dumpsys power": "mWakefulness=Asleep"})
    await a._poll_once()
    assert _pub(a, "on_off") == [False]


async def test_an_inconclusive_probe_leaves_the_librarys_answer_alone():
    """A dumpsys format this does not recognise must not be read as 'off' — that
    would turn an unparsed string into a device the house believes is powered down."""
    a = _rig(_upd(state="playing"), shell={"dumpsys power": "something unfamiliar"})
    await a._poll_once()
    assert _pub(a, "on_off") == [True]


async def test_a_missing_current_app_falls_back_to_a_dumpsys_probe():
    """current_app=None with the TV on is the Android 11+ format change, not an idle
    device — without the fallback the source tile goes blank mid-film."""
    a = _rig(_upd(app=None),
             shell={"dumpsys power": "mWakefulness=Awake",
                    "ResumedActivity": "ResumedActivity: ... com.netflix.ninja/.MainActivity"})
    await a._poll_once()
    assert _pub(a, "text") == ["com.netflix.ninja"]


async def test_the_second_probe_is_tried_when_the_first_says_nothing():
    a = _rig(_upd(app=None),
             shell={"dumpsys power": "mWakefulness=Awake",
                    "mCurrentFocus": "mCurrentFocus=Window{... com.plexapp.android/.Main}"})
    await a._poll_once()
    assert _pub(a, "text") == ["com.plexapp.android"]


async def test_a_friendly_name_is_used_when_the_package_is_known():
    a = _rig(_upd(app="com.plexapp.android"),
             shell={"dumpsys power": "mWakefulness=Awake"},
             apps=[{"link": "com.plexapp.android", "name": "Plex"}])
    await a._poll_once()
    assert _pub(a, "source") == ["Plex"]
    assert _pub(a, "text") == ["com.plexapp.android"], \
        "the raw package must still be published — it is what commands address"


async def test_screensaver_CLEARS_now_playing():
    """Ambient mode reports no foreground app. Leaving the old values would have the
    photo frame still advertising last night's film."""
    a = _rig(_upd(app=None), shell={"dumpsys power": "mWakefulness=Awake"})
    await a._poll_once()
    for cap in ("source", "media_title", "media_art"):
        assert _pub(a, cap) == [""], f"{cap} kept a stale value through the screensaver"


async def test_a_device_that_is_OFF_clears_now_playing_too():
    a = _rig(_upd(state="off", app="com.plex"),
             shell={"dumpsys power": "mWakefulness=Asleep"})
    await a._poll_once()
    assert _pub(a, "source") == [""]


async def test_volume_is_scaled_to_percent_and_clamped():
    a = _rig(_upd(vol=0.5), shell={"dumpsys power": "mWakefulness=Awake"})
    await a._poll_once()
    assert _pub(a, "volume") == [50]

    b = _rig(_upd(vol=1.4), shell={"dumpsys power": "mWakefulness=Awake"})
    await b._poll_once()
    assert _pub(b, "volume") == [100]


async def test_volume_capability_is_ANNOUNCED_the_first_time_it_appears():
    """Not every Shield/TV combination exposes volume. It joins the catalog on first
    sighting; without the re-announce the UI has a value it will not render."""
    a = _rig(_upd(vol=0.4), shell={"dumpsys power": "mWakefulness=Awake"})
    assert a._has_volume is False
    await a._poll_once()
    assert a._has_volume is True
    assert any("volume" in (getattr(e, "capabilities", None) or [])
               for e in a._bus.entities), "volume never entered the announced catalog"


async def test_a_device_without_volume_does_not_announce_it():
    a = _rig(_upd(vol=None), shell={"dumpsys power": "mWakefulness=Awake"})
    await a._poll_once()
    assert a._has_volume is False
    assert _pub(a, "volume") == []


async def test_an_unchanged_value_is_not_republished():
    a = _rig(_upd(vol=0.5), shell={"dumpsys power": "mWakefulness=Awake"})
    await a._poll_once()
    a._bus.states.clear()
    await a._poll_once()
    assert a._bus.states == []


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


async def test_reachability_is_keyed_by_the_tv_host():
    from dida_adapter_androidtv.adapter import AndroidTVAdapter

    a = AndroidTVAdapter()
    a._bus = _ReachBus()
    a._host = "192.168.1.30"
    await a._publish_reach(True)
    await a._publish_reach(False, "connection lost")
    assert _rkeys(a._bus) == [("192.168.1.30", True), ("192.168.1.30", False)]


# --- a dead ADB link: adb_shell has closed the socket under python-androidtv ------------


class _DeadAtv(_Atv):
    async def adb_shell(self, query: str):
        raise TcpTimeoutException("Reading from 192.168.1.30:5555 timed out (9.0 seconds)")


def _linked(atv):
    a = AndroidTVAdapter()
    a.status = StatusReporter("androidtv")
    a._bus = _ReachBus()
    a._entity = "androidtv:shield"
    a._host = "192.168.1.30"
    a._atv = atv
    a._connected = True
    a._reach = True
    return a


async def test_a_command_that_times_out_ends_the_link_instead_of_reusing_the_dead_socket():
    a = _linked(_DeadAtv(_upd()))
    tune = Command(entity_id="androidtv:shield", capability="channel", command="set_channel", ts_ns=0, args={"value": "1"})
    with pytest.raises(CommandRejected, match="no answer from the device — reconnecting"):
        await a.handle_command(tune)
    assert a._connected is False
    assert _rkeys(a._bus) == [("192.168.1.30", False)]
    with pytest.raises(CommandRejected, match="not connected"):
        await a.handle_command(tune)


async def test_a_poll_that_times_out_ends_the_link_at_once():
    a = _linked(_Atv(_upd()))
    a._cfg = type("Cfg", (), {"int": lambda self, key, default: default})()

    async def dead(**kw):
        raise TcpTimeoutException("timed out")

    a._atv.update = dead
    await asyncio.wait_for(a._poll_until_disconnect(), timeout=1)
    assert a._connected is False
    assert _rkeys(a._bus) == [("192.168.1.30", False)]
