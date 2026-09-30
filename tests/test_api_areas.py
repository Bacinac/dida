"""Integration test — the area (room) routes end to end against a real
Postgres: list (any authenticated user), admin create/patch/delete, and the
"nothing to update" / not-found edge cases.

NB: there is no dida_api/areas.py — the /areas routes are defined in
dida_api/entities.py (entity + area topology). This file is named for the
route surface it covers, matching the task's naming, not the module path.

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


async def test_areas_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM areas WHERE name LIKE 'zztest_area%'")
    await pool.execute("DELETE FROM users WHERE username = 'areaadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('areaadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "areas-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "areaadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # create → 201, echoes {id, name, kind}; kind is unset at creation
        r = await c.post("/areas", json={"name": "zztest_area1"})
        assert r.status_code == 201, "admin creates an area"
        area1 = r.json()
        aid1 = area1["id"]
        assert area1["name"] == "zztest_area1", "create echoes the name"
        assert area1["kind"] is None, "kind is unset at creation"

        # list contains the new area (round-trip); the full column set is present
        r = await c.get("/areas")
        assert r.status_code == 200
        row = next(a for a in r.json() if a["id"] == aid1)
        assert row["name"] == "zztest_area1"
        assert set(row) == {"id", "name", "kind", "fp_floor", "fp_x", "fp_y", "fp_poly", "sensor_config", "media_config"}

        # patch: rename, retype, place on the floorplan, trace a polygon, set the
        # sensor/media config → 200, echoing every updated field
        patch_body = {
            "name": "zztest_area1_renamed",
            "kind": "utility",
            "fp_floor": "ground",
            "fp_x": 12.5,
            "fp_y": 34.25,
            "fp_poly": [[0, 0], [10, 0], [10, 10], [0, 10]],
            "sensor_config": {"hidden": ["pressure"]},
            "media_config": {"sources": []},
        }
        r = await c.patch(f"/areas/{aid1}", json=patch_body)
        assert r.status_code == 200
        patched = r.json()
        assert patched["name"] == "zztest_area1_renamed" and patched["kind"] == "utility"
        assert patched["fp_floor"] == "ground" and patched["fp_x"] == 12.5 and patched["fp_y"] == 34.25
        assert patched["fp_poly"] == [[0, 0], [10, 0], [10, 10], [0, 10]]
        assert patched["sensor_config"] == {"hidden": ["pressure"]}
        assert patched["media_config"] == {"sources": []}

        # reflected on a fresh GET
        r = await c.get("/areas")
        row = next(a for a in r.json() if a["id"] == aid1)
        assert row["name"] == "zztest_area1_renamed" and row["kind"] == "utility", "patch persisted"
        assert row["fp_poly"] == [[0, 0], [10, 0], [10, 10], [0, 10]], "polygon persisted"

        # patch with an empty body → 400
        r = await c.patch(f"/areas/{aid1}", json={})
        assert r.status_code == 400, "empty patch body is rejected"

        # patch a non-existent area → 404
        r = await c.patch("/areas/999999999", json={"name": "ghost"})
        assert r.status_code == 404, "patching a non-existent area is 404"

        # a second area, to prove delete only removes the targeted row
        r = await c.post("/areas", json={"name": "zztest_area2"})
        assert r.status_code == 201
        aid2 = r.json()["id"]

        # delete area1 → 204, gone from the list; area2 untouched
        r = await c.delete(f"/areas/{aid1}")
        assert r.status_code == 204, "delete area1"
        r = await c.get("/areas")
        ids = {a["id"] for a in r.json()}
        assert aid1 not in ids, "area1 is gone after delete"
        assert aid2 in ids, "area2 is untouched"

        # deleting an already-gone area is a no-op 204 (the route has no existence check)
        r = await c.delete(f"/areas/{aid1}")
        assert r.status_code == 204, "deleting a non-existent area is still 204 (unconditional DELETE)"

        r = await c.delete(f"/areas/{aid2}")
        assert r.status_code == 204, "delete area2"

    await pool.execute("DELETE FROM areas WHERE name LIKE 'zztest_area%'")
    await pool.execute("DELETE FROM users WHERE username = 'areaadmin'")
    await pool.close()
