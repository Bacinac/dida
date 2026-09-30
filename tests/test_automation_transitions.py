"""Regression tests for transition-aware automation triggering (audit A1/A2/A3).

Run inside the automation image (has dida_core + dida_automation installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/automation:latest \
      -c "python tests/test_automation_transitions.py"

No DB/NATS needed — a fake pool/bus stands in. Asserts:
  A1 — a `to=` trigger fires on the transition INTO the value, NOT on an
       unchanged re-report (the z2m-checkin / esphome-reconnect re-fire bug).
  A2 — a `for_seconds` hold does NOT restart on an unchanged re-report (a
       periodic reporter could otherwise push the fire out forever), and cancels
       when the value moves away.
  STALE — a JetStream-replayed event older than STALE_EVENT_NS updates the cache
       but never fires an action, and is cache-only for holds: it neither arms nor
       cancels one (H — a stale move-away must not drop a reconstructed hold).
"""
import asyncio
import contextlib
import time

import msgspec
from dida_automation.__main__ import (
    FIRE_RATE_LIMIT,
    STALE_EVENT_NS,
    AutomationEngine,
    Helper,
    Rule,
)
from dida_core import Action, AutomationDef, Condition, StateUpdate, Trigger


class FakeBus:
    def __init__(self):
        self.commands = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def publish_state(self, upd):
        pass


class FakePool:
    async def execute(self, *a, **k):
        pass

    async def fetch(self, *a, **k):
        return []

    async def fetchval(self, *a, **k):
        return None

    async def fetchrow(self, *a, **k):
        return None


def upd(eid, cap, val, *, age_s: float = 0.0, source_lag_s: float = 0.0):
    # The staleness gate reads `received_ns`, the bus's own clock. Default = a fresh
    # event; age_s simulates a JetStream replay of an old one, source_lag_s a device
    # or peer whose clock runs behind.
    now = time.time_ns()
    return StateUpdate(
        entity_id=eid, capability=cap, value=val, adapter="test",
        ts_ns=now - int((age_s + source_lag_s) * 1e9),
        received_ns=now - int(age_s * 1e9),
    )


async def test_a1_fires_on_transition_not_on_rereport():
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:door", capability="contact", to=True)],
        actions=[Action(entity_id="mqtt:siren", capability="on_off", command="turn_on")],
    )
    eng._rules = [Rule(1, "alarm", defn)]
    eng._last = {("mqtt:door", "contact"): False}

    await eng.on_event(upd("mqtt:door", "contact", True))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1, "should fire once on the false->true transition"

    await eng.on_event(upd("mqtt:door", "contact", True))  # unchanged re-report
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1, "must NOT re-fire on an unchanged re-report"


async def test_a2_hold_not_restarted_by_rereport():
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=False, for_seconds=300)],
        actions=[Action(entity_id="mqtt:light", capability="on_off", command="turn_off")],
    )
    eng._rules = [Rule(2, "lights-off", defn)]
    eng._last = {("mqtt:pir", "occupancy"): True}

    await eng.on_event(upd("mqtt:pir", "occupancy", False))
    task1 = eng._pending.get((2, 0))
    assert task1 is not None, "hold should start on the true->false transition"

    await eng.on_event(upd("mqtt:pir", "occupancy", False))  # unchanged re-report
    assert eng._pending.get((2, 0)) is task1, "unchanged re-report must NOT restart the hold"

    await eng.on_event(upd("mqtt:pir", "occupancy", True))  # move away
    assert eng._pending.get((2, 0)) is None, "hold cancelled when value leaves the condition"
    await asyncio.sleep(0.02)
    assert task1.cancelled() or task1.done()


async def test_hold_fire_not_cancelled_by_moveaway():
    # Once a hold has passed its sleep + re-confirm and COMMITS to fire, a move-away
    # arriving mid-fire must NOT cancel the side-effecting sequence partway (a gate
    # left half-actuated). The fire un-registers from `_pending` before running, so
    # the move-away's cancel finds nothing to cancel.
    class ConfirmPool(FakePool):
        async def fetchval(self, *a, **k):
            return False  # re-confirm sees the value still held → proceed to fire

    eng = AutomationEngine(FakeBus(), ConfirmPool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=False, for_seconds=0.05)],
        actions=[Action(entity_id="mqtt:gate", capability="on_off", command="turn_on")],
    )
    eng._rules = [Rule(3, "gate", defn)]
    eng._last = {("mqtt:pir", "occupancy"): True}

    seen: dict = {}
    async def slow_fire(rule, trig, update):
        seen["pending_at_fire"] = eng._pending.get((3, 0))  # must be None (un-registered)
        await asyncio.sleep(0.15)  # a multi-step physical sequence
        seen["done"] = True

    eng._fire = slow_fire
    await eng.on_event(upd("mqtt:pir", "occupancy", False))  # arm the hold
    assert eng._pending.get((3, 0)) is not None, "hold armed on the transition"

    await asyncio.sleep(0.10)  # for_seconds (0.05) elapses → sleep+reconfirm pass → _fire is now running
    await eng.on_event(upd("mqtt:pir", "occupancy", True))  # MOVE-AWAY mid-fire
    await asyncio.sleep(0.20)  # let the fire finish

    assert seen.get("pending_at_fire") is None, "hold un-registered from _pending BEFORE firing"
    assert seen.get("done") is True, "the fire ran to completion despite the mid-fire move-away"


async def test_reconstruct_holds_rearms_mid_countdown():
    # After a restart mid-hold, an enabled held rule whose value STILL equals `to`
    # re-arms the REMAINING time (a deploy mid-countdown mustn't leave lights on all
    # night); a rule whose value moved away is NOT re-armed.
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=False, for_seconds=300)],
        actions=[Action(entity_id="mqtt:light", capability="on_off", command="turn_off")],
    )

    class HeldPool(FakePool):
        async def fetchrow(self, *a, **k):
            return {"value": False, "age": 100.0}  # value == to(False), 100s already elapsed

    eng = AutomationEngine(FakeBus(), HeldPool())
    eng._rules = [Rule(7, "lights-off", defn)]
    await eng.reconstruct_holds()
    task = eng._pending.get((7, 0))
    assert task is not None, "a mid-countdown hold is re-armed on boot"
    task.cancel()  # 200s remaining — don't actually wait it out
    with contextlib.suppress(asyncio.CancelledError):
        await task

    class MovedAwayPool(FakePool):
        async def fetchrow(self, *a, **k):
            return {"value": True, "age": 100.0}  # value != to → no longer qualifies

    eng2 = AutomationEngine(FakeBus(), MovedAwayPool())
    eng2._rules = [Rule(8, "lights-off", defn)]
    await eng2.reconstruct_holds()
    assert eng2._pending.get((8, 0)) is None, "not re-armed once the value no longer qualifies"


async def test_to_none_fires_only_on_change():
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:btn", capability="number")],
        actions=[Action(entity_id="mqtt:x", capability="on_off", command="turn_on")],
    )
    eng._rules = [Rule(3, "onchange", defn)]
    eng._last = {("mqtt:btn", "number"): 5.0}

    await eng.on_event(upd("mqtt:btn", "number", 5.0))  # unchanged
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 0

    await eng.on_event(upd("mqtt:btn", "number", 6.0))  # changed
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1


async def test_stale_events_update_cache_but_never_act():
    stale_age = (STALE_EVENT_NS / 1e9) + 10
    eng = AutomationEngine(FakeBus(), FakePool())
    fire_defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:door", capability="contact", to=True)],
        actions=[Action(entity_id="mqtt:siren", capability="on_off", command="turn_on")],
    )
    hold_defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=False, for_seconds=300)],
        actions=[Action(entity_id="mqtt:light", capability="on_off", command="turn_off")],
    )
    eng._rules = [Rule(1, "alarm", fire_defn), Rule(2, "lights-off", hold_defn)]
    eng._last = {("mqtt:door", "contact"): False, ("mqtt:pir", "occupancy"): True}

    # stale transition into `to` — cache updates, NO fire
    await eng.on_event(upd("mqtt:door", "contact", True, age_s=stale_age))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 0, "stale replayed event must NOT fire an action"
    assert eng._last[("mqtt:door", "contact")] is True, "stale event must still update the cache"

    # stale qualifying event — NO hold armed
    await eng.on_event(upd("mqtt:pir", "occupancy", False, age_s=stale_age))
    assert eng._pending.get((2, 0)) is None, "stale event must NOT arm a hold"

    # a stale move-away must NOT cancel a pending hold (H): it's older than the state
    # reconstruct armed from, and being stale it could never re-arm — only a FRESH
    # move-away cancels. First arm a REAL hold: the stale replay above already caught
    # the cache up to False, so a plain re-report of False is no longer a transition
    # (correctly won't arm) — we must go occupied→idle afresh to arm.
    await eng.on_event(upd("mqtt:pir", "occupancy", True))   # fresh: occupied again
    await eng.on_event(upd("mqtt:pir", "occupancy", False))  # fresh transition → arms
    hold = eng._pending.get((2, 0))
    assert hold is not None
    await eng.on_event(upd("mqtt:pir", "occupancy", True, age_s=stale_age))  # stale move-away
    assert eng._pending.get((2, 0)) is hold, "stale move-away must NOT cancel the hold (H)"
    await eng.on_event(upd("mqtt:pir", "occupancy", True))   # fresh move-away
    assert eng._pending.get((2, 0)) is None, "fresh move-away DOES cancel the hold"
    await asyncio.sleep(0.02)


async def test_a_source_clock_running_behind_does_not_make_triggers_stale():
    """A peer installation or a device whose clock lags reported every event as old,
    and every rule it triggered was skipped without a word."""
    eng = AutomationEngine(FakeBus(), FakePool())
    eng._rules = [Rule(1, "alarm", AutomationDef(
        triggers=[Trigger(entity_id="peer:door", capability="contact", to=True)],
        actions=[Action(entity_id="mqtt:siren", capability="on_off", command="turn_on")],
    ))]
    eng._last = {("peer:door", "contact"): False}
    await eng.on_event(upd("peer:door", "contact", True, source_lag_s=600))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1


async def test_skipped_stale_triggers_are_recorded_once_per_burst(monkeypatch):
    monkeypatch.setattr("dida_automation.__main__.STALE_NOTE_DELAY_S", 0.01)
    journal = []

    async def _emit(bus, kind, **kw):
        journal.append((kind, kw))

    monkeypatch.setattr("dida_automation.__main__.emit_journal", _emit)
    runs = []
    eng = AutomationEngine(FakeBus(), FakePool())

    async def _record(rule, outcome, detail=""):
        runs.append((rule.id, outcome, detail))

    eng._record_run = _record
    eng._rules = [Rule(1, "motion", AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=True)],
        actions=[Action(entity_id="mqtt:light", capability="on_off", command="turn_on")],
    ))]
    stale_age = (STALE_EVENT_NS / 1e9) + 10
    for value in (True, False, True):
        await eng.on_event(upd("mqtt:pir", "occupancy", value, age_s=stale_age))
    await eng.on_event(msgspec.structs.replace(
        upd("mqtt:pir", "occupancy", False), received_ns=None))
    await eng.on_event(msgspec.structs.replace(
        upd("mqtt:pir", "occupancy", True), received_ns=None))
    await asyncio.sleep(0.05)
    assert len(eng._bus.commands) == 0
    assert runs == [(1, "stale", "3")], "one run-log row per burst, with the count"
    assert [k for k, _ in journal] == ["automation_stale"]
    assert journal[0][1]["data"]["count"] == 3


async def test_helper_transition_fires_consumer():
    """A computed helper's value change must fire a consumer's `to=` trigger.

    The helper is published by THIS service, but its value must still round-trip
    through on_event (as the engine's projection re-emits it) to advance _last —
    otherwise the consumer never sees the transition. Regression for the
    optimistic-_last bug that left the irrigation / mower `to=` consumers silent
    while the helper itself computed the right value.
    """
    # A bus that feeds every published helper state straight back into on_event,
    # exactly as the engine's projection would re-emit it on dida.events.
    holder = {}

    class RoundTripBus(FakeBus):
        async def publish_state(self, upd):
            await holder["eng"].on_event(msgspec.structs.replace(upd, received_ns=time.time_ns()))

    eng = AutomationEngine(RoundTripBus(), FakePool())
    holder["eng"] = eng

    # helper:zone → "Zone 1" once the sun dips below -0.5°, else "Off" (typed, no code).
    eng._helpers = {"helper:zone": Helper("helper:zone", "Zone", "text", {
        "branches": [{"conditions": [{"entity_id": "astro:sun", "capability": "sun_elevation",
                                      "op": "<", "value": -0.5}], "value": "Zone 1"}],
        "default": "Off",
    })}
    # Consumer: open a valve when helper:zone transitions INTO "Zone 1".
    defn = AutomationDef(
        triggers=[Trigger(entity_id="helper:zone", capability="text", to="Zone 1")],
        actions=[Action(entity_id="esphome:valve", capability="on_off", command="turn_on")],
    )
    eng._rules = [Rule(1, "irrigation", defn)]
    # Start settled: sun above the threshold, helper already "Off".
    eng._last = {("astro:sun", "sun_elevation"): 5.0, ("helper:zone", "text"): "Off"}
    eng._published = {("helper:zone", "text"): "Off"}

    # Sun drops below -0.5 → helper recomputes "Zone 1" → publishes → round-trip → fires.
    await eng.on_event(upd("astro:sun", "sun_elevation", -1.0))
    await asyncio.sleep(0.05)
    assert eng._last[("helper:zone", "text")] == "Zone 1", "helper value advanced via the round-trip"
    assert len(eng._bus.commands) == 1, "consumer must fire on the helper's Off->Zone 1 transition"
    assert eng._bus.commands[0].entity_id == "esphome:valve"

    # A further sun tick that keeps the helper at "Zone 1" must NOT re-publish or re-fire.
    await eng.on_event(upd("astro:sun", "sun_elevation", -1.2))
    await asyncio.sleep(0.05)
    assert len(eng._bus.commands) == 1, "unchanged helper value must not re-fire the consumer"


async def test_runaway_firing_guard_auto_disables():
    # A rule that fires SUCCESSFULLY over and over (a feedback loop) never trips the
    # error breaker — the firing-rate guard must stop it. Drive _fire directly so
    # each completes before the next (no mode=single overlap masking the rate).
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:x", capability="number")],
        actions=[Action(entity_id="mqtt:y", capability="on_off", command="turn_on")],
    )
    rule = Rule(7, "loop", defn)
    eng._rules = [rule]

    for i in range(FIRE_RATE_LIMIT + 5):
        await eng._fire(rule, None, upd("mqtt:x", "number", float(i)))

    assert rule.disabled is True, "runaway rule must auto-disable"
    assert len(eng._bus.commands) == FIRE_RATE_LIMIT, (
        f"must fire exactly {FIRE_RATE_LIMIT}× then stop, got {len(eng._bus.commands)}"
    )
    # once disabled, further fires are a no-op (halted until reload drops it)
    await eng._fire(rule, None, upd("mqtt:x", "number", 999.0))
    assert len(eng._bus.commands) == FIRE_RATE_LIMIT, "a disabled rule must not fire again"


async def test_cooldown_silences_the_second_crossing_of_one_visit():
    """A zone people WALK THROUGH reports every crossing truthfully: one visit
    crosses it on the way in and again on the way out, and the gate camera's own
    contract forbids the consumer from doctoring that state. The cooldown is where
    "don't tell me twice" belongs — the mirror stays exact, only firing is spaced."""
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="baba:gate:zone", capability="occupancy", to=True)],
        actions=[Action(entity_id="mqtt:siren", capability="on_off", command="turn_on")],
        cooldown_seconds=300,
    )
    rule = Rule(3, "gate-person", defn)
    eng._rules = [rule]
    eng._last = {("baba:gate:zone", "occupancy"): False}

    await eng.on_event(upd("baba:gate:zone", "occupancy", True))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1, "the arrival still fires"

    # Out of the zone and back in — a GENUINE transition, not a re-report.
    await eng.on_event(upd("baba:gate:zone", "occupancy", False))
    await eng.on_event(upd("baba:gate:zone", "occupancy", True))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 1, "the way back out of the same visit stays silent"
    assert len(rule.fires) == 1, "a suppressed match must not feed the runaway guard"

    # A cooldown is not a mute button: the next visit, past it, rings again.
    rule.last_fire -= 301
    await eng.on_event(upd("baba:gate:zone", "occupancy", False))
    await eng.on_event(upd("baba:gate:zone", "occupancy", True))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 2, "a later visit fires once the cooldown lapsed"


async def test_no_cooldown_leaves_every_transition_firing():
    eng = AutomationEngine(FakeBus(), FakePool())
    defn = AutomationDef(
        triggers=[Trigger(entity_id="mqtt:pir", capability="occupancy", to=True)],
        actions=[Action(entity_id="mqtt:siren", capability="on_off", command="turn_on")],
    )
    eng._rules = [Rule(4, "no-cooldown", defn)]
    eng._last = {("mqtt:pir", "occupancy"): False}
    for _ in range(3):
        await eng.on_event(upd("mqtt:pir", "occupancy", True))
        await eng.on_event(upd("mqtt:pir", "occupancy", False))
    await asyncio.sleep(0.02)
    assert len(eng._bus.commands) == 3, "absent a cooldown, nothing is suppressed"


# --- an unreachable device is unknown, not its last value ----------------------
# 14.09: the living-room FP2 dropped off the network with its zones on "empty".
# The music rule read that memory as a reading and switched the room off around
# the people in it. A device the engine marks unreachable must read as unknown.


class RowsPool(FakePool):
    def __init__(self, rows=(), held=None):
        self.rows = list(rows)
        self.held = held

    async def fetch(self, sql, *a, **k):
        if "reachable" in sql:
            return [{"entity_id": e} for e in self.unreachable]
        return self.rows

    async def fetchval(self, *a, **k):
        return self.held

    unreachable: tuple = ()


def _music_off():
    return AutomationDef(
        triggers=[Trigger(entity_id="esphome:kitchen", capability="occupancy", to=False)],
        conditions=[Condition(entity_id="homekit:living", capability="occupancy", op="==", value=False)],
        actions=[Action(entity_id="denon:amp", capability="on_off", command="turn_off")],
    )


async def _kitchen_empties(unreachable):
    pool = RowsPool(rows=[{"entity_id": "homekit:living", "capability": "occupancy", "value": False}])
    pool.unreachable = unreachable
    eng = AutomationEngine(FakeBus(), pool)
    await eng.refresh_reachability()
    eng._rules = [Rule(9, "music-off", _music_off())]
    eng._last = {("esphome:kitchen", "occupancy"): True, ("homekit:living", "occupancy"): False}
    await eng.on_event(upd("esphome:kitchen", "occupancy", False))
    await asyncio.sleep(0.02)
    return eng._bus.commands


async def test_a_condition_on_an_unreachable_device_does_not_pass_on_its_last_value():
    assert await _kitchen_empties(("homekit:living",)) == []


async def test_the_same_condition_passes_while_the_device_is_reachable():
    assert len(await _kitchen_empties(())) == 1


async def test_a_script_snapshot_leaves_the_unreachable_device_out():
    pool = RowsPool()
    pool.unreachable = ("homekit:living",)
    eng = AutomationEngine(FakeBus(), pool)
    eng._last = {("homekit:living", "occupancy"): False, ("esphome:kitchen", "occupancy"): True}
    await eng.refresh_reachability()
    assert eng._snapshot() == {("esphome:kitchen", "occupancy"): True}


async def _hold_on(unreachable_after_arming):
    pool = RowsPool(held=False)
    eng = AutomationEngine(FakeBus(), pool)
    defn = AutomationDef(
        triggers=[Trigger(entity_id="homekit:living", capability="occupancy", to=False, for_seconds=0.05)],
        actions=[Action(entity_id="denon:amp", capability="on_off", command="turn_off")],
    )
    eng._rules = [Rule(10, "held-off", defn)]
    eng._last = {("homekit:living", "occupancy"): True}
    await eng.on_event(upd("homekit:living", "occupancy", False))
    pool.unreachable = unreachable_after_arming
    await eng.refresh_reachability()
    await asyncio.sleep(0.15)
    return eng._bus.commands


async def test_a_hold_does_not_fire_on_a_device_that_went_unreachable_during_it():
    assert await _hold_on(("homekit:living",)) == []


async def test_a_hold_fires_when_the_device_stays_reachable():
    assert len(await _hold_on(())) == 1


async def test_a_helper_reading_a_device_is_recomputed_when_the_device_drops_off():
    published = []

    class Bus(FakeBus):
        async def publish_state(self, u):
            published.append(u.value)

    pool = RowsPool()
    eng = AutomationEngine(Bus(), pool)
    eng._helpers = {"helper:room": Helper("helper:room", "Room", "text", {
        "branches": [{"conditions": [{"entity_id": "homekit:living", "capability": "occupancy",
                                      "op": "==", "value": False}], "value": "empty"}],
        "default": "unknown",
    })}
    eng._last = {("homekit:living", "occupancy"): False}
    eng._published = {("helper:room", "text"): "empty"}
    pool.unreachable = ("homekit:living",)
    await eng.refresh_reachability()
    await asyncio.sleep(0.05)
    assert published == ["unknown"]
