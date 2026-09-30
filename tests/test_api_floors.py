"""Integration test — the floor-plan level routes (dida_api.floors) end to end
against a real Postgres: list, admin create (with its slug-collision dedup),
rename/reorder patch (immutable key, 400-on-empty, 404-on-missing), and delete
(idempotent — no 404 on an already-gone id). Scoped to the CRUD surface; the
image-upload/border-detection endpoints call out to planvision/disk and aren't
exercised here.

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


async def test_floors_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM floors WHERE key LIKE 'zztest%'")
    await pool.execute("DELETE FROM users WHERE username = 'flradmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('flradmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "floors-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "flradmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # Floors belong to the installation — nothing seeds them, so the list is
        # whatever this database has, and the CRUD below works from its own rows.
        r = await c.get("/floors")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        # create → 201, key is a slug of the name, only the required cols are set
        r = await c.post("/floors", json={"name": "zztest_floor"})
        assert r.status_code == 201, "admin creates a floor"
        f1 = r.json()
        assert f1["key"] == "zztest-floor", "key is a slug of the name"
        assert f1["name"] == "zztest_floor"
        assert f1["img_path"] is None and f1["borders"] is None, "no image/polygon set on create"
        fid1, sort1 = f1["id"], f1["sort_order"]

        # a second floor with the SAME name gets a de-duplicated slug, not a clash
        r = await c.post("/floors", json={"name": "zztest_floor"})
        assert r.status_code == 201
        f2 = r.json()
        assert f2["key"] == "zztest-floor-2", "colliding slug is de-duplicated"
        assert f2["sort_order"] == sort1 + 1, "new floor sorts after the previous max"
        fid2 = f2["id"]

        # new floor appears in the list
        r = await c.get("/floors")
        assert {f["key"] for f in r.json()} >= {"zztest-floor", "zztest-floor-2"}, "both floors appear in the list"

        # patch → rename + reorder; key is immutable
        r = await c.patch(f"/floors/{fid1}", json={"name": "zztest_floor_renamed", "sort_order": 5})
        assert r.status_code == 200
        patched = r.json()
        assert patched["name"] == "zztest_floor_renamed"
        assert patched["sort_order"] == 5
        assert patched["key"] == "zztest-floor", "key never changes on rename"
        r = await c.get("/floors")
        assert next(f for f in r.json() if f["id"] == fid1)["name"] == "zztest_floor_renamed", "rename persisted"

        # patch with no fields → 400
        r = await c.patch(f"/floors/{fid1}", json={})
        assert r.status_code == 400, "empty patch body rejected"

        # patch a non-existent floor → 404
        r = await c.patch("/floors/999999999", json={"name": "Ghost"})
        assert r.status_code == 404, "patching a non-existent floor is 404"

        # delete removes it
        r = await c.delete(f"/floors/{fid2}")
        assert r.status_code == 204
        r = await c.get("/floors")
        assert not any(f["id"] == fid2 for f in r.json()), "deleted floor is gone from the list"
        assert any(f["id"] == fid1 for f in r.json()), "the other floor is untouched"

        r = await c.delete(f"/floors/{fid1}")
        assert r.status_code == 204

        # deleting an already-gone floor is a no-op 204, not a 404 (no existence guard)
        r = await c.delete(f"/floors/{fid1}")
        assert r.status_code == 204, "delete is idempotent"

    await pool.execute("DELETE FROM floors WHERE key LIKE 'zztest%'")
    await pool.execute("DELETE FROM users WHERE username = 'flradmin'")
    await pool.close()
