"""The Volumio radio watchdog — the only layer that can tell playing from wedged.

`mapping.py` is at 100%; everything uncovered here is lifecycle, and the piece worth
pinning down is the watchdog, because it exists for a failure every OTHER layer
reported as healthy. Measured live: elapsed frozen at 67:15 for twenty minutes while
Volumio's getState kept saying "play" with an interpolated playhead, every screen in
the house agreed, and the idempotent-play guard swallowed the user's own play press
on top. MPD's `status` elapsed is the one truthful decode signal.

There are two wedges, through different doors, and the second was found only after
the first was fixed:

  * MPD says PLAY and elapsed does not move → the upstream connection is dead;
  * MPD says STOP while Volumio still claims play → a re-cast that never completed
    its TCP handshake (2026-07-30: the box lost DNS). The frozen-elapsed check cannot
    see this one, because `stop` has no elapsed at all.

And two things it must NOT do: resume a deliberate pause, or freeze the poll loop on
a hung MPD that accepts TCP and never answers (2026-07-18).

Time is injected rather than slept: a watchdog whose test takes twenty seconds per
case is one that gets deleted.
"""

from __future__ import annotations

import pytest
from dida_adapter_volumio.adapter import _STALL_AFTER, Player, VolumioAdapter
from dida_core.health import StatusReporter


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def rig(monkeypatch):
    """Adapter + player + a controllable clock, with MPD and the re-cast stubbed."""
    a = VolumioAdapter()
    a.status = StatusReporter("volumio")
    p = Player("volumio:zen", "Zen", "192.168.1.50")
    p.radio = {"url": "http://stream.example/1", "station": "Radio 1"}

    clock = _Clock()
    monkeypatch.setattr("dida_adapter_volumio.adapter.time.monotonic", clock)

    recasts: list[str] = []

    async def _recast(player):
        recasts.append(player.entity_id)
    monkeypatch.setattr(a, "_recast_radio", _recast)

    status: dict = {}

    async def _mpd_status(player):
        if status.get("__raise__"):
            raise RuntimeError("MPD hung")
        return {k: v for k, v in status.items() if not k.startswith("__")}
    monkeypatch.setattr(a, "_mpd_status", _mpd_status)

    p.live_at = clock.t
    return a, p, clock, status, recasts


async def test_a_moving_playhead_is_never_touched(rig):
    a, p, clock, status, recasts = rig
    for elapsed in (10, 12, 14, 16):
        status.update({"state": "play", "elapsed": str(elapsed)})
        clock.advance(_STALL_AFTER)
        await a._radio_watchdog(p)
    assert recasts == [], "a decoding stream was re-cast"
    assert p.stalled is False


async def test_a_frozen_playhead_while_claiming_play_is_a_wedge(rig):
    """The original incident, in three lines."""
    a, p, clock, status, recasts = rig
    status.update({"state": "play", "elapsed": "4035"})   # 67:15
    await a._radio_watchdog(p)                            # first sample
    clock.advance(_STALL_AFTER + 1)
    await a._radio_watchdog(p)                            # same elapsed, later
    assert recasts == ["volumio:zen"]
    assert p.stalled is True


async def test_a_freeze_SHORTER_than_the_threshold_is_not_a_wedge(rig):
    """A slow buffer-fill at cast start must not be mistaken for a dead stream."""
    a, p, clock, status, recasts = rig
    status.update({"state": "play", "elapsed": "5"})
    await a._radio_watchdog(p)
    clock.advance(_STALL_AFTER - 1)
    await a._radio_watchdog(p)
    assert recasts == []


async def test_the_recast_paces_itself_instead_of_firing_every_poll(rig):
    """The poll runs every ~2 s. Without the cooldown a dead stream would re-cast
    thirty times a minute while the internet is down."""
    a, p, clock, status, recasts = rig
    status.update({"state": "play", "elapsed": "4035"})
    await a._radio_watchdog(p)
    clock.advance(_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    clock.advance(2)
    await a._radio_watchdog(p)
    assert recasts == ["volumio:zen"], "the watchdog re-cast again inside its own cooldown"


async def test_the_stream_decoding_again_clears_the_wedge(rig):
    a, p, clock, status, _recasts = rig
    status.update({"state": "play", "elapsed": "4035"})
    await a._radio_watchdog(p)
    clock.advance(_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    assert p.stalled is True

    status["elapsed"] = "4036"
    clock.advance(2)
    await a._radio_watchdog(p)
    assert p.stalled is False


async def test_MPD_STOPPED_while_volumio_claims_play_is_the_SECOND_wedge(rig):
    """A re-cast that never completed its handshake parks MPD in `stop`, and stop has
    no elapsed — so the frozen-playhead check above is blind to it. This one held for
    hours in production while every screen said play."""
    a, p, clock, status, recasts = rig
    status.update({"state": "stop"})
    await a._radio_watchdog(p)            # arms the stop timer
    clock.advance(_STALL_AFTER + 1)
    await a._radio_watchdog(p)
    assert recasts == ["volumio:zen"]


async def test_a_BRIEF_stop_is_not_a_wedge(rig):
    a, p, clock, status, recasts = rig
    status.update({"state": "stop"})
    await a._radio_watchdog(p)
    clock.advance(_STALL_AFTER - 1)
    await a._radio_watchdog(p)
    assert recasts == []


async def test_playing_again_disarms_the_stop_timer(rig):
    """Otherwise a stop from an hour ago would trigger a re-cast on a stream that has
    been playing happily since."""
    a, p, clock, status, recasts = rig
    status.update({"state": "stop"})
    await a._radio_watchdog(p)
    clock.advance(5)
    status.update({"state": "play", "elapsed": "1"})
    await a._radio_watchdog(p)
    assert p.stopped_at is None

    clock.advance(5)
    status.update({"state": "stop"})
    await a._radio_watchdog(p)
    clock.advance(_STALL_AFTER - 1)
    await a._radio_watchdog(p)
    assert recasts == []


async def test_a_PAUSE_is_never_auto_resumed(rig):
    """A pause may be deliberate. Re-casting over it would restart the user's music
    every twenty seconds, which is worse than the wedge."""
    a, p, clock, status, recasts = rig
    status.update({"state": "pause", "elapsed": "30"})
    for _ in range(5):
        clock.advance(_STALL_AFTER + 1)
        await a._radio_watchdog(p)
    assert recasts == []


async def test_a_HUNG_MPD_blinds_the_watchdog_instead_of_freezing_the_poll(rig, caplog):
    """MPD that accepts TCP and never answers (2026-07-18). The probe must give up and
    return — an unbounded readline here would stop every other capability updating."""
    a, p, _clock, status, recasts = rig
    status["__raise__"] = True
    await a._radio_watchdog(p)
    assert recasts == []
    assert p.probe_warned is True, "a blind watchdog must say so"


async def test_the_probe_warning_is_logged_ONCE_not_every_poll(rig, caplog):
    import logging

    a, p, _clock, status, _recasts = rig
    status["__raise__"] = True
    with caplog.at_level(logging.WARNING, logger="dida.adapter.volumio"):
        for _ in range(5):
            await a._radio_watchdog(p)
    assert caplog.text.count("liveness probe failed") <= 1


async def test_a_recovered_probe_re_arms_the_warning(rig):
    a, p, _clock, status, _recasts = rig
    status["__raise__"] = True
    await a._radio_watchdog(p)
    assert p.probe_warned is True

    status.clear()
    status.update({"state": "play", "elapsed": "1"})
    await a._radio_watchdog(p)
    assert p.probe_warned is False


async def test_an_unparseable_elapsed_is_ignored_rather_than_guessed(rig):
    a, p, clock, status, recasts = rig
    status.update({"state": "play", "elapsed": "not-a-number"})
    clock.advance(_STALL_AFTER * 3)
    await a._radio_watchdog(p)
    assert recasts == []


def test_mpd_arguments_are_quoted():
    """An MPD command is whitespace-separated: a URL with a space in it would split
    into two arguments and the command would fail, or worse, half-succeed."""
    assert VolumioAdapter._mpd_arg("http://a/b c") == '"http://a/b c"'
    assert VolumioAdapter._mpd_arg('say "hi"') == '"say \\"hi\\""'
    assert VolumioAdapter._mpd_arg("back\\slash") == '"back\\\\slash"'


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


async def test_reachability_is_keyed_by_the_player_host():
    from dida_adapter_volumio.adapter import Player, VolumioAdapter

    a = VolumioAdapter()
    a._bus = _ReachBus()
    a._player = Player("volumio:zen", "iFi Zen", "192.168.1.31")
    await a._publish_reach(False, "getState timed out")
    await a._publish_reach(False, "again")
    assert _rkeys(a._bus) == [("192.168.1.31", False)]
    assert a._bus.reach[0].detail == "getState timed out"
