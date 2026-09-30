"""The engine recording an adapter's reachability verdict — against a real database.

An adapter says "this device is (un)reachable"; the engine writes that to the
device row so every entity of it inherits the answer, and journals the transition
so "when did this stop reporting" has an answer. The value of doing it here rather
than with a stub is the SQL itself: the CTE that captures the previous value to
detect a change, the CASE that stamps reachable_since only on a real transition,
and the guard that drops a verdict for a device that does not exist. A stub would
prove the method calls itself.
"""

from __future__ import annotations

from dida_core import (
    Bus,
    EntityInfo,
    ReachabilityEvent,
    StateUpdate,
    apply_migrations,
    jsonb_init,
    pg_pool,
)
from dida_engine.__main__ import Engine


class StubBus:
    encode_event = staticmethod(Bus.encode_event)

    def __init__(self) -> None:
        self.journals: list = []

    async def publish_raw(self, subject, payload):
        pass

    async def flush(self, timeout=5.0):
        pass

    async def publish_journal(self, event):
        self.journals.append(event)


class StubHistory:
    def enqueue(self, *a):
        pass


async def _fresh():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    for sql in (
        "DELETE FROM current_state WHERE entity_id LIKE 'rch:%'",
        "DELETE FROM entities WHERE entity_id LIKE 'rch:%'",
        "DELETE FROM current_state WHERE entity_id LIKE 'rch2:%' OR entity_id LIKE 'presence:rch%'",
        "DELETE FROM entities WHERE entity_id LIKE 'rch2:%' OR entity_id LIKE 'presence:rch%'",
        "DELETE FROM devices WHERE device_key IN ('rch_dev', 'rch_ghost')",
    ):
        await pool.execute(sql)
    return pool


async def _announce(engine, dev="rch_dev"):
    """Create the device + entity rows the way a real adapter does."""
    await engine.on_entity_info(EntityInfo(
        entity_id=f"rch:{dev}", adapter="rch", capabilities=["on_off"], name=dev,
        device=dev, device_name=dev, device_type="switch"))


def _kinds(bus):
    return [e.kind for e in bus.journals]


async def test_a_device_starts_reachable():
    pool = await _fresh()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine)
        assert await pool.fetchval(
            "SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is True
    finally:
        await pool.close()


async def test_an_unreachable_verdict_is_recorded_and_journalled():
    pool = await _fresh()
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    try:
        await _announce(engine)
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=1, device_key="rch_dev", adapter="rch", reachable=False, detail="MQTT offline"))
        assert await pool.fetchval(
            "SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is False
        assert "offline" in _kinds(bus), "the transition must be journalled"
        msg = next(e.message for e in bus.journals if e.kind == "offline")
        assert "MQTT offline" in msg, "the reason rides along to the timeline"
    finally:
        await pool.close()


async def test_reachable_since_moves_only_on_a_change():
    """The alert's hold and the floor plan's 'down for 20 min' read this stamp, so a
    repeated verdict must NOT reset it — otherwise a device that flaps its status
    text (without changing reachable) looks freshly down every time."""
    pool = await _fresh()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine)
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=1, device_key="rch_dev", adapter="rch", reachable=False))
        first = await pool.fetchval(
            "SELECT reachable_since FROM devices WHERE device_key = 'rch_dev'")
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=2, device_key="rch_dev", adapter="rch", reachable=False))
        again = await pool.fetchval(
            "SELECT reachable_since FROM devices WHERE device_key = 'rch_dev'")
        assert first == again, "reachable_since was bumped without a change"
    finally:
        await pool.close()


async def test_a_repeated_verdict_does_not_re_journal():
    """Edge-triggered at the engine too: the adapter is edge-triggered, but a
    redelivery or a second adapter must not double the timeline."""
    pool = await _fresh()
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    try:
        await _announce(engine)
        for _ in range(3):
            await engine.on_reachability(ReachabilityEvent(
                ts_ns=1, device_key="rch_dev", adapter="rch", reachable=False))
        assert _kinds(bus).count("offline") == 1
    finally:
        await pool.close()


async def test_recovery_is_recorded_and_journalled():
    pool = await _fresh()
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    try:
        await _announce(engine)
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=1, device_key="rch_dev", adapter="rch", reachable=False))
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=2, device_key="rch_dev", adapter="rch", reachable=True))
        assert await pool.fetchval(
            "SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is True
        assert _kinds(bus) == ["offline", "online"]
    finally:
        await pool.close()


async def test_a_verdict_for_an_unknown_device_is_dropped():
    """The device row is created by state/announce. A reachability ping for a device
    the engine has never seen must not conjure an adapter-less, entity-less ghost."""
    pool = await _fresh()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=1, device_key="rch_ghost", adapter="rch", reachable=False))
        assert await pool.fetchval(
            "SELECT count(*) FROM devices WHERE device_key = 'rch_ghost'") == 0
    finally:
        await pool.close()


async def test_a_verdict_for_a_removed_device_is_ignored():
    pool = await _fresh()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine)
        engine._removed.add("rch_dev")
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=1, device_key="rch_dev", adapter="rch", reachable=False))
        # Untouched: still reachable, because the verdict was dropped before the write.
        assert await pool.fetchval(
            "SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is True
    finally:
        await pool.close()


async def _fresh_liveness(pool):
    await pool.execute("DELETE FROM adapter_liveness WHERE adapter = 'rch'")


async def test_a_silent_adapter_takes_its_devices_down():
    from dida_engine.__main__ import ADAPTER_SILENCE_S

    pool = await _fresh()
    await _fresh_liveness(pool)
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    await _announce(engine)
    await engine.on_heartbeat("rch")
    assert await pool.fetchval("SELECT alive FROM adapter_liveness WHERE adapter = 'rch'") is True
    assert _kinds(bus) == []  # a first heartbeat is not news

    engine._started -= ADAPTER_SILENCE_S + 1
    engine._beats["rch"] -= ADAPTER_SILENCE_S + 1
    await engine.judge_liveness()
    assert await pool.fetchval("SELECT alive FROM adapter_liveness WHERE adapter = 'rch'") is False
    # Other tests' adapters share the database and fall silent in the same sweep.
    assert [e.kind for e in bus.journals if e.device_key == "rch_dev"] == ["offline"]

    await engine.on_heartbeat("rch")
    assert await pool.fetchval("SELECT alive FROM adapter_liveness WHERE adapter = 'rch'") is True
    assert [e.kind for e in bus.journals if e.device_key == "rch_dev"] == ["offline", "online"]
    await pool.close()


async def test_an_engine_restart_is_not_a_silence():
    pool = await _fresh()
    await _fresh_liveness(pool)
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    await _announce(engine)
    await engine.judge_liveness()  # no heartbeat yet, but the engine only just started
    assert await pool.fetchval("SELECT alive FROM adapter_liveness WHERE adapter = 'rch'") is None
    await pool.close()


async def test_bus_heartbeats_for_every_namespace_it_speaks_for():
    import asyncio

    from dida_core import Bus, ReachabilityEvent, StateUpdate

    sent: list = []

    class NC:
        async def publish(self, subject, payload):
            sent.append((subject, payload))

    bus = Bus("nats://x", name="t", user="t")
    bus._nc = NC()
    await bus.publish_entity(EntityInfo(entity_id="a:1", adapter="frigate", capabilities=[]))
    await bus.publish_entity(EntityInfo(entity_id="b:1", adapter="frigate-seaside", capabilities=[]))
    await bus.publish_state(StateUpdate(entity_id="c:1", capability="on_off", value=True,
                                        adapter="shelly", ts_ns=1))
    await bus.publish_reachability(ReachabilityEvent(ts_ns=1, device_key="d", adapter="heos",
                                                     reachable=True))
    await asyncio.sleep(0)
    beats = [(s, p) for s, p in sent if s.startswith("dida.heartbeat.")]
    assert beats == [(f"dida.heartbeat.{a}", b"")
                     for a in ("frigate", "frigate-seaside", "heos", "shelly")], \
        "one empty beat per namespace, the name in the subject the server grants it"
    bus._nc = None
    await bus.close()


async def test_an_adapter_cannot_write_into_another_namespace():
    """The server vouches for who sent a fact, not for whose entity it names.
    Taken at its word, a confused adapter overwrote another's entity and moved
    it into its own column."""
    pool = await _fresh()
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    try:
        await _announce(engine)
        await engine.on_state(StateUpdate(entity_id="rch:rch_dev", capability="on_off",
                                          value=True, adapter="rch2", ts_ns=1))
        await engine.on_entity_info(EntityInfo(entity_id="rch:rch_dev", adapter="rch2",
                                               capabilities=["on_off"], name="stolen"))
        row = await pool.fetchrow("SELECT adapter, name FROM entities WHERE entity_id = 'rch:rch_dev'")
        assert (row["adapter"], row["name"]) == ("rch", "rch_dev")
        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = 'rch:rch_dev'") == 0
        assert engine._rejected == 2 and "validation_rejected" in _kinds(bus)
    finally:
        await pool.close()


async def test_presence_is_written_by_gps_and_by_wifi():
    pool = await _fresh()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        for ts, adapter in ((1, "presence"), (2, "unifi")):
            await engine.on_state(StateUpdate(entity_id="presence:rch", capability="location",
                                              value="home", adapter=adapter, ts_ns=ts))
        assert engine._rejected == 0 and engine._accepted == 2
    finally:
        await pool.close()


async def test_grouping_into_a_device_does_not_take_it_over():
    """heos files its player under the AVR the denon adapter registered. The row
    used to follow whoever wrote last, so the AVR's reachability and its adapter's
    silence alternated between two owners."""
    pool = await _fresh()
    bus = StubBus()
    engine = Engine(bus, pool, StubHistory())
    try:
        await _announce(engine)
        await engine.on_entity_info(EntityInfo(entity_id="rch2:player", adapter="rch2",
                                               capabilities=["on_off"], name="player",
                                               device="rch_dev", native_key="other"))
        await engine.on_state(StateUpdate(entity_id="rch2:player", capability="on_off",
                                          value=True, adapter="rch2", ts_ns=1, device="rch_dev"))
        row = await pool.fetchrow("SELECT adapter, native_key FROM devices WHERE device_key = 'rch_dev'")
        assert row["adapter"] == "rch" and row["native_key"] != "other"

        await engine.on_reachability(ReachabilityEvent(
            ts_ns=2, device_key="rch_dev", adapter="rch2", reachable=False))
        assert await pool.fetchval("SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is True
        await engine.on_reachability(ReachabilityEvent(
            ts_ns=3, device_key="rch_dev", adapter="rch", reachable=False))
        assert await pool.fetchval("SELECT reachable FROM devices WHERE device_key = 'rch_dev'") is False
    finally:
        await pool.close()
