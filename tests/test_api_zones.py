"""Integration test — the geofence-zone routes (dida_api.zones) end to end
against a real Postgres: list (any authenticated user), admin create/patch/
delete, the "nothing to update" / not-found edge cases, and the single-home-
zone invariant (creating/patching a second `is_home` zone unsets the first).

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_zones_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM zones WHERE name LIKE 'zztest_zone%'")
    await pool.execute("DELETE FROM users WHERE username = 'zoneadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('zoneadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "zones-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "zoneadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # create → 201, echoes the full zone row
        body = {"name": "zztest_zone1", "latitude": 43.5, "longitude": 16.25, "radius_m": 50, "is_home": False}
        r = await c.post("/zones", json=body)
        assert r.status_code == 201, "admin creates a zone"
        z1 = r.json()
        zid1 = z1["id"]
        assert z1["name"] == "zztest_zone1", "create echoes the name"
        assert z1["latitude"] == 43.5 and z1["longitude"] == 16.25, "create echoes the coordinates"
        assert z1["radius_m"] == 50 and z1["is_home"] is False, "create echoes radius + is_home"

        # list contains the new zone (round-trip)
        r = await c.get("/zones")
        assert r.status_code == 200
        assert any(z["id"] == zid1 and z["name"] == "zztest_zone1" for z in r.json()), "new zone appears in the list"

        # patch: rename + resize → 200, reflected on GET
        r = await c.patch(f"/zones/{zid1}", json={"name": "zztest_zone1_renamed", "radius_m": 250})
        assert r.status_code == 200
        assert r.json()["name"] == "zztest_zone1_renamed" and r.json()["radius_m"] == 250, "patch echoes the update"
        r = await c.get("/zones")
        renamed = next(z for z in r.json() if z["id"] == zid1)
        assert renamed["name"] == "zztest_zone1_renamed" and renamed["radius_m"] == 250, "patch persisted"

        # patch with an empty body → 400
        r = await c.patch(f"/zones/{zid1}", json={})
        assert r.status_code == 400, "empty patch body is rejected"

        # patch a non-existent zone → 404
        r = await c.patch("/zones/999999999", json={"name": "ghost"})
        assert r.status_code == 404, "patching a non-existent zone is 404"

        # single-home-zone invariant: flagging zone1 home, then creating a second
        # home zone must unset zone1's flag (only one `is_home` zone at a time)
        r = await c.patch(f"/zones/{zid1}", json={"is_home": True})
        assert r.status_code == 200 and r.json()["is_home"] is True, "zone1 becomes the home zone"

        body2 = {"name": "zztest_zone2", "latitude": 44.0, "longitude": 15.0, "is_home": True}
        r = await c.post("/zones", json=body2)
        assert r.status_code == 201 and r.json()["is_home"] is True, "zone2 created as the new home zone"
        zid2 = r.json()["id"]
        assert r.json()["radius_m"] == 100, "radius_m defaults to 100 when omitted"

        r = await c.get("/zones")
        rows = {z["id"]: z for z in r.json()}
        assert rows[zid2]["is_home"] is True, "zone2 is now the home zone"
        assert rows[zid1]["is_home"] is False, "zone1's home flag was cleared by zone2's creation"

        # delete zone2 → 204, gone from the list
        r = await c.delete(f"/zones/{zid2}")
        assert r.status_code == 204, "delete zone2"
        r = await c.get("/zones")
        assert not any(z["id"] == zid2 for z in r.json()), "zone2 is gone after delete"

        # delete zone1 → 204, gone from the list
        r = await c.delete(f"/zones/{zid1}")
        assert r.status_code == 204, "delete zone1"
        r = await c.get("/zones")
        assert not any(z["id"] == zid1 for z in r.json()), "zone1 is gone after delete"

        # deleting an already-gone zone is a no-op 204 (the route has no existence check)
        r = await c.delete(f"/zones/{zid1}")
        assert r.status_code == 204, "deleting a non-existent zone is still 204 (unconditional DELETE)"

    await pool.execute("DELETE FROM zones WHERE name LIKE 'zztest_zone%'")
    await pool.execute("DELETE FROM users WHERE username = 'zoneadmin'")
    await pool.close()
