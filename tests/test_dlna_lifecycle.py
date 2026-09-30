"""The DLNA adapter's status badge — the thing that once said "fine" for three days.

`mapping.py` is at 100%; every uncovered line in this adapter is lifecycle, and the
lifecycle decision that matters most is what the badge says. It is not decoration:
it is the ONLY signal that distinguishes "the TVs are off" (ordinary, every evening)
from "the M-SEARCH is leaving by the wrong network leg" (a three-day production
blindness after the host move, during which the adapter reported healthy throughout,
because health meant "the process is alive" and never "it can see anything").

The rule the badge encodes is subtle enough to be worth pinning down:

  * driving ZERO renderers is normal and must never raise an alarm — a red badge
    every quiet evening is one nobody reads;
  * having heard NOTHING, EVER is the fault, because the excluded renderers answer
    on the right LAN regardless;
  * one reply, ever, disarms it for the life of the process — replies are UDP and
    devices rate-limit M-SEARCH, so a single empty round proves nothing (measured on
    the live LAN: 3 replies, then 2, then 0, within minutes);
  * `replies` counts devices that answered AS RENDERERS. The IoT VLAN has a power
    meter that answers every M-SEARCH with `upnp:rootdevice` — counting bare replies
    would have kept the badge green through the exact blindness it exists to catch.

Pure state machine over `_renderers` / `_heard` / `_poll_fails`, so it needs no
network: exactly the part that can be tested, and the part that was wrong.
"""

from __future__ import annotations

from dida_adapter_dlna.adapter import _SILENT_PASSES, DlnaAdapter, Renderer
from dida_core.health import StatusReporter


def _renderer(eid: str) -> Renderer:
    """The REAL Renderer, with only the network client stubbed.

    A hand-rolled stand-in was tried first and failed on `icy_task` and `last` —
    fields the dispatcher genuinely uses. A stub that has to grow every attribute the
    code touches is a second, worse copy of the class; using the real one means the
    test breaks when the object changes, which is the point."""
    return Renderer(udn=f"uuid:{eid}", eid=eid, name=eid, dmr=None, location="http://x/d")


def _adapter(renderers=(), heard=False, fails=None):
    a = DlnaAdapter()
    # The runner injects this (adapter_runner.py) — adapters report through
    # `self.status`, they do not own it. Attaching a real StatusReporter rather than
    # a recorder keeps the state machine (idle/ok/error, and the transition
    # bookkeeping) in the test instead of stubbed away.
    a.status = StatusReporter("dlna")
    a._iface_ip = "192.168.1.100"
    a._heard = heard
    a._renderers = {f"udn{i}": _renderer(e) for i, e in enumerate(renderers)}
    a._by_eid = {r.entity_id: r for r in a._renderers.values()}
    a._poll_fails = dict(fails or {})
    return a


def _state(a):
    return a.status.snapshot()["state"], a.status.snapshot()["detail"]


# --- the empty state: discovery owns the badge --------------------------------


def test_lifelong_silence_is_reported_as_a_fault():
    """The whole point. Nothing has ever answered — on a correctly-wired LAN even the
    excluded renderers reply — so the search is going out of the wrong interface."""
    a = _adapter()
    for _ in range(_SILENT_PASSES):
        a._discovery_badge(replies=0, silent=_SILENT_PASSES)
    state, detail = _state(a)
    assert state == "error"
    assert "192.168.1.100" in detail, "the badge must name the leg it is bound to"


def test_ONE_reply_ever_disarms_the_alarm_for_good():
    """Replies are UDP and devices rate-limit M-SEARCH: an empty round is normal on a
    healthy LAN (measured: 3, then 2, then 0 within minutes). Alarming on a
    consecutive run would cry wolf every few hours."""
    a = _adapter()
    a._discovery_badge(replies=1, silent=0)
    for _ in range(_SILENT_PASSES * 3):
        a._discovery_badge(replies=0, silent=_SILENT_PASSES * 3)
    assert _state(a)[0] != "error", "a proven-good leg must not later be called broken"


def test_a_few_silent_passes_are_not_yet_a_fault():
    """Below the threshold this is 'discovering', not a problem."""
    a = _adapter()
    a._discovery_badge(replies=0, silent=_SILENT_PASSES - 1)
    assert _state(a)[0] == "idle"


def test_hearing_devices_but_driving_none_is_idle_not_an_error():
    """The TVs are off and the iFi/Marantz are excluded on purpose. Ordinary."""
    a = _adapter()
    a._discovery_badge(replies=4, silent=0)
    state, detail = _state(a)
    assert state == "idle"
    assert "none to drive" in detail


def test_driving_a_renderer_means_discovery_does_NOT_touch_the_badge():
    """Once there is something to drive, reachability owns the badge — otherwise a
    quiet discovery round would overwrite a live 'ok'."""
    a = _adapter(renderers=["dlna:tv"])
    a.status.ok("1 renderer(s)")
    a._discovery_badge(replies=0, silent=_SILENT_PASSES * 5)
    assert _state(a) == ("ok", "1 renderer(s)")


# --- the driving state: reachability owns the badge ---------------------------


def test_every_renderer_reachable_is_ok():
    a = _adapter(renderers=["dlna:tv", "dlna:beamer"])
    a._update_badge()
    assert _state(a) == ("ok", "2 renderer(s)")


def test_some_unreachable_is_still_ok_but_says_how_many():
    a = _adapter(renderers=["dlna:tv", "dlna:beamer"], fails={"dlna:tv": 3})
    a._update_badge()
    assert _state(a) == ("ok", "1/2 on")


def test_ALL_unreachable_is_idle_not_an_error():
    """For DLNA every renderer being unreachable is almost always the TVs being off —
    their normal state. A red badge here would fire nightly and train the operator to
    ignore the one that matters."""
    a = _adapter(renderers=["dlna:tv"], fails={"dlna:tv": 3})
    a._update_badge()
    assert _state(a) == ("idle", "0/1 on")


def test_a_renderer_is_only_dead_after_repeated_failures():
    """One missed poll is a dropped UDP packet, not a dead TV."""
    a = _adapter(renderers=["dlna:tv"], fails={"dlna:tv": 2})
    a._update_badge()
    assert _state(a) == ("ok", "1 renderer(s)")


def test_a_failure_count_for_a_renderer_that_is_GONE_is_ignored():
    """`_poll_fails` outlives the renderer it counted; counting a departed device as
    dead would report 0/1 on for a device that is simply no longer there."""
    a = _adapter(renderers=["dlna:tv"], fails={"dlna:tv": 0, "dlna:vanished": 9})
    a._update_badge()
    assert _state(a) == ("ok", "1 renderer(s)")


def test_with_nothing_to_drive_reachability_leaves_the_badge_alone():
    a = _adapter()
    a.status.idle("discovering")
    a._update_badge()
    assert _state(a) == ("idle", "discovering")


# --- command dispatch: a command that silently does nothing is the worst kind ----


class _Dmr:
    """Records what the renderer was actually told to do."""

    def __init__(self, transport="STOPPED", volume=0.5, muted=False) -> None:
        self.calls: list[tuple[str, object]] = []
        self.transport_state = transport
        self.volume_level = volume
        self.is_volume_muted = muted

    def _rec(self, name):
        async def _f(*a):
            self.calls.append((name, a[0] if a else None))
        return _f

    def __getattr__(self, name):
        if name.startswith("async_"):
            return self._rec(name)
        raise AttributeError(name)


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, object]] = []

    async def publish_state(self, update) -> None:
        self.published.append((update.entity_id, update.capability, update.value))

    async def publish_entity(self, info) -> None:
        pass


def _with_dmr(**kw):
    a = _adapter(renderers=["dlna:tv"])
    a._bus = _Bus()          # queue moves publish; without it `_play_index` asserts
    r = a._by_eid["dlna:tv"]
    r.dmr = _Dmr(**kw)
    return a, r


async def test_play_and_pause_reach_the_renderer():
    a, r = _with_dmr()
    await a._dispatch(r, "media_transport", "play", {})
    await a._dispatch(r, "media_transport", "pause", {})
    assert [c for c, _ in r.dmr.calls] == ["async_play", "async_pause"]


async def test_play_pause_toggles_from_the_CURRENT_transport_state():
    """A blind toggle would pause a stopped renderer — the button would look dead."""
    a, r = _with_dmr(transport="PLAYING")
    await a._dispatch(r, "media_transport", "play_pause", {})
    assert r.dmr.calls[-1][0] == "async_pause"

    a2, r2 = _with_dmr(transport="STOPPED")
    await a2._dispatch(r2, "media_transport", "play_pause", {})
    assert r2.dmr.calls[-1][0] == "async_play"


async def test_stop_ends_the_queue_AND_the_radio():
    """Leaving radio mode running kept the ICY task reopening the upstream stream
    every 15 s forever, and its stale station name masked whatever played next."""
    a, r = _with_dmr()
    r.queue = [{"url": "a"}, {"url": "b"}]
    r.qpos = 1
    r.radio = {"url": "http://stream", "station": "Radio 1"}

    await a._dispatch(r, "media_transport", "stop", {})
    assert r.queue == [] and r.qpos == -1
    assert r.radio is None, "radio mode survived a stop"
    assert ("async_stop", None) in r.dmr.calls


async def test_next_prefers_DIDAs_queue_over_the_renderers_own():
    """The renderer's own `next` knows nothing about the playlist DIDA is driving —
    using it would skip to whatever the device had queued, or nothing at all."""
    a, r = _with_dmr()
    r.queue = [{"url": "a", "title": "A"}, {"url": "b", "title": "B"}]
    r.qpos = 0
    await a._dispatch(r, "media_transport", "next", {})
    assert "async_next" not in [c for c, _ in r.dmr.calls], \
        "fell through to the renderer while DIDA had a next track"


async def test_next_at_the_END_of_the_queue_falls_through_to_the_renderer():
    a, r = _with_dmr()
    r.queue = [{"url": "a"}]
    r.qpos = 0
    await a._dispatch(r, "media_transport", "next", {})
    assert "async_next" in [c for c, _ in r.dmr.calls]


async def test_previous_at_the_START_falls_through_too():
    a, r = _with_dmr()
    r.queue = [{"url": "a"}]
    r.qpos = 0
    await a._dispatch(r, "media_transport", "previous", {})
    assert "async_previous" in [c for c, _ in r.dmr.calls]


async def test_volume_up_and_down_are_relative_to_the_current_level():
    a, r = _with_dmr(volume=0.5)
    await a._dispatch(r, "volume", "volume_up", {})
    up = r.dmr.calls[-1][1]
    await a._dispatch(r, "volume", "volume_down", {})
    down = r.dmr.calls[-1][1]
    assert up > 0.5 > down


async def test_volume_is_clamped_at_both_ends():
    a, r = _with_dmr(volume=1.0)
    await a._dispatch(r, "volume", "volume_up", {})
    assert r.dmr.calls[-1][1] == 1.0

    a2, r2 = _with_dmr(volume=0.0)
    await a2._dispatch(r2, "volume", "volume_down", {})
    assert r2.dmr.calls[-1][1] == 0.0


async def test_mute_toggle_reads_the_current_mute_state():
    a, r = _with_dmr(muted=False)
    await a._dispatch(r, "mute", "toggle", {})
    assert r.dmr.calls[-1] == ("async_mute_volume", True)

    a2, r2 = _with_dmr(muted=True)
    await a2._dispatch(r2, "mute", "toggle", {})
    assert r2.dmr.calls[-1] == ("async_mute_volume", False)


async def test_an_unknown_command_is_a_no_op_not_a_crash():
    """An adapter must not die on a command it does not implement — the process is
    shared by every renderer in the house."""
    a, r = _with_dmr()
    await a._dispatch(r, "media_transport", "teleport", {})
    assert r.dmr.calls == []
