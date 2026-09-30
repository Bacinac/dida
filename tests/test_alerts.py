"""Unit tests for the system-health alert evaluator (dida_api.alerts).

The evaluator's decision logic — threshold conditions, the buffer-drop edge, the
anti-flap hold, fire→resolve transitions, and the critical-only admin notify — is
driven here against stubbed collaborators (a fake health snapshot, a stub pool/
bus/ClickHouse). No Postgres, no bus: pure logic. Runs in the api image.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import dida_api.alerts as alerts
from dida_api.alerts import Evaluator, _disk_free_pct


class StubBus:
    def __init__(self, routes=("notify:admin",)):
        self.commands = []
        self.routes = routes
        self.nc = self

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def request(self, subject, data, timeout):
        assert subject == "dida.notify.ctl"
        if self.routes is None:
            raise TimeoutError
        return type("Msg", (), {"data": json.dumps({"routes": list(self.routes)}).encode()})()


class StubCH:
    def __init__(self):
        self.inserts = []

    async def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))


class StubPool:
    def __init__(self, breaker=0, recipients=("notify:admin",), unreachable=(), silences=None):
        self.breaker = breaker
        self.recipients = list(recipients)
        # [(device_key, name)] the adapters have declared unreachable.
        self.unreachable = list(unreachable)
        # {(key, scope): until} — None means until the alert resolves.
        self.silences = dict(silences or {})
        self.executed = []

    async def fetchval(self, q, *a):
        if "app_settings" in q:
            return json.dumps(self.recipients)
        return self.breaker  # the breaker-count query

    async def fetch(self, q, *a):
        if "alert_silences" in q:
            return [{"key": k, "scope": s, "until": u} for (k, s), u in self.silences.items()]
        if "reachable = false" in q:
            return [{"device_key": k, "nm": nm} for k, nm in self.unreachable]
        return []

    async def execute(self, q, *a):
        self.executed.append((q, a))
        if q.startswith("DELETE FROM alert_silences WHERE until IS NOT NULL"):
            now = datetime.now(UTC)
            self.silences = {k: u for k, u in self.silences.items() if u is None or u > now}
        elif q.startswith("DELETE FROM alert_silences"):
            self.silences.pop(tuple(a), None)


class StubState:
    def __init__(self, pool, bus, ch):
        self.pool, self.bus, self.ch = pool, bus, ch


class StubApp:
    def __init__(self, pool=None, bus=None, ch=None):
        self.state = StubState(pool or StubPool(), bus or StubBus(), ch)


ALL_RULES = {
    "adapter_offline": {"severity": "critical", "threshold": None, "hold_s": 0},
    "consumer_lag": {"severity": "critical", "threshold": 1000, "hold_s": 0},
    "buffer_drop": {"severity": "critical", "threshold": None, "hold_s": 0},
    "breaker_open": {"severity": "warning", "threshold": None, "hold_s": 0},
    "disk_low": {"severity": "warning", "threshold": 15, "hold_s": 0},
    "device_unreachable": {"severity": "warning", "threshold": None, "hold_s": 0},
}


def _snapshot(backlog=0, dropped=0, adapters=None):
    return {
        "engine": {"backlog": backlog, "history": {"dropped": dropped}},
        "adapters": adapters if adapters is not None else [],
        "adapter_counts": {},
        "db": {"postgres": True, "clickhouse": True},
    }


def _patch_gather(monkeypatch, snap):
    async def fake_gather(app):
        return snap() if callable(snap) else snap
    monkeypatch.setattr(alerts, "_gather", fake_gather)


def test_disk_free_pct():
    pct = _disk_free_pct("/")
    assert pct is not None and 0.0 <= pct <= 100.0, "root fs free% is a sane percentage"
    assert _disk_free_pct("/no/such/path/xyzzy") is None, "an unstat-able path yields None, not a crash"


async def test_conditions_thresholds(monkeypatch):
    # backlog over the 1000 threshold, one adapter offline + one ok, 2 tripped breakers.
    _patch_gather(monkeypatch, _snapshot(
        backlog=2000, dropped=0,
        adapters=[{"name": "mqtt", "state": "offline"}, {"name": "shelly", "state": "ok"}],
    ))
    ev = Evaluator(StubApp(pool=StubPool(breaker=2)))
    conds = {(k, s): (active, val) for k, s, active, val, _ in await ev._conditions(ALL_RULES)}

    assert conds[("consumer_lag", "")][0] is True and conds[("consumer_lag", "")][1] == 2000.0, "backlog>1000 fires"
    assert conds[("adapter_offline", "mqtt")][0] is True, "offline adapter is active"
    assert conds[("adapter_offline", "shelly")][0] is False, "ok adapter is not active"
    assert conds[("breaker_open", "")][0] is True and conds[("breaker_open", "")][1] == 2.0, "2 tripped breakers fire"


async def test_adapter_in_error_fires_like_an_offline_one(monkeypatch):
    # An adapter that answers while its backend is dead (landroid with Worx' MQTT
    # down: status polls, nothing can be commanded) went unnoticed for hours
    # because only "offline" fired. `error` is the same class of failure.
    _patch_gather(monkeypatch, _snapshot(
        adapters=[{"name": "landroid", "state": "error", "detail": "control unavailable — Worx MQTT down 900 s"}],
    ))
    ev = Evaluator(StubApp())
    conds = {(k, s): (active, msg) for k, s, active, _v, msg in await ev._conditions(ALL_RULES)}

    active, msg = conds[("adapter_offline", "landroid")]
    assert active is True, "an erroring adapter fires the alert"
    assert "Worx MQTT down" in msg, "the badge detail rides along so the push says what broke"


async def test_consumer_lag_ignores_silent_engine(monkeypatch):
    # backlog == -1 means the engine didn't answer — that's an out-of-band concern,
    # so a lag alert must NOT fire on a down engine.
    _patch_gather(monkeypatch, _snapshot(backlog=-1))
    ev = Evaluator(StubApp())
    conds = {k: active for k, s, active, v, m in await ev._conditions({"consumer_lag": ALL_RULES["consumer_lag"]})}
    assert conds["consumer_lag"] is False, "silent engine (backlog -1) does not fire a lag alert"


async def test_buffer_drop_is_edge_triggered(monkeypatch):
    rules = {"buffer_drop": ALL_RULES["buffer_drop"]}
    state = {"dropped": 5}
    _patch_gather(monkeypatch, lambda: _snapshot(dropped=state["dropped"]))
    ev = Evaluator(StubApp())

    # First tick only sets the baseline — never fires on startup.
    c1 = {k: active for k, s, active, v, m in await ev._conditions(rules)}
    assert c1["buffer_drop"] is False, "first observation sets the baseline, does not fire"

    # No new drops → still quiet.
    c2 = {k: active for k, s, active, v, m in await ev._conditions(rules)}
    assert c2["buffer_drop"] is False, "no new drops → no alert"

    # The counter grows → a fresh drop happened → fire.
    state["dropped"] = 7
    c3 = {k: active for k, s, active, v, m in await ev._conditions(rules)}
    assert c3["buffer_drop"] is True, "an increased dropped-counter fires the edge"


async def test_buffer_drop_ignores_silent_engine(monkeypatch):
    # Regression: a timed-out dida.engine.stats request (engine=None) must NOT be read
    # as dropped=0 and corrupt the baseline — that fired a false CRITICAL page on the
    # next real tick. Mirrors the silent-engine guard consumer_lag already had.
    rules = {"buffer_drop": ALL_RULES["buffer_drop"]}
    box = {"snap": _snapshot(dropped=10)}
    _patch_gather(monkeypatch, lambda: box["snap"])
    ev = Evaluator(StubApp())

    await ev._conditions(rules)  # baseline established at 10

    box["snap"] = {"engine": None, "adapters": [], "adapter_counts": {}, "db": {}}
    silent = {k: active for k, s, active, v, m in await ev._conditions(rules)}
    assert silent["buffer_drop"] is False, "a silent engine never fires buffer_drop"

    box["snap"] = _snapshot(dropped=10)  # engine returns at the SAME count
    back = {k: active for k, s, active, v, m in await ev._conditions(rules)}
    assert back["buffer_drop"] is False, "baseline survived the silent tick — no false positive"


async def test_fire_notifies_the_recipients_and_records(monkeypatch):
    _patch_gather(monkeypatch, _snapshot(backlog=5000))
    bus, ch = StubBus(), StubCH()
    ev = Evaluator(StubApp(pool=StubPool(recipients=("notify:alex", "notify:ana")), bus=bus, ch=ch))
    rules = {"consumer_lag": ALL_RULES["consumer_lag"]}  # hold_s=0 → fires at once
    monkeypatch.setattr(ev, "_rules", lambda: _async(rules))

    await ev.tick()
    assert ("consumer_lag", "") in ev._active, "the critical condition is now an active alert"
    assert [i[0] for i in ch.inserts] == ["alert_history"], "a fired row was recorded to ClickHouse"
    assert ch.inserts[0][1][0][4] == "fired", "the recorded event is 'fired'"
    assert len(bus.commands) == 2, "both recipients were notified"
    assert {c.entity_id for c in bus.commands} == {"notify:alex", "notify:ana"}, "one notify per recipient"
    assert all(c.capability == "notify" for c in bus.commands), "delivered via the notify capability"


async def test_resolve_when_condition_clears(monkeypatch):
    snap = {"backlog": 5000}
    _patch_gather(monkeypatch, lambda: _snapshot(backlog=snap["backlog"]))
    bus, ch = StubBus(), StubCH()
    ev = Evaluator(StubApp(bus=bus, ch=ch))
    rules = {"consumer_lag": ALL_RULES["consumer_lag"]}

    monkeypatch.setattr(ev, "_rules", lambda: _async(rules))
    await ev.tick()
    assert ("consumer_lag", "") in ev._active, "fired while over threshold"

    snap["backlog"] = 0  # recovered
    await ev.tick()
    assert ("consumer_lag", "") not in ev._active, "cleared once back under threshold"
    events = [i[1][0][4] for i in ch.inserts]
    assert events == ["fired", "resolved"], "recorded fired then resolved"
    assert len(bus.commands) == 2, "notified on fire AND on resolve (critical)"


async def test_hold_suppresses_flapping(monkeypatch):
    _patch_gather(monkeypatch, _snapshot(backlog=5000))
    ev = Evaluator(StubApp(bus=StubBus(), ch=StubCH()))
    held = {"consumer_lag": {"severity": "critical", "threshold": 1000, "hold_s": 999}}
    monkeypatch.setattr(ev, "_rules", lambda: _async(held))

    await ev.tick()
    assert ("consumer_lag", "") not in ev._active, "does not fire before the hold elapses"
    assert ("consumer_lag", "") in ev._pending, "but the pending timer is armed"

    # Pretend the condition has held long enough.
    ev._pending[("consumer_lag", "")] = asyncio.get_running_loop().time() - 1000
    await ev.tick()
    assert ("consumer_lag", "") in ev._active, "fires once the hold is satisfied"



async def test_device_unreachable_fires_per_device_from_the_adapter_verdict(monkeypatch):
    """The gap the Cabin incident exposed: a healthy adapter with dead devices
    behind it. One alert per unreachable device, scoped by device_key, driven by
    what the adapter stored (reachable=false) — never by a stale timestamp."""
    _patch_gather(monkeypatch, _snapshot())
    pool = StubPool(unreachable=[("kuhinja", "Kuhinja"), ("192_168_20_35", "Ema room")])
    ev = Evaluator(StubApp(pool=pool))
    conds = {(k, s): (active, msg) for k, s, active, _v, msg
             in await ev._conditions({"device_unreachable": ALL_RULES["device_unreachable"]})}
    assert conds[("device_unreachable", "kuhinja")][0] is True
    assert "Kuhinja" in conds[("device_unreachable", "kuhinja")][1]
    assert ("device_unreachable", "192_168_20_35") in conds


async def test_no_unreachable_devices_fires_nothing(monkeypatch):
    """Every device reachable (the normal house) → not one condition. A rule that
    fired on a quiet house is the false-alarm this whole design avoided."""
    _patch_gather(monkeypatch, _snapshot())
    ev = Evaluator(StubApp(pool=StubPool(unreachable=[])))
    conds = await ev._conditions({"device_unreachable": ALL_RULES["device_unreachable"]})
    assert conds == []


async def test_device_unreachable_recovers(monkeypatch):
    """The device comes back → the condition is gone → the alert resolves. A hold
    from a stale reading would leave it stuck; this reads live device state each
    tick, so recovery clears on its own."""
    _patch_gather(monkeypatch, _snapshot())
    pool = StubPool(unreachable=[("kuhinja", "Kuhinja")])
    ev = Evaluator(StubApp(pool=pool, bus=StubBus(), ch=StubCH()))
    rules = {"device_unreachable": ALL_RULES["device_unreachable"]}
    monkeypatch.setattr(ev, "_rules", lambda: _async(rules))
    await ev.tick()
    assert ("device_unreachable", "kuhinja") in ev._active
    pool.unreachable = []  # recovered
    await ev.tick()
    assert ("device_unreachable", "kuhinja") not in ev._active

async def _async(v):
    return v


async def test_a_deliberately_disabled_adapter_does_not_alarm(monkeypatch):
    """Cabin runs no tuya/unifi/volumio — their runner profiles are switched off, so
    they answer nothing and read 'offline'. That is a choice, not a failure: every
    installation would otherwise scream critical for each adapter it doesn't use."""
    _patch_gather(monkeypatch, _snapshot(adapters=[
        {"name": "tuya", "state": "offline", "enabled": False},
        {"name": "mqtt", "state": "offline", "enabled": True},
    ]))
    ev = Evaluator(StubApp(pool=StubPool()))
    conds = {(k, s): active for k, s, active, _v, _m in await ev._conditions(ALL_RULES)}
    assert conds[("adapter_offline", "tuya")] is False, "switched-off adapter is not broken"
    assert conds[("adapter_offline", "mqtt")] is True, "an enabled offline adapter still fires"


async def test_an_adapter_with_no_enabled_flag_still_alarms(monkeypatch):
    """The runner being silent must not mute the alert — with no profile info the
    snapshot omits/defaults `enabled`, and offline stays loud (fail loud)."""
    _patch_gather(monkeypatch, _snapshot(adapters=[{"name": "mqtt", "state": "offline"}]))
    ev = Evaluator(StubApp(pool=StubPool()))
    conds = {(k, s): active for k, s, active, _v, _m in await ev._conditions(ALL_RULES)}
    assert conds[("adapter_offline", "mqtt")] is True


# --- who an alert reaches, and silencing one ----------------------------------
# 13.09: "homekit down" fired a minute after the living-room sensor dropped off and
# went to the admin role — one desk account with no phone — so it reached nobody for
# 27 hours. Recipients are chosen, a list that reaches no device is itself an alert,
# and one alert can be told to stop repeating without switching its rule off.


LAG = {"consumer_lag": ALL_RULES["consumer_lag"]}
UNROUTED = {"alert_unrouted": {"severity": "critical", "threshold": None, "hold_s": 0}}


async def _unrouted(monkeypatch, recipients, routes):
    _patch_gather(monkeypatch, _snapshot())
    ev = Evaluator(StubApp(pool=StubPool(recipients=recipients), bus=StubBus(routes=routes)))
    [(_key, _scope, active, _v, msg)] = await ev._conditions(UNROUTED)
    return active, msg


async def test_recipients_with_no_device_behind_them_are_an_alert(monkeypatch):
    active, msg = await _unrouted(monkeypatch, ["notify:admin"], ["notify:marko"])
    assert active is True and "admin" in msg


async def test_one_reachable_recipient_is_enough(monkeypatch):
    active, _ = await _unrouted(monkeypatch, ["notify:admin", "notify:marko"], ["notify:marko"])
    assert active is False


async def test_no_recipient_chosen_is_an_alert(monkeypatch):
    active, msg = await _unrouted(monkeypatch, [], ["notify:marko"])
    assert active is True and "primatelja" in msg


async def test_a_silent_notify_adapter_means_nothing_gets_out(monkeypatch):
    active, msg = await _unrouted(monkeypatch, ["notify:marko"], None)
    assert active is True and "ne odgovara" in msg


async def _lagging(monkeypatch, silences):
    box = {"backlog": 5000}
    _patch_gather(monkeypatch, lambda: _snapshot(backlog=box["backlog"]))
    pool, bus, ch = StubPool(silences=silences), StubBus(), StubCH()
    ev = Evaluator(StubApp(pool=pool, bus=bus, ch=ch))
    monkeypatch.setattr(ev, "_rules", lambda: _async(LAG))
    return ev, box, pool, bus, ch


async def test_a_silenced_alert_fires_and_resolves_without_a_push(monkeypatch):
    later = datetime.now(UTC) + timedelta(hours=8)
    ev, box, pool, bus, ch = await _lagging(monkeypatch, {("consumer_lag", ""): later})
    await ev.tick()
    [active] = ev.active_list()
    assert active["silenced"] is True and active["silenced_until"] == later.isoformat()
    box["backlog"] = 0
    await ev.tick()
    assert [i[1][0][4] for i in ch.inserts] == ["fired", "resolved"], "still recorded"
    assert bus.commands == [], "but nobody was pushed"
    assert ("consumer_lag", "") in pool.silences, "a timed silence outlives a flap"


async def test_a_silence_until_resolved_ends_with_the_alert(monkeypatch):
    ev, box, pool, bus, _ = await _lagging(monkeypatch, {("consumer_lag", ""): None})
    await ev.tick()
    box["backlog"] = 0
    await ev.tick()
    assert bus.commands == []
    assert pool.silences == {}, "gone once the alert it was for is gone"
    box["backlog"] = 5000
    await ev.tick()
    assert len(bus.commands) == 1, "the next occurrence pushes again"


async def test_an_expired_silence_on_a_live_alert_says_it_is_still_going(monkeypatch):
    ev, _box, pool, bus, _ = await _lagging(monkeypatch, {("consumer_lag", ""): datetime.now(UTC) + timedelta(hours=1)})
    await ev.tick()
    assert bus.commands == []
    pool.silences[("consumer_lag", "")] = datetime.now(UTC) - timedelta(seconds=1)
    await ev.tick()
    assert [c.args["message"] for c in bus.commands] == ["⚠️ Još traje: JetStream zaostatak: 5000 poruka u redu"]
    assert pool.silences == {}
    await ev.tick()
    assert len(bus.commands) == 1, "said once, not every tick"


async def test_an_unsilenced_alert_pushes_as_before(monkeypatch):
    ev, _box, _pool, bus, _ = await _lagging(monkeypatch, {})
    await ev.tick()
    assert len(bus.commands) == 1
    assert ev.active_list()[0]["silenced"] is False
