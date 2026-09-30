"""Integration test — the automation-rule CRUD routes (dida_api.automations) end
to end against a real Postgres: create (+ 400 on an invalid definition), list,
update (unconditionally clears last_error), patch enable/disable (conditionally
clears last_error only on re-enable — the breaker-reset semantics), force-run
(blocked while disabled, publishes bus.publish_raw once enabled), and delete
with its 404-on-repeat guard.

Definitions are validated by `validate_definition` (dida_core.automations) at the
write boundary — the minimal valid shape mirrors tests/test_automation_transitions.py:
one trigger + one action.

SKIPPED: /automations/synthesize and /{id}/explain (both need a configured Claude
client — app.state has none wired here) and /automations/check-script (delegates to
the automation service's Starlark sandbox over bus.nc.request — a live engine, not
just a NATS publish). /run IS covered: it only does `bus.publish_raw(...)`, which a
plain StubBus method can record without a live automation service.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import json

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

# Mirrors the shape used by tests/test_automation_transitions.py: a `to=` trigger
# + one turn_on action — the minimal definition validate_definition() accepts.
_VALID_DEF = {
    "triggers": [{"entity_id": "mqtt:zztest_door", "capability": "contact", "to": True}],
    "actions": [{"entity_id": "mqtt:zztest_siren", "capability": "on_off", "command": "turn_on"}],
}
_UPDATED_DEF = {
    "triggers": [{"entity_id": "mqtt:zztest_door", "capability": "contact", "to": False}],
    "actions": [{"entity_id": "mqtt:zztest_siren", "capability": "on_off", "command": "turn_off"}],
}
# No actions -> validate_definition raises "automation has no actions".
_INVALID_DEF = {"triggers": _VALID_DEF["triggers"], "actions": []}


class StubBus:
    def __init__(self):
        self.raw_calls = []

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass

    async def publish_raw(self, subject, payload):
        self.raw_calls.append((subject, payload))


async def _cleanup(pool):
    await pool.execute("DELETE FROM automation_runs WHERE name LIKE 'zztest_auto%'")
    await pool.execute("DELETE FROM automations WHERE name LIKE 'zztest_auto%'")
    await pool.execute("DELETE FROM users WHERE username = 'autoadmin'")


async def test_automations_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('autoadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "automations-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "autoadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # create with an invalid definition (no actions) -> 400, boundary validation
        r = await c.post("/automations", json={"name": "zztest_auto_bad", "definition": _INVALID_DEF})
        assert r.status_code == 400, "an automation with no actions is rejected at the boundary"

        # create -> 201, echoes the full row
        r = await c.post("/automations", json={"name": "zztest_auto1", "definition": _VALID_DEF})
        assert r.status_code == 201, "admin creates an automation"
        row = r.json()
        aid = row["id"]
        assert row["name"] == "zztest_auto1" and row["enabled"] is True, "create echoes name + default enabled"
        assert row["definition"] == _VALID_DEF, "create echoes the definition verbatim (no msgspec normalisation)"
        assert row["last_triggered_at"] is None and row["last_error"] is None, "a fresh rule has no run history"

        # list contains the new rule (round-trip)
        r = await c.get("/automations")
        assert r.status_code == 200
        assert any(a["id"] == aid and a["name"] == "zztest_auto1" for a in r.json()), "new rule appears in the list"

        # seed a last_error as if the breaker had tripped, to prove PUT clears it
        # UNCONDITIONALLY (unlike the enable/disable patch below)
        await pool.execute("UPDATE automations SET last_error = 'boom' WHERE id = $1", aid)

        # update -> 200, rename + swap definition + disable; last_error cleared
        r = await c.put(
            f"/automations/{aid}",
            json={"name": "zztest_auto1_renamed", "definition": _UPDATED_DEF, "enabled": False},
        )
        assert r.status_code == 200
        updated = r.json()
        assert updated["name"] == "zztest_auto1_renamed" and updated["enabled"] is False, "update persisted name + enabled"
        assert updated["definition"] == _UPDATED_DEF, "update persisted the new definition"
        assert updated["last_error"] is None, "PUT unconditionally clears last_error"

        # update with an invalid definition -> 400 (validation runs on update too)
        r = await c.put(f"/automations/{aid}", json={"name": "x", "definition": _INVALID_DEF, "enabled": True})
        assert r.status_code == 400, "update also rejects an invalid definition"

        # update a non-existent rule -> 404
        r = await c.put("/automations/999999999", json={"name": "ghost", "definition": _VALID_DEF, "enabled": True})
        assert r.status_code == 404, "updating a non-existent automation is 404"

        # run while disabled -> 409, nothing published
        r = await c.post(f"/automations/{aid}/run")
        assert r.status_code == 409, "force-run a disabled rule is rejected"
        assert bus.raw_calls == [], "a blocked run never reaches the bus"

        # re-seed last_error, then PATCH enabled=true -> 200, last_error cleared
        # (the CASE WHEN $2 THEN NULL branch)
        await pool.execute("UPDATE automations SET last_error = 'boom again' WHERE id = $1", aid)
        r = await c.patch(f"/automations/{aid}", json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["enabled"] is True and r.json()["last_error"] is None, "re-enabling clears a tripped breaker"

        # run while enabled -> 200, publishes to the run subject with the id payload
        r = await c.post(f"/automations/{aid}/run")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert len(bus.raw_calls) == 1, "an enabled run reaches the bus exactly once"
        subject, payload = bus.raw_calls[0]
        assert subject == "dida.automation.run"
        assert json.loads(payload) == {"id": aid}, "the run payload carries the automation id"

        # PATCH enabled=false while last_error is set -> 200, last_error PRESERVED
        # (the CASE WHEN $2 THEN NULL ELSE last_error branch — disabling doesn't wipe it)
        await pool.execute("UPDATE automations SET last_error = 'still broken' WHERE id = $1", aid)
        r = await c.patch(f"/automations/{aid}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False and r.json()["last_error"] == "still broken", (
            "disabling preserves a pre-existing last_error"
        )

        # patch a non-existent rule -> 404
        r = await c.patch("/automations/999999999", json={"enabled": True})
        assert r.status_code == 404, "patching a non-existent automation is 404"

        # run a non-existent rule -> 404
        r = await c.post("/automations/999999999/run")
        assert r.status_code == 404, "force-running a non-existent automation is 404"

        # runs log: no firings were ever recorded (force-run only publishes to the
        # automation service, it doesn't write automation_runs itself) — still a
        # well-formed empty list, filterable by automation_id
        r = await c.get("/automations/runs")
        assert r.status_code == 200 and isinstance(r.json(), list)
        r = await c.get("/automations/runs", params={"automation_id": aid})
        assert r.status_code == 200 and r.json() == [], "no runs were logged for this rule"

        # delete -> 204, gone from the list
        r = await c.delete(f"/automations/{aid}")
        assert r.status_code == 204, "delete automation"
        r = await c.get("/automations")
        assert not any(a["id"] == aid for a in r.json()), "rule is gone after delete"

        # deleting an already-gone rule -> 404 (unlike zones' unconditional delete,
        # this route checks the affected row count)
        r = await c.delete(f"/automations/{aid}")
        assert r.status_code == 404, "deleting a non-existent automation is 404"

    await _cleanup(pool)
    await pool.close()
