"""Integration test — the device-level routes (dida_api.devices) end to end
against a real Postgres: list, label edit (upserts the `devices` row), room +
device_type edit (writes through to every entity sharing the device_key), and
uniform remove (blocks the key in removed_devices, tells the engine to forget
it, then deletes state/entities/devices), and restore (lifts the block without
re-creating anything). All admin except the list.

A "device" here is the `devices` row (device_key/adapter/name/label) grouped
with one or more `entities` rows sharing that device_key — the engine creates
both normally; this test seeds them directly. delete_device publishes to
`bus.nc.publish` (the raw NATS client), not `bus.publish_command/_event`, so
the stub bus carries a stub `.nc` too.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import asyncio

import asyncpg
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from dida_core.db import DELETE_DEVICE_ENTITIES
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class _StubNC:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class StubBus:
    def __init__(self):
        self.nc = _StubNC()

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _cleanup(pool):
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%' OR device_key = 'zztest:device'")
    await pool.execute("DELETE FROM devices WHERE device_key = 'zztest:device'")
    await pool.execute("DELETE FROM removed_devices WHERE key = 'zztest:device'")
    await pool.execute("DELETE FROM areas WHERE name = 'zztest_room'")
    await pool.execute("DELETE FROM users WHERE username = 'devsadmin'")


async def test_devices_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('devsadmin', $1, 'admin')", await hash_password("adminpw12")
    )
    area_id = await pool.fetchval("INSERT INTO areas (name, kind) VALUES ('zztest_room', 'other') RETURNING id")
    # A grouped device: a `devices` row (as the engine would create it) plus one
    # entity carrying its device_key, so the adapter/area/type lookups resolve.
    await pool.execute("INSERT INTO devices (device_key, adapter, name) VALUES ('zztest:device', 'mqtt', 'Auto Lamp')")
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, device_key) VALUES ('zztest:device:sw', 'mqtt', 'zztest:device')"
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "devices-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "devsadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # list shows the seeded device with its auto name, no label yet
        r = await c.get("/devices")
        assert r.status_code == 200
        row = next(d for d in r.json() if d["device_key"] == "zztest:device")
        assert row["adapter"] == "mqtt" and row["name"] == "Auto Lamp" and row["label"] is None

        # label edit → upserts devices.label; the auto name is untouched
        r = await c.patch("/devices/zztest:device", json={"name": "My Lamp"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        r = await c.get("/devices")
        row = next(d for d in r.json() if d["device_key"] == "zztest:device")
        assert row["label"] == "My Lamp", "label persisted"
        assert row["name"] == "Auto Lamp", "auto-detected name is untouched by a label edit"

        # room edit → writes through to every entity sharing the device_key
        r = await c.patch("/devices/zztest:device", json={"area_id": area_id})
        assert r.status_code == 200
        got = await pool.fetchval("SELECT area_id FROM entities WHERE entity_id = 'zztest:device:sw'")
        assert got == area_id, "room persisted on the device's entity"

        # device_type edit → same fan-out, validated against the canonical vocabulary
        r = await c.patch("/devices/zztest:device", json={"device_type": "light"})
        assert r.status_code == 200
        got = await pool.fetchval("SELECT device_type FROM entities WHERE entity_id = 'zztest:device:sw'")
        assert got == "light", "device_type persisted"

        r = await c.patch("/devices/zztest:device", json={"device_type": "not-a-real-type"})
        assert r.status_code == 400, "invalid device_type is rejected"

        # a no-op patch (no recognised fields set) still 200s — devices.py has no
        # "nothing to update" guard, unlike floors.py
        r = await c.patch("/devices/zztest:device", json={})
        assert r.status_code == 200 and r.json() == {"ok": True}

        # a label edit on a key with no matching entity/device 404s (adapter lookup fails)
        r = await c.patch("/devices/zztest:ghost", json={"name": "x"})
        assert r.status_code == 404, "labelling an unknown device is 404"

        # remove: blocks the key, tells the engine to forget it, deletes entities + devices
        r = await c.delete("/devices/zztest:device")
        assert r.status_code == 204
        blocked = await pool.fetchval("SELECT adapter FROM removed_devices WHERE key = 'zztest:device'")
        assert blocked == "mqtt", "the key is blocked on the engine with its adapter"
        remaining = await pool.fetchval("SELECT count(*) FROM entities WHERE device_key = 'zztest:device'")
        assert remaining == 0, "its entities are gone"
        r = await c.get("/devices")
        assert not any(d["device_key"] == "zztest:device" for d in r.json()), "removed device is gone from the list"

        # restore: the block list is the only place a removed device is still
        # visible, and lifting it must both drop the row and tell the engine —
        # whose copy of the block is in memory.
        r = await c.get("/devices/removed")
        assert r.status_code == 200
        assert [d["key"] for d in r.json() if d["key"] == "zztest:device"] == ["zztest:device"]

        r = await c.delete("/devices/removed/zztest:device")
        assert r.status_code == 204
        assert appmod.app.state.bus.nc.published[-1] == ("dida.engine.restore", b"zztest:device"), \
            "the engine is told, or the restore only takes effect at its next restart"
        assert await pool.fetchval(
            "SELECT count(*) FROM removed_devices WHERE key = 'zztest:device'"
        ) == 0, "the tombstone is gone"
        assert not any(d["key"] == "zztest:device" for d in (await c.get("/devices/removed")).json())

        # Nothing is re-created here: the device comes back only when its adapter
        # announces it again. Asserting that keeps a future "helpful" resurrect
        # from sneaking in — it would fabricate a device the adapter may not have.
        assert await pool.fetchval(
            "SELECT count(*) FROM entities WHERE device_key = 'zztest:device'"
        ) == 0, "restore lifts the block, it does not invent the device"

        r = await c.delete("/devices/removed/zztest:device")
        assert r.status_code == 404, "restoring something that isn't blocked is 404"

    await _cleanup(pool)
    await pool.close()


# --- remove vs the engine's forget: both delete the same rows at once -------------

RACE = "zztest:race"
_PLAIN_ENTITIES = "DELETE FROM entities WHERE device_key = $1 OR entity_id = $1"
_STATE_FIRST = (
    "DELETE FROM current_state WHERE entity_id IN "
    "(SELECT entity_id FROM entities WHERE device_key = $1 OR entity_id = $1)"
)


async def _seed_race(pool):
    await pool.execute("DELETE FROM entities WHERE device_key = $1", RACE)
    await pool.execute("DELETE FROM devices WHERE device_key = $1", RACE)
    await pool.execute("INSERT INTO devices (device_key, adapter, name) VALUES ($1, 'cast', 'Race')", RACE)
    for eid in (f"{RACE}:a", f"{RACE}:b"):
        await pool.execute("INSERT INTO entities (entity_id, adapter, device_key) VALUES ($1, 'cast', $2)", eid, RACE)
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value) VALUES ($1, 'on_off', 'true')", eid)


async def _wait_until_blocked(pool, pid):
    for _ in range(300):
        if await pool.fetchval("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the engine's delete never blocked, so the interleaving was not reproduced")


async def _race(pool, api_steps, engine_sql):
    """The api's remove transaction takes its first lock, the engine's forget starts
    and blocks behind it, then the api carries on: the interleaving that deadlocked
    on production (13. 10. 2026)."""
    await _seed_race(pool)
    errors = []
    async with pool.acquire() as api, pool.acquire() as engine:
        pid = await engine.fetchval("SELECT pg_backend_pid()")
        forget = None
        try:
            async with api.transaction():
                await api.execute(api_steps[0], RACE)
                forget = asyncio.create_task(engine.execute(engine_sql, RACE))
                await _wait_until_blocked(pool, pid)
                for step in api_steps[1:]:
                    await api.execute(step, RACE)
                await api.execute("DELETE FROM devices WHERE device_key = $1", RACE)
        except asyncpg.exceptions.DeadlockDetectedError as exc:
            errors.append(exc)
        if forget is not None:
            try:
                await forget
            except asyncpg.exceptions.DeadlockDetectedError as exc:
                errors.append(exc)
    return errors


async def _race_pool():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    return pool


async def test_the_old_remove_order_deadlocks_against_forget():
    """Proves the rig reproduces the production interleaving; without it the test
    below could pass by never racing at all."""
    pool = await _race_pool()
    try:
        errors = await _race(pool, [_STATE_FIRST, _PLAIN_ENTITIES], _PLAIN_ENTITIES)
        assert len(errors) == 1, "state-first locking must deadlock against the engine's entity delete"
    finally:
        await pool.execute("DELETE FROM entities WHERE device_key = $1", RACE)
        await pool.execute("DELETE FROM devices WHERE device_key = $1", RACE)
        await pool.close()


async def test_remove_and_forget_together_cannot_deadlock():
    pool = await _race_pool()
    try:
        errors = await _race(pool, [DELETE_DEVICE_ENTITIES], DELETE_DEVICE_ENTITIES)
        assert errors == []
        assert await pool.fetchval("SELECT count(*) FROM entities WHERE device_key = $1", RACE) == 0
        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id LIKE $1", f"{RACE}:%") == 0, \
            "current_state goes with its entity through the cascade"
    finally:
        await pool.execute("DELETE FROM entities WHERE device_key = $1", RACE)
        await pool.execute("DELETE FROM devices WHERE device_key = $1", RACE)
        await pool.close()
