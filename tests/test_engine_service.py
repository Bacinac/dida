"""Integration test — the engine SERVICE, not its parts.

Every other engine suite reaches inside: it builds an `Engine` with a stub bus and
calls methods on it. That leaves `main()` — the 160 lines that wire the whole
process together — as the largest untested block in the single writer of all state.
What lives only in there is not ceremony:

  * health is CONDITIONAL on the ClickHouse history schema, so a schema that will
    not apply surfaces in the deploy gate instead of one log line at boot;
  * the removed-device tombstones are RE-READ periodically, because `forget` is a
    core-NATS at-most-once message and one lost during a reconnect would otherwise
    let the engine re-insert a device the API deleted, until an unrelated restart;
  * the shutdown order — consumers cancelled, flusher AWAITED (not merely
    cancelled), outbox drained, and the dedicated LISTEN connection released BEFORE
    `pool.close()`, because the pool waits for every acquired connection and leaving
    that one out hangs shutdown forever: a SIGKILL on every deploy.

So this boots the real process against a throwaway Postgres AND NATS, talks to it
the way an adapter does, and stops it with a real signal. It is the only test that
can fail if the service does not actually come up.

ONE boot for the whole conversation, deliberately. A fixture per test meant nine
boots of a service that opens a connection pool, a bus and two durable consumers —
a minute of wall clock, and teardowns racing each other across event loops. The
sub-assertions carry their own messages, so a single test still says what broke.
"""

import asyncio
import contextlib
import json
import os
import pathlib
import signal
import time

import pytest
from dida_core import CORE, Bus, EntityInfo, StateUpdate, pg_pool

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIDA_NATS_URL"),
    reason="needs the ephemeral NATS the runner starts for this section",
)

HEALTH = pathlib.Path("/tmp/dida_healthy_engine")


async def _wait(predicate, timeout=25.0, interval=0.1) -> bool:
    """Poll until true. Returns False on timeout so the CALLER asserts with its own
    message — a bare TimeoutError says nothing about what never happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = predicate()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return True
        await asyncio.sleep(interval)
    return False


async def _boot():
    """Start the real `main()` and wait until it says it is healthy."""
    from dida_engine.__main__ import main

    HEALTH.unlink(missing_ok=True)
    task = asyncio.create_task(main())
    up = await _wait(lambda: HEALTH.exists() or task.done())
    if task.done():                    # surface the real error, not "never healthy"
        task.result()
        raise AssertionError("the engine exited during startup")
    assert up, "the engine never reported healthy"
    return task


async def _stop(task):
    if task.done():
        return
    signal.raise_signal(signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    if not task.done():
        task.cancel()
    with contextlib.suppress(Exception):
        await task


async def test_the_engine_service_answers_the_way_an_adapter_expects():
    """Boot, project, announce, report, forget — over the real bus, in one run."""
    service = await _boot()
    bus = Bus(os.environ["DIDA_NATS_URL"], name="test-client", user=CORE)
    await bus.connect()
    pool = await pg_pool(max_size=2)
    try:
        # --- healthy for the right REASON. ClickHouse is deliberately absent here:
        # that is the connection-outage path (buffer and retry), which must not be
        # mistaken for a schema fault, and must not withhold health.
        assert HEALTH.exists()

        # --- an adapter's update reaches current_state, over the durable consumer.
        eid = "test:service:1"
        await bus.publish_state(StateUpdate(
            entity_id=eid, capability="temperature", value=21.5, adapter="test",
            ts_ns=time.time_ns()))

        async def temp():
            return await pool.fetchval(
                "SELECT value FROM current_state WHERE entity_id = $1 "
                "AND capability = 'temperature'", eid)
        assert await _wait(temp), "the update never reached current_state"
        assert json.loads(await temp()) == 21.5

        # --- and is ANNOUNCED. The relay is the only publisher: if its loop stops,
        # Postgres stays correct and every consumer goes deaf — a failure that looks
        # like nothing at all.
        seen: asyncio.Queue = asyncio.Queue()
        await bus.subscribe_events(seen.put)
        await bus.nc.flush()
        await bus.publish_state(StateUpdate(
            entity_id="test:service:2", capability="on_off", value=True,
            adapter="test", ts_ns=time.time_ns()))
        # Listening on the adapter's own subject hears the adapter, not the relay:
        # it passed with the relay dead.
        announced: list[str] = []
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and "test:service:2" not in announced:
            with contextlib.suppress(asyncio.TimeoutError):
                announced.append((await asyncio.wait_for(seen.get(), timeout=1)).entity_id)
        assert "test:service:2" in announced, \
            f"the projection was never announced on the bus; saw {announced}"

        # --- the stats reply: what the System page and any scraper read.
        #
        # POLLED, not read once. The relay publishes the moment the projection
        # commits, so the announcement above can arrive before `on_state` has
        # resumed to bump its counter — reading the counters at that instant caught
        # them mid-step and failed on a service that was working correctly.
        async def stats():
            return json.loads(
                (await bus.nc.request("dida.engine.stats", b"", timeout=10)).data)

        payload = await stats()
        for field in ("accepted", "rejected", "stale", "backlog", "outbox",
                      "published", "uptime_s", "history"):
            assert field in payload, f"stats lost `{field}`"

        async def counted_both():
            return (await stats())["accepted"] >= 2
        assert await _wait(counted_both, timeout=10), "the counters are not counting"
        payload = await stats()
        assert payload["published"] >= 1, "the relay never reported a publish"
        # Each number has its own fallback: one unavailable value must not take the
        # whole reply with it, which is exactly when it is needed.
        assert payload["backlog"] >= -1 and payload["outbox"] >= -1
        assert payload["history"]["schema_broken"] is False, \
            "an unreachable ClickHouse is an outage, not a schema fault"

        # --- an announcement registers an entity. This is a SECOND durable
        # consumer; a service that wired only the state one passes everything above.
        await bus.publish_entity(EntityInfo(
            entity_id="test:service:5", device="test:service", name="Service Test",
            device_type="sensor", capabilities=["temperature"], adapter="test"))
        assert await _wait(lambda: pool.fetchval(
            "SELECT count(*) FROM entities WHERE entity_id = $1", "test:service:5")), \
            "the announcement never registered the entity"

        # --- an EMPTY forget is not a key. A truncated message or a mis-wired
        # caller must not be read as "forget everything".
        before = await pool.fetchval("SELECT count(*) FROM current_state")
        await bus.nc.publish("dida.engine.forget", b"   ")
        await bus.nc.flush()
        await asyncio.sleep(1.5)
        assert await pool.fetchval("SELECT count(*) FROM current_state") == before, \
            "an empty forget deleted something"

        # --- a real one does remove the device.
        await bus.nc.publish("dida.engine.forget", eid.encode())
        await bus.nc.flush()
        assert await _wait(lambda: pool.fetchval(
            "SELECT count(*) = 0 FROM current_state WHERE entity_id = $1", eid)), \
            "forget did not remove the rows"

        # --- and restore lets it come back.
        await bus.nc.publish("dida.engine.restore", eid.encode())
        await bus.nc.flush()
        await asyncio.sleep(0.5)
        await bus.publish_state(StateUpdate(
            entity_id=eid, capability="temperature", value=22.0, adapter="test",
            ts_ns=time.time_ns()))
        assert await _wait(temp), "a restored device could not report again"
    finally:
        # Leave no tombstone behind: this database is shared with every other suite
        # in this section, and a stray row made an UNRELATED test fail — which is a
        # worse bug than the one being tested, because it points at the wrong file.
        with contextlib.suppress(Exception):
            await pool.execute("DELETE FROM removed_devices WHERE key = $1", eid)
        await pool.close()
        await bus.close()
        await _stop(service)


async def test_SIGTERM_shuts_the_service_down_instead_of_hanging():
    """The invariant with the sharpest edge in the file.

    `pool.close()` waits for every acquired connection, and the relay holds one
    dedicated connection for LISTEN. Release it too late — or not at all — and
    shutdown never returns: the deploy waits out its grace period and the container
    is SIGKILLed every single time, while the last log line says "shutting down".
    Invisible to any test that never stops the service."""
    service = await _boot()
    signal.raise_signal(signal.SIGTERM)
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.shield(service), timeout=30)
    except TimeoutError:
        service.cancel()
        with contextlib.suppress(Exception):
            await service
        pytest.fail("shutdown hung — this is the SIGKILL-on-every-deploy bug")
    assert time.monotonic() - started < 30
    assert service.exception() is None, f"shutdown raised: {service.exception()!r}"


async def test_a_broken_history_schema_withholds_health_without_stopping_the_house(monkeypatch):
    """The distinction the whole ClickHouse guard exists to make.

    A schema that will not apply means every historical value is being discarded.
    That used to be one warning at boot under a green container — invisible. Now the
    engine stops touching its health marker, which puts it where problems are already
    looked for: the deploy gate, `docker ps`, the System page.

    And it must do ONLY that. State projection, automations and the house keep
    running: losing graphs is not a reason to stop switching on lights."""
    from dida_engine import __main__ as engine_main

    monkeypatch.setattr(engine_main.HistoryWriter, "schema_broken",
                        property(lambda self: True))
    HEALTH.unlink(missing_ok=True)
    service = asyncio.create_task(engine_main.main())
    bus = Bus(os.environ["DIDA_NATS_URL"], name="test-unhealthy", user=CORE)
    pool = None
    try:
        # Give it well past the point a healthy engine would have reported.
        await asyncio.sleep(8)
        if service.done():
            service.result()
            pytest.fail("the engine exited instead of running unhealthy")
        assert not HEALTH.exists(), \
            "health was reported while every historical value was being discarded"

        # ...and the house still works.
        await bus.connect()
        pool = await pg_pool(max_size=2)
        eid = "test:service:broken"
        await bus.publish_state(StateUpdate(
            entity_id=eid, capability="on_off", value=True, adapter="test",
            ts_ns=time.time_ns()))
        assert await _wait(lambda: pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = $1", eid)), \
            "projection stopped — losing history must not take the house down"
    finally:
        if pool is not None:
            await pool.close()
        with contextlib.suppress(Exception):
            await bus.close()
        await _stop(service)


async def test_a_tombstone_written_by_the_api_is_picked_up_without_a_restart(monkeypatch):
    """`forget` is core-NATS: at-most-once.

    One published while the engine is mid-reconnect is simply gone, and
    `load_removed` otherwise only ran at boot — so the engine would keep
    re-inserting a device the API had deleted, until an unrelated restart. The
    periodic re-read is the recovery, and it is invisible unless something writes a
    tombstone WITHOUT sending the message."""
    from dida_engine import __main__ as engine_main

    monkeypatch.setattr(engine_main, "REMOVED_RELOAD_S", 1)
    service = await _boot()
    bus = Bus(os.environ["DIDA_NATS_URL"], name="test-tombstone", user=CORE)
    await bus.connect()
    pool = await pg_pool(max_size=2)
    eid = "test:service:tomb"
    try:
        # The API's delete path writes the tombstone row; the bus message is what
        # gets lost. Write only the row.
        await pool.execute(
            "INSERT INTO removed_devices (key) VALUES ($1) ON CONFLICT DO NOTHING",
            eid)
        # > REMOVED_RELOAD_S is not enough on its own: the loop that checks it sleeps
        # up to 5 s per turn, so the shortened interval only takes effect on the next
        # tick. Wait past a full tick.
        await asyncio.sleep(8)

        await bus.publish_state(StateUpdate(
            entity_id=eid, capability="on_off", value=True, adapter="test",
            ts_ns=time.time_ns()))
        await asyncio.sleep(2)
        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = $1", eid) == 0, \
            "a device the API deleted came back — the tombstone re-read is not running"
    finally:
        # Leave no tombstone behind: this database is shared with every other suite
        # in this section, and a stray row made an UNRELATED test fail — which is a
        # worse bug than the one being tested, because it points at the wrong file.
        with contextlib.suppress(Exception):
            await pool.execute("DELETE FROM removed_devices WHERE key = $1", eid)
        await pool.close()
        await bus.close()
        await _stop(service)
