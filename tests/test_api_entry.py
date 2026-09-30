"""Integration test — the /entry access-point routes (dida_api.entry) end to end
against a real Postgres: the default-empty config, the admin PUT round-trip
(including silently skipping a slot whose configured entity doesn't exist, and
carrying a state-status source through even though it's never validated), the
action dispatch (a resolved bus command + the door's notify:all broadcast), and
the real security boundary entry.py leans on for a limited-trust login —
is_hidden (404, checked first) and require_control (403) — ahead of the plain
admin-only guard on PUT.

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
    def __init__(self):
        self.commands = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def publish_event(self, *a, **k):
        pass


async def _cleanup(pool):
    await pool.execute("DELETE FROM user_access_rules WHERE ref LIKE 'zztest:%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%'")
    await pool.execute("DELETE FROM app_settings WHERE key = 'entry_controls'")
    await pool.execute("DELETE FROM users WHERE username IN ('entryadmin', 'entryuser')")


async def test_entry_config_and_action():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('entryadmin', $1, 'admin')", await hash_password("adminpw12")
    )
    user_id = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) VALUES ('entryuser', $1, 'user', false) "
        "RETURNING id",
        await hash_password("userpw12"),
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, capabilities) VALUES ($1, 'mqtt', $2::jsonb)",
        "zztest:car_gate", json.dumps(["open_close"]),
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, capabilities) VALUES ($1, 'mqtt', $2::jsonb)",
        "zztest:door_lock", json.dumps(["lock"]),
    )
    # entryuser is view-blocked from the door — exercises the is_hidden branch
    # of /entry/action (checked BEFORE require_control).
    await pool.execute(
        "INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'view', 'entity', 'zztest:door_lock')",
        user_id,
    )

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "entry-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "entryadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # nothing configured yet → empty slots, notify defaults to True
        r = await c.get("/entry/config")
        assert r.status_code == 200
        assert r.json() == {"slots": [], "notify_on_open": True}, "no config yet → empty surface"

        # admin PUT: car + door bound to real entities, pedestrian to a ghost
        # entity that doesn't exist (must be silently skipped, not an error),
        # plus a state-status source for the door (never validated either)
        body = {
            "car": "zztest:car_gate",
            "pedestrian": "zztest:ghost",
            "door": "zztest:door_lock",
            "state_door": "zztest:door_contact",
            "notify_on_open": True,
        }
        r = await c.put("/entry/config", json=body)
        assert r.status_code == 204, "admin sets the entry config"

        r = await c.get("/entry/config")
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["notify_on_open"] is True
        slots = {s["slot"]: s for s in cfg["slots"]}
        assert set(slots) == {"car", "door"}, "the ghost pedestrian entity is skipped, not erroring"
        assert slots["car"] == {
            "slot": "car", "entity_id": "zztest:car_gate", "name": "zztest:car_gate",
            "kind": "cover", "state_entity": None, "state_fallback": None,
        }, "car resolves to its open_close entity (cover-kind tile, no live-status source)"
        assert slots["door"]["kind"] == "lock" and slots["door"]["entity_id"] == "zztest:door_lock"
        assert slots["door"]["state_entity"] == "zztest:door_contact", "state source carried through unvalidated"
        assert slots["door"]["state_fallback"] is None, "no fallback was configured"

        # action: car opens via open_close → the bus gets exactly the resolved command
        r = await c.post("/entry/action", json={"slot": "car"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert len(bus.commands) == 1
        cmd = bus.commands[0]
        assert (cmd.entity_id, cmd.capability, cmd.command) == ("zztest:car_gate", "open_close", "open")
        assert cmd.source == "user:entryadmin:entry"

        # action: door opens via lock/unlock AND fires the notify:all broadcast
        # (notify_on_open=True) — two more commands land on the bus
        r = await c.post("/entry/action", json={"slot": "door"})
        assert r.status_code == 200
        assert len(bus.commands) == 3
        door_cmd, notify_cmd = bus.commands[1], bus.commands[2]
        assert (door_cmd.entity_id, door_cmd.capability, door_cmd.command) == ("zztest:door_lock", "lock", "unlock")
        assert notify_cmd.entity_id == "notify:all" and notify_cmd.capability == "notify"
        assert notify_cmd.args["message"] == "Ulazna vrata otvorena — entryadmin"

        # an unconfigured slot → 404 before ever touching the bus
        r = await c.post("/entry/action", json={"slot": "pedestrian"})
        assert r.status_code == 404, "pedestrian was never bound to a real entity"

        # an unknown slot name → 400
        r = await c.post("/entry/action", json={"slot": "attic"})
        assert r.status_code == 400, "not one of car/pedestrian/door"

        # non-admin may READ the config (current_user, not require_admin)…
        c.cookies.clear()
        r = await c.post("/auth/login", json={"username": "entryuser", "password": "userpw12"})
        assert r.status_code == 200, "entryuser login"
        r = await c.get("/entry/config")
        assert r.status_code == 200, "any authenticated user may read the resolved surface"

        # …but not WRITE it
        r = await c.put("/entry/config", json={"car": "zztest:car_gate"})
        assert r.status_code == 403, "PUT /entry/config is admin-only"

        # the real boundary #1: a view-hidden entity 404s BEFORE the control check
        before = len(bus.commands)
        r = await c.post("/entry/action", json={"slot": "door"})
        assert r.status_code == 404, "door is view-hidden from entryuser → 404, not 403"
        assert len(bus.commands) == before, "a hidden action never reaches the bus"

        # the real boundary #2: car isn't hidden, but entryuser's can_control=false
        # baseline with no grant blocks it → 403
        r = await c.post("/entry/action", json={"slot": "car"})
        assert r.status_code == 403, "can_control=false + no grant → 403 (require_control)"
        assert len(bus.commands) == before, "a blocked action never reaches the bus"

    await _cleanup(pool)
    await pool.close()
