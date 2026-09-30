"""Integration test — the virtual-entity ("helper") routes (dida_api.virtual) end
to end against a real Postgres: list (any authenticated user), admin create with
its capability/enum validation and slug-derived entity_id, the duplicate-slug
409, and delete — including the side-effect cleanup of the entity's live
current_state/entities rows once the virtual adapter has registered it.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import json

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _cleanup(pool):
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'virtual:zztest%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'virtual:zztest%'")
    await pool.execute("DELETE FROM virtual_entities WHERE entity_id LIKE 'virtual:zztest%'")
    await pool.execute("DELETE FROM users WHERE username = 'virtualadmin'")


async def test_virtual_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('virtualadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "virtual-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "virtualadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # invalid capability → 400, nothing inserted
        r = await c.post("/virtual", json={"name": "ZZTest Bad", "capability": "string"})
        assert r.status_code == 400, "capability must be one of the helper types"

        # enum with fewer than 2 real options (blanks are stripped first) → 400
        r = await c.post("/virtual", json={"name": "ZZTest Bad Enum", "capability": "enum", "options": [" ", "Only"]})
        assert r.status_code == 400, "an enum helper needs at least 2 real options"

        # create a boolean helper → 201, entity_id is a lowercased slug of the name
        r = await c.post("/virtual", json={"name": "ZZTest Helper", "capability": "boolean"})
        assert r.status_code == 201, "admin creates a boolean helper"
        helper = r.json()
        assert helper["entity_id"] == "virtual:zztest_helper", "entity_id is the slugged name"
        assert helper["name"] == "ZZTest Helper", "the display name keeps its original casing"
        assert helper["capability"] == "boolean" and helper["options"] is None, "non-enum helpers carry no options"

        # a switch helper: what an automation acts on and puts back, and what a
        # voice assistant is handed through Matter as a sentence it may say
        r = await c.post("/virtual", json={"name": "ZZTest Show Ema", "capability": "on_off"})
        assert r.status_code == 201, "admin creates a switch helper"
        assert r.json()["entity_id"] == "virtual:zztest_show_ema"
        assert r.json()["capability"] == "on_off" and r.json()["options"] is None

        # duplicate name → same slug → 409
        r = await c.post("/virtual", json={"name": "ZZTest Helper", "capability": "boolean"})
        assert r.status_code == 409, "a second helper with the same slug is rejected"

        # a boolean helper defaults to category 'control' (a device you operate)
        assert helper["category"] == "control", "helpers default to control (shown on devices)"

        # a `time` CONFIG helper: the household-setting shape (quiet-hours boundary).
        # category 'config' is what hides it from the device/floor-plan views.
        r = await c.post("/virtual", json={"name": "ZZQuiet Start", "capability": "time", "category": "config"})
        assert r.status_code == 201, "admin creates a time config helper"
        th = r.json()
        assert th["entity_id"] == "virtual:zzquiet_start"
        assert th["capability"] == "time" and th["category"] == "config", "time + config round-trip"

        # a bogus category is rejected (fail loud, never silently stored as control)
        r = await c.post("/virtual", json={"name": "ZZBad Cat", "capability": "boolean", "category": "banana"})
        assert r.status_code == 400, "category must be control|config"

        # quiet-hours boundaries + day/night are NO LONGER virtual fields — migration 0050
        # consolidated them into self-contained computed helpers (helper:quiet_hours inlines
        # its window, helper:daynight derives from the sun). They must not resurface here.
        r = await c.get("/virtual")
        seeded = {v["entity_id"] for v in r.json()}
        assert "virtual:quiet_start" not in seeded, "quiet_start consolidated into helper:quiet_hours"
        assert "virtual:quiet_end" not in seeded, "quiet_end consolidated into helper:quiet_hours"
        assert "virtual:daynight" not in seeded, "daynight consolidated into helper:daynight"

        # enum helper: blank options are filtered, order preserved
        r = await c.post(
            "/virtual",
            json={"name": "ZZTest Enum", "capability": "enum", "options": [" ", "Low", "", "Mid", "High"]},
        )
        assert r.status_code == 201, "admin creates an enum helper"
        enum_helper = r.json()
        assert enum_helper["entity_id"] == "virtual:zztest_enum"
        assert enum_helper["options"] == ["Low", "Mid", "High"], "blank options are dropped, order kept"

        # list contains both, round-tripped
        r = await c.get("/virtual")
        assert r.status_code == 200
        ids = {v["entity_id"] for v in r.json()}
        assert {"virtual:zztest_helper", "virtual:zztest_enum"} <= ids, "both helpers appear in the list"

        # simulate the virtual adapter having registered the helper as a live
        # entity with state — delete must clean up both, not just its own row
        await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ('virtual:zztest_helper', 'virtual')")
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value) VALUES ('virtual:zztest_helper', 'boolean', $1::jsonb)",
            json.dumps(True),
        )

        # delete a non-virtual-looking id → 400, no lookup even attempted
        r = await c.delete("/virtual/mqtt:some_switch")
        assert r.status_code == 400, "not a virtual entity id"

        # delete the helper → 204; its virtual_entities row, live state and
        # registry entry are all gone
        r = await c.delete("/virtual/virtual:zztest_helper")
        assert r.status_code == 204, "delete the boolean helper"
        r = await c.get("/virtual")
        assert not any(v["entity_id"] == "virtual:zztest_helper" for v in r.json()), "gone from the list"
        left_state = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id = 'virtual:zztest_helper'")
        assert left_state == 0, "live state cleaned up"
        left_entity = await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id = 'virtual:zztest_helper'")
        assert left_entity == 0, "registry entry cleaned up"

        # deleting an already-gone helper → 404
        r = await c.delete("/virtual/virtual:zztest_helper")
        assert r.status_code == 404, "helper not found"

        # clean up the enum helper too
        r = await c.delete("/virtual/virtual:zztest_enum")
        assert r.status_code == 204, "delete the enum helper"

    await _cleanup(pool)
    await pool.close()
