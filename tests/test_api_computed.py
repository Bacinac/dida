"""Integration test — the computed-helper routes (dida_api.computed_helpers) end to
end against a real Postgres. Helpers reuse the automation Condition model: a rule is
condition→value branches (+ default), or an advanced Starlark `value = …`. The API
validates the value TYPE, the conditions, and the no-computed-input rule (a helper may
read only raw statuses, never another helper/derived — no race/cycle).
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


NIGHT = {  # a typed helper: WHEN sun < -6 → "night", else "day"
    "branches": [{"conditions": [{"entity_id": "astro:sun", "capability": "sun_elevation",
                                  "op": "<", "value": -6}], "value": "night"}],
    "default": "day",
}


async def test_computed_helper_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM computed_helpers WHERE entity_id LIKE 'helper:zz%'")
    await pool.execute("DELETE FROM users WHERE username = 'chadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('chadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "computed-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "chadmin", "password": "adminpw12"})).status_code == 200

        # typed create → 201: a condition→value rule, no code
        r = await c.post("/computed-helpers", json={"name": "ZZ Night", "capability": "enum", "definition": NIGHT})
        assert r.status_code == 201, "admin creates a typed computed helper"
        h = r.json()
        assert h["entity_id"] == "helper:zz_night" and h["capability"] == "enum" and h["last_error"] is None

        # bogus value type → 400
        assert (await c.post("/computed-helpers", json={"name": "ZZ Bad", "capability": "banana",
                "definition": {"branches": [], "default": 1}})).status_code == 400

        # a branch value that violates the capability → 400 (validated against the type)
        assert (await c.post("/computed-helpers", json={"name": "ZZ BadVal", "capability": "number",
                "definition": {"branches": [{"conditions": [{"entity_id": "x:y", "capability": "motion",
                "op": "==", "value": True}], "value": "not-a-number"}], "default": 0}})).status_code == 400

        # a condition reading ANOTHER computed value → 400 (no race/cycle)
        assert (await c.post("/computed-helpers", json={"name": "ZZ Loop", "capability": "text",
                "definition": {"branches": [{"conditions": [{"entity_id": "helper:zz_night",
                "capability": "enum", "op": "==", "value": "night"}], "value": "x"}], "default": "y"}})
                ).status_code == 400
        # advanced script reading a derived value → 400 too
        assert (await c.post("/computed-helpers", json={"name": "ZZ Loop2", "capability": "text",
                "definition": {"script": 'value = state("derived:x", "boolean")'}})).status_code == 400

        # advanced Starlark helper (raw statuses only) → 201
        r = await c.post("/computed-helpers", json={"name": "ZZ Free", "capability": "text",
                "definition": {"script": 'value = "sun " + str(state("astro:sun", "sun_elevation"))'}})
        assert r.status_code == 201, "an advanced script over raw statuses is fine"

        # duplicate slug → 409
        assert (await c.post("/computed-helpers", json={"name": "ZZ Night", "capability": "text",
                "definition": {"script": "value = 1"}})).status_code == 409

        # neither branches nor script → 400
        assert (await c.post("/computed-helpers", json={"name": "ZZ Empty", "capability": "text",
                "definition": {}})).status_code == 400

        # list has both, update swaps the rule, delete cleans up the entity
        ids = {x["entity_id"] for x in (await c.get("/computed-helpers")).json()}
        assert {"helper:zz_night", "helper:zz_free"} <= ids
        r = await c.put(f"/computed-helpers/{h['id']}", json={"name": "ZZ Night", "capability": "boolean",
                "definition": {"branches": [{"conditions": [{"entity_id": "astro:sun",
                "capability": "sun_elevation", "op": "<", "value": -6}], "value": True}], "default": False},
                "enabled": True})
        assert r.status_code == 200 and r.json()["capability"] == "boolean"

        await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ('helper:zz_night', 'helper')")
        await pool.execute("INSERT INTO current_state (entity_id, capability, value) "
                           "VALUES ('helper:zz_night', 'boolean', 'true'::jsonb)")
        assert (await c.delete(f"/computed-helpers/{h['id']}")).status_code == 204
        assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id = 'helper:zz_night'") == 0

    await pool.execute("DELETE FROM computed_helpers WHERE entity_id LIKE 'helper:zz%'")
    await pool.execute("DELETE FROM users WHERE username = 'chadmin'")
    await pool.close()
