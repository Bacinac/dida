"""A light switched on by hand is held against rules while its room is occupied.

No DB/NATS: a fake pool answers the entity query and records the writes to
manual_overrides; a fake bus records commands and journal events.
"""
import asyncio
import time

from dida_automation import manual
from dida_automation.__main__ import AutomationEngine, Rule
from dida_automation.manual import ManualOverrides
from dida_core import Action, AutomationDef, Command, StateUpdate, Trigger

LIGHT = "mqtt:living_lamp"
DARK = "mqtt:hall_lamp"
FP2 = "homekit:living"


class FakeBus:
    def __init__(self):
        self.commands = []
        self.journal = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def publish_state(self, upd):
        pass

    async def publish_journal(self, event):
        self.journal.append(event)


class Pool:
    def __init__(self):
        self.held: dict[str, object] = {}
        self.runs: list[tuple] = []

    async def fetch(self, sql, *a):
        if "manual_overrides" in sql:
            return [{"entity_id": e, "since": s} for e, s in self.held.items()]
        if "FROM entities" in sql:
            return [
                {"entity_id": LIGHT, "area_id": 1, "device_type": "light",
                 "capabilities": ["on_off", "brightness"], "hidden_caps": []},
                {"entity_id": FP2, "area_id": 1, "device_type": "sensor",
                 "capabilities": ["occupancy"], "hidden_caps": []},
                {"entity_id": DARK, "area_id": 2, "device_type": "light",
                 "capabilities": ["on_off"], "hidden_caps": []},
            ]
        return []

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO manual_overrides"):
            self.held.setdefault(a[0], a[1])
        elif sql.startswith("DELETE FROM manual_overrides"):
            self.held.pop(a[0], None)
        elif sql.startswith("INSERT INTO automation_runs"):
            self.runs.append(a)

    async def fetchval(self, *a):
        return None

    async def fetchrow(self, *a):
        return None


async def overrides():
    pool = Pool()
    mo = ManualOverrides(FakeBus(), pool)
    await mo.refresh()
    await mo.load_held()
    return mo, pool


def off(entity=LIGHT):
    return Command(entity_id=entity, capability="on_off", command="turn_off", ts_ns=0)


async def test_a_light_switched_on_by_hand_is_held_and_recorded():
    mo, pool = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)
    assert LIGHT in mo.held and LIGHT in pool.held
    assert [e.kind for e in mo._bus.journal] == ["manual_override"]
    assert mo.holds(off(), {(LIGHT, "on_off"): True})


async def test_a_light_a_rule_switched_on_is_not_held():
    mo, _ = await overrides()
    mo.note_command(Command(entity_id=LIGHT, capability="on_off", command="turn_on", ts_ns=0))
    await mo.on_state(LIGHT, "on_off", False, True, True)
    assert LIGHT not in mo.held


async def test_a_light_in_a_room_nobody_can_be_seen_in_is_never_held():
    mo, _ = await overrides()
    await mo.on_state(DARK, "on_off", False, True, True)
    assert DARK not in mo.held


async def test_a_replayed_or_first_report_does_not_count_as_by_hand():
    mo, _ = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, False)
    await mo.on_state(LIGHT, "on_off", None, True, True)
    assert LIGHT not in mo.held


async def test_the_hold_blocks_only_turning_off():
    mo, _ = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)
    on = {(LIGHT, "on_off"): True}
    assert mo.holds(Command(entity_id=LIGHT, capability="on_off", command="toggle", ts_ns=0), on)
    assert not mo.holds(Command(entity_id=LIGHT, capability="on_off", command="turn_on", ts_ns=0), on)
    assert not mo.holds(Command(entity_id=LIGHT, capability="brightness", command="set",
                                args={"value": 20}, ts_ns=0), on)


async def test_turning_the_light_off_ends_the_hold():
    mo, pool = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)
    await mo.on_state(LIGHT, "on_off", True, False, True)
    assert LIGHT not in mo.held and LIGHT not in pool.held
    assert mo._bus.journal[-1].kind == "manual_released"


async def test_the_hold_lets_go_only_after_the_room_stays_empty(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(manual.time, "monotonic", lambda: clock[0])
    mo, pool = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)

    snap = {(LIGHT, "on_off"): True, (FP2, "occupancy"): True}
    clock[0] += 600
    await mo.sweep(snap)
    assert LIGHT in mo.held, "an occupied room keeps the hold however long it lasts"

    snap[(FP2, "occupancy")] = False
    await mo.sweep(snap)
    clock[0] += manual.RELEASE_AFTER_S - 1
    await mo.sweep(snap)
    assert LIGHT in mo.held, "a short gap in presence is not an empty room"

    clock[0] += 2
    await mo.sweep(snap)
    assert LIGHT not in mo.held and LIGHT not in pool.held


async def test_unknown_presence_keeps_the_hold_and_restarts_the_full_empty_interval(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(manual.time, "monotonic", lambda: clock[0])
    mo, pool = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)
    snap = {(LIGHT, "on_off"): True, (FP2, "occupancy"): False}
    await mo.sweep(snap)
    clock[0] += 30
    del snap[(FP2, "occupancy")]
    await mo.sweep(snap)
    clock[0] += 600
    await mo.sweep(snap)
    assert LIGHT in mo.held and LIGHT in pool.held
    snap[(FP2, "occupancy")] = False
    await mo.sweep(snap)
    clock[0] += manual.RELEASE_AFTER_S - 1
    await mo.sweep(snap)
    assert LIGHT in mo.held
    clock[0] += 1
    await mo.sweep(snap)
    assert LIGHT not in mo.held


async def test_one_false_sensor_and_one_unknown_sensor_do_not_prove_an_empty_room(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(manual.time, "monotonic", lambda: clock[0])
    mo, _ = await overrides()
    mo._presence[1].append(("mqtt:motion", "motion"))
    await mo.on_state(LIGHT, "on_off", False, True, True)
    snap = {(LIGHT, "on_off"): True, (FP2, "occupancy"): False}
    await mo.sweep(snap)
    clock[0] += 600
    await mo.sweep(snap)
    assert LIGHT in mo.held


async def test_a_hold_survives_a_restart():
    mo, pool = await overrides()
    await mo.on_state(LIGHT, "on_off", False, True, True)
    again = ManualOverrides(FakeBus(), pool)
    await again.refresh()
    await again.load_held()
    assert LIGHT in again.held


def _lights_off_when_empty():
    return AutomationDef(
        triggers=[Trigger(entity_id=FP2, capability="occupancy", to=False)],
        actions=[Action(entity_id=LIGHT, capability="on_off", command="turn_off")],
    )


async def _engine():
    pool = Pool()
    eng = AutomationEngine(FakeBus(), pool)
    await eng._manual.refresh()
    eng._rules = [Rule(5, "living-off", _lights_off_when_empty())]
    eng._last = {(FP2, "occupancy"): True, (LIGHT, "on_off"): False}
    return eng, pool


def upd(eid, cap, val):
    now = time.time_ns()
    return StateUpdate(entity_id=eid, capability=cap, value=val, adapter="test", ts_ns=now, received_ns=now)


async def test_a_rule_does_not_turn_off_a_light_held_by_hand():
    eng, pool = await _engine()
    await eng.on_event(upd(LIGHT, "on_off", True))
    await eng.on_event(upd(FP2, "occupancy", False))
    await asyncio.sleep(0.02)
    assert eng._bus.commands == []
    assert [r[2] for r in pool.runs] == ["held"]


async def test_the_same_rule_turns_it_off_once_a_rule_switched_it_on():
    eng, _ = await _engine()
    eng._manual.note_command(Command(entity_id=LIGHT, capability="on_off", command="turn_on", ts_ns=0))
    await eng.on_event(upd(LIGHT, "on_off", True))
    await eng.on_event(upd(FP2, "occupancy", False))
    await asyncio.sleep(0.02)
    assert [c.command for c in eng._bus.commands] == ["turn_off"]


async def test_running_the_rule_by_hand_is_not_held():
    eng, _ = await _engine()
    await eng.on_event(upd(LIGHT, "on_off", True))
    await eng.run_now(eng._rules[0])
    assert [c.command for c in eng._bus.commands] == ["turn_off"]
