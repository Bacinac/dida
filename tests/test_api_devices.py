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
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
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
