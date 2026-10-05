"""Integration test — engine projection (validate → project → current_state) and
the ts_ns ORDERING/idempotency guard, the core correctness claim that was
untested because it needs a real database.

This is the FIRST infra-backed test: the runner spins up a throwaway Postgres and
wires POSTGRES_* at it (see tests/run.sh "integration: engine projection"); every
other suite stays zero-infra. Uses the real Engine + a real asyncpg pool, with a
stub bus + stub history (we assert on projection + the accept/stale/reject
counters, not on delivery).
"""
import asyncio
import contextlib
import json
import time

import msgspec
from dida_core import Bus, StateUpdate, apply_migrations, jsonb_init, pg_pool
from dida_engine.__main__ import Engine, OutboxRelay

_decode = msgspec.msgpack.Decoder(StateUpdate).decode


class StubBus:
    """Stands in for the bus at the two points the engine now touches it: encoding an
    event for the outbox, and (via OutboxRelay) publishing raw payloads + flushing."""

    def __init__(self, fail_publish=False, fail_flush=False):
        self.published = []          # (subject, payload) actually handed to the bus
        self._fail_publish = fail_publish
        self._fail_flush = fail_flush

    encode_event = staticmethod(Bus.encode_event)  # the REAL encoder — payload fidelity matters

    async def publish_raw(self, subject, payload):
        if self._fail_publish:
            raise RuntimeError("bus down")
        self.published.append((subject, payload))

    async def flush(self, timeout=5.0):
        if self._fail_flush:
            raise RuntimeError("flush timed out")

    async def publish_event(self, ev):  # legacy path — must no longer be used
        raise AssertionError("engine must publish via the outbox, never inline")


class StubHistory:
    def __init__(self):
        self.rows = []

    def enqueue(self, *a):
        self.rows.append(a)


def upd(cap, value, ts):
    return StateUpdate(entity_id="test:sensor:1", capability=cap, value=value, adapter="test", ts_ns=ts)


async def test_next_occurrence_clear_replaces_the_persisted_date_and_is_published():
    pool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    eid = "test:calendar:1"
    try:
        await pool.execute("DELETE FROM current_state WHERE entity_id=$1", eid)
        bus, history = StubBus(), StubHistory()
        engine = Engine(bus, pool, history)
        for value, stamp in [("2026-10-05", 1000), ("", 2000)]:
            await engine.on_state(StateUpdate(entity_id=eid, capability="next_occurrence", value=value,
                                              adapter="test", ts_ns=stamp))
        assert engine._accepted == 2 and engine._rejected == 0
        stored = await pool.fetchval("SELECT value FROM current_state WHERE entity_id=$1 AND capability='next_occurrence'", eid)
        assert json.loads(stored) == ""
        queued = await pool.fetch("SELECT payload FROM state_outbox")
        updates = [_decode(bytes(row["payload"])) for row in queued]
        assert [update.value for update in updates if update.entity_id == eid] == ["2026-10-05", ""]
        assert [row[3] for row in history.rows] == ["2026-10-05", ""]
    finally:
        await pool.close()


async def test_engine_projection_and_ts_ns_idempotency():
    pool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:sensor:1'")  # idempotent re-runs
    await pool.execute("DELETE FROM state_outbox")

    bus, hist = StubBus(), StubHistory()
    eng = Engine(bus, pool, hist)

    async def queued():
        return await pool.fetchval("SELECT count(*) FROM state_outbox")

    async def row():
        return await pool.fetchrow(
            "SELECT value, ts_ns FROM current_state WHERE entity_id='test:sensor:1' AND capability='temperature'"
        )

    # 1) a valid update is validated, projected, published, and historised
    await eng.on_state(upd("temperature", 21.5, 1000))
    r = await row()
    assert r is not None and float(r["value"]) == 21.5 and r["ts_ns"] == 1000, "valid update projected to current_state"
    assert eng._accepted == 1 and await queued() == 1 and len(hist.rows) == 1, "accepted → one queued event + one history point"

    # 2) EXACT redelivery (same ts_ns) is a no-op — the '<' guard: equal
    #    ts_ns can only be a JetStream at-least-once duplicate, never a new value
    await eng.on_state(upd("temperature", 21.5, 1000))
    assert eng._stale == 1, "exact redelivery counted stale, not accepted"
    assert await queued() == 1 and len(hist.rows) == 1, "redelivery queues NO event and no history point (no phantom transition / double count)"

    # 3) an out-of-order OLDER value cannot regress the stored row
    await eng.on_state(upd("temperature", 99.0, 500))
    r = await row()
    assert eng._stale == 2 and float(r["value"]) == 21.5 and r["ts_ns"] == 1000, "older redelivery dropped, row unchanged"

    # 4) a strictly NEWER value is applied
    await eng.on_state(upd("temperature", 22.0, 2000))
    r = await row()
    assert float(r["value"]) == 22.0 and r["ts_ns"] == 2000, "newer value applied"
    assert eng._accepted == 2 and await queued() == 2, "second accept → second queued event"

    # 5) the boundary rejects an invalid value — it never reaches current_state
    await eng.on_state(upd("temperature", "hot", 3000))
    r = await row()
    assert eng._rejected == 1, "type-invalid value rejected at the capability boundary"
    assert float(r["value"]) == 22.0 and r["ts_ns"] == 2000, "rejected update did NOT corrupt current_state"
    assert await queued() == 2, "rejected update queued no event"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:sensor:1'")
    await pool.execute("DELETE FROM state_outbox")
    await pool.close()


async def test_engine_rejects_oversized_metadata():
    # validate_state guards `value`; the sibling metadata strings (name/unit/…) land
    # in unbounded TEXT columns, so a buggy adapter streaming a multi-MB name must be
    # rejected at the boundary too — never reach the DB.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:meta:1'")
    eng = Engine(StubBus(), pool, StubHistory())

    huge = "x" * 600  # > _MAX_META (512)
    await eng.on_state(StateUpdate(
        entity_id="test:meta:1", capability="temperature", value=21.0, adapter="test",
        ts_ns=1000, name=huge,
    ))
    assert eng._rejected == 1, "an oversized metadata string is rejected at the boundary"
    r = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id = 'test:meta:1'")
    assert r == 0, "the rejected update never reached current_state"

    # a normal-sized name still projects fine
    await eng.on_state(StateUpdate(
        entity_id="test:meta:1", capability="temperature", value=21.0, adapter="test",
        ts_ns=2000, name="Kitchen sensor",
    ))
    r = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id = 'test:meta:1'")
    assert r == 1, "a normal metadata string projects normally"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:meta:1'")
    await pool.close()


async def test_engine_rejects_far_future_ts_ns():
    # A host whose clock is far in the future would poison current_state.ts_ns and
    # wedge the entity (every later, correct update looks stale). Reject it at the
    # boundary; a normal (near-now) ts_ns still projects.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:skew:1'")
    eng = Engine(StubBus(), pool, StubHistory())

    future = time.time_ns() + 10 * 60 * 1_000_000_000  # 10 min ahead → > the 5 min bound
    await eng.on_state(StateUpdate(
        entity_id="test:skew:1", capability="temperature", value=21.0, adapter="test", ts_ns=future,
    ))
    assert eng._rejected == 1, "a far-future ts_ns is rejected at the boundary"
    r = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id = 'test:skew:1'")
    assert r == 0, "the skewed update never reached current_state (no wedge)"

    await eng.on_state(StateUpdate(
        entity_id="test:skew:1", capability="temperature", value=21.0, adapter="test", ts_ns=time.time_ns(),
    ))
    r = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id = 'test:skew:1'")
    assert r == 1, "a near-now ts_ns projects normally"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:skew:1'")
    await pool.close()


async def test_state_and_its_announcement_commit_together():
    # THE outbox guarantee: a value in current_state always has a durable instruction
    # to announce it. Same transaction, so there is no window where one exists alone.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:1'")
    await pool.execute("DELETE FROM state_outbox")

    eng = Engine(StubBus(), pool, StubHistory())
    await eng.on_state(StateUpdate(
        entity_id="test:pub:1", capability="temperature", value=20.0, adapter="test", ts_ns=1000,
    ))

    rows = await pool.fetch("SELECT payload FROM state_outbox")
    assert len(rows) == 1, "the projected update queued exactly one announcement"
    ev = _decode(rows[0]["payload"])
    assert (ev.entity_id, ev.capability, ev.value) == ("test:pub:1", "temperature", 20.0), \
        "the queued payload is the validated event, ready to publish verbatim"

    await pool.execute("DELETE FROM state_outbox")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:1'")
    await pool.close()


async def test_a_publish_that_never_happened_is_not_lost():
    # The old silent drop: the bus is down when the row commits. The ts_ns guard means
    # JetStream redelivery can never re-announce it, so before the outbox this
    # transition was gone for good. Now it simply waits its turn.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:2'")
    await pool.execute("DELETE FROM state_outbox")

    down = StubBus(fail_publish=True)
    eng = Engine(down, pool, StubHistory())
    await eng.on_state(StateUpdate(
        entity_id="test:pub:2", capability="temperature", value=21.0, adapter="test", ts_ns=1000,
    ))
    relay = OutboxRelay(down, pool)
    with contextlib.suppress(RuntimeError):  # bus down; run() logs + retries — row must survive
        await relay.drain_once()
    assert await pool.fetchval("SELECT count(*) FROM state_outbox") == 1, \
        "a failed publish leaves the event queued — not dropped"

    # The bus comes back: the SAME event is announced, nothing re-projected.
    up = StubBus()
    assert await OutboxRelay(up, pool).drain_once() == 1
    assert await pool.fetchval("SELECT count(*) FROM state_outbox") == 0, "drained once published"
    assert _decode(up.published[0][1]).value == 21.0, "the event survived the outage intact"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:2'")
    await pool.close()


async def test_a_relay_that_cannot_drain_is_reported_stuck():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:5'")
    await pool.execute("DELETE FROM state_outbox")

    down = StubBus(fail_publish=True)
    await Engine(down, pool, StubHistory()).on_state(StateUpdate(
        entity_id="test:pub:5", capability="temperature", value=20.0, adapter="test", ts_ns=1000,
    ))
    relay = OutboxRelay(down, pool)
    relay._drained_at -= 31
    with contextlib.suppress(RuntimeError):
        await relay.drain_once()
    assert relay.stuck(), "a relay whose drains keep failing takes the engine's health with it"

    relay._bus = StubBus()
    assert await relay.drain_once() == 1 and not relay.stuck(), "one completed drain clears it"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:5'")
    await pool.close()


async def test_rows_are_kept_until_the_bus_confirms_the_flush():
    # publish() only buffers in the NATS client. Deleting on publish alone would
    # reintroduce the drop on a crash, so the delete must wait for the flush.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:3'")
    await pool.execute("DELETE FROM state_outbox")

    eng = Engine(StubBus(), pool, StubHistory())
    await eng.on_state(StateUpdate(
        entity_id="test:pub:3", capability="temperature", value=22.0, adapter="test", ts_ns=1000,
    ))
    unflushed = StubBus(fail_flush=True)
    with contextlib.suppress(RuntimeError):
        await OutboxRelay(unflushed, pool).drain_once()
    assert await pool.fetchval("SELECT count(*) FROM state_outbox") == 1, \
        "published but not flushed is NOT delivered — the row stays until it is"

    await pool.execute("DELETE FROM state_outbox")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:3'")
    await pool.close()


async def test_a_duplicate_redelivery_queues_no_second_announcement():
    # The ts_ns guard already makes redelivery a no-op for the projection; it must
    # stay one for the announcement too, or a replayed message double-fires a
    # momentary BUTTON automation.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:4'")
    await pool.execute("DELETE FROM state_outbox")

    eng = Engine(StubBus(), pool, StubHistory())
    same = StateUpdate(entity_id="test:pub:4", capability="temperature", value=23.0,
                       adapter="test", ts_ns=1000)
    await eng.on_state(same)
    await eng.on_state(same)  # JetStream at-least-once replay
    assert await pool.fetchval("SELECT count(*) FROM state_outbox") == 1, \
        "the replay projected nothing, so it announces nothing"
    assert eng._stale == 1, "and it is counted as the duplicate it is"

    await pool.execute("DELETE FROM state_outbox")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:4'")
    await pool.close()


async def test_drain_publishes_in_projection_order():
    # The engine's state consumer is sequential, so id order IS projection order —
    # replaying it out of order would fire transitions against stale values.
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:5'")
    await pool.execute("DELETE FROM state_outbox")

    bus = StubBus()
    eng = Engine(bus, pool, StubHistory())
    for i, temp in enumerate((10.0, 11.0, 12.0), start=1):
        await eng.on_state(StateUpdate(entity_id="test:pub:5", capability="temperature",
                                       value=temp, adapter="test", ts_ns=1000 + i))
    assert await OutboxRelay(bus, pool).drain_once() == 3
    assert [_decode(p).value for _, p in bus.published] == [10.0, 11.0, 12.0], \
        "announced in the order they were projected"

    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:5'")
    await pool.close()


async def test_a_failed_drain_round_does_not_kill_the_relay():
    """The relay is the ONLY publisher of engine events.

    If one bad round could end its loop, the rows would stay committed and correct
    in Postgres while every consumer went deaf — no error after the first, no
    unhealthy container, just a house that stopped reacting. So a round that raises
    must be logged and retried, and only cancellation may end the loop."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:9'")
    await pool.execute("DELETE FROM state_outbox")

    bus = StubBus(fail_publish=True)
    eng = Engine(bus, pool, StubHistory())
    await eng.on_state(StateUpdate(entity_id="test:pub:9", capability="temperature",
                                   value=13.0, adapter="test", ts_ns=2000))

    relay = OutboxRelay(bus, pool)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop))
    await asyncio.sleep(0.3)
    assert not task.done(), "one failed round ended the only publisher"
    assert await relay.backlog() == 1, "a failed publish must leave the row queued"

    # Recover: the same relay, once the bus is back, announces what it was holding.
    bus._fail_publish = False
    relay.wake()
    await asyncio.sleep(0.3)
    assert await relay.backlog() == 0, "the relay never retried after the bus came back"
    assert [s for s, _ in bus.published], "nothing was announced after recovery"

    stop.set()
    relay.wake()
    await asyncio.wait_for(task, timeout=5)
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'test:pub:9'")
    await pool.close()
