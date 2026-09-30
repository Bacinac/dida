"""Integration test — scenes (dida_api.scenes) end to end against a real
Postgres: save a scene (captures settable current_state), list it, recall it
(asserts the captured state is republished as a command through the bus), and
delete it, with the 404/409 edges. Exercises the migration-0027 scenes table
and the current_state → entities FK chain a scene's capture reads from.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly — the routes read app.state.pool/.secret_key/.bus. StubBus captures
published commands so recall can be asserted against the bus, not just the
response body.
"""
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


async def test_scenes_save_recall_list_delete():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM scenes WHERE lower(name) = lower('zztest_scene')")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'mqtt:zzscnlamp'")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'mqtt:zzscnlamp'")
    await pool.execute("DELETE FROM users WHERE username = 'scnadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('scnadmin', $1, 'admin')", await hash_password("adminpw12")
    )
    await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ('mqtt:zzscnlamp', 'mqtt')")
    # A settable on_off row — create_scene only captures states setter_command()
    # recognises (sensors/momentary verbs are skipped), so the scene needs a real,
    # actionable capability. Native bool, no pre-dump/cast — the pool's jsonb
    # codec (jsonb_init) encodes it exactly once (see scenes.py's own comment).
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES ('mqtt:zzscnlamp', 'on_off', $1)",
        True,
    )

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "scenes-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "scnadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # save → 201, captures the one settable row
        r = await c.post("/scenes", json={"name": "zztest_scene", "entity_ids": ["mqtt:zzscnlamp"]})
        assert r.status_code == 201, "admin saves a scene"
        body = r.json()
        scene_id = body["id"]
        assert body["name"] == "zztest_scene" and body["count"] == 1, "scene captured the one settable state"

        # duplicate name (case-insensitive unique index) → 409
        r = await c.post("/scenes", json={"name": "ZZTEST_SCENE", "entity_ids": ["mqtt:zzscnlamp"]})
        assert r.status_code == 409, "duplicate scene name is rejected case-insensitively"

        # list contains it, with the captured count
        r = await c.get("/scenes")
        assert r.status_code == 200
        listed = next(s for s in r.json() if s["name"] == "zztest_scene")
        assert listed["count"] == 1, "list reflects the captured state count"

        # recall → republishes a command for the captured state via the bus
        r = await c.post(f"/scenes/{scene_id}/recall")
        assert r.status_code == 200
        assert r.json() == {"applied": 1, "skipped": 0}, "the one captured state was applied, none skipped"
        assert len(bus.commands) == 1, "recall published exactly one command to the bus"
        cmd = bus.commands[0]
        assert cmd.entity_id == "mqtt:zzscnlamp"
        assert cmd.capability == "on_off"
        assert cmd.command == "turn_on", "captured value True round-trips through setter_command to turn_on"
        assert cmd.args == {}
        assert cmd.source == f"user:scnadmin:scene:{scene_id}", "audit trail names the initiating scene"

        # recalling a non-existent scene → 404
        r = await c.post("/scenes/999999999/recall")
        assert r.status_code == 404, "recalling a gone scene is 404"

        # delete → 204, gone from the list
        r = await c.delete(f"/scenes/{scene_id}")
        assert r.status_code == 204, "delete scene"
        r = await c.get("/scenes")
        assert not any(s["name"] == "zztest_scene" for s in r.json()), "scene is gone after delete"

        # deleting an already-gone scene → 404
        r = await c.delete(f"/scenes/{scene_id}")
        assert r.status_code == 404, "deleting a non-existent scene is 404"

    await pool.execute("DELETE FROM scenes WHERE lower(name) = lower('zztest_scene')")
    await pool.execute("DELETE FROM current_state WHERE entity_id = 'mqtt:zzscnlamp'")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'mqtt:zzscnlamp'")
    await pool.execute("DELETE FROM users WHERE username = 'scnadmin'")
    await pool.close()


async def test_scene_inspect_edit_and_scoped_capture():
    """GET/PUT — a scene can be seen and changed, not just created and fired.

    Until these existed the only way to alter a scene was delete-and-recapture,
    and there was no way at all to see what one would do before pressing recall —
    on a whole-house snapshot that included a door lock.
    """
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM scenes WHERE lower(name) LIKE 'zzedit%'")
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'mqtt:zzedit%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'mqtt:zzedit%'")
    await pool.execute("DELETE FROM users WHERE username = 'scneditor'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('scneditor', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    # A control lamp and a CONFIG entity. An unscoped capture must take the first
    # and leave the second: config is device tuning, not "how the room looks".
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, category) VALUES ('mqtt:zzeditlamp', 'mqtt', 'control')")
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, category) VALUES ('mqtt:zzeditcfg', 'mqtt', 'config')")
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES ('mqtt:zzeditlamp', 'on_off', $1)", True)
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES ('mqtt:zzeditlamp', 'brightness', $1)", 40)
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES ('mqtt:zzeditcfg', 'number', $1)", 7)

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "scenes-edit-test-secret-0123456789ab"
    appmod.app.state.bus = bus

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login",
                             json={"username": "scneditor", "password": "adminpw12"})).status_code == 200

        # Unscoped capture skips the config entity.
        r = await c.post("/scenes", json={"name": "zzedit_all"})
        assert r.status_code == 201
        got = {(s["entity_id"], s["capability"])
               for s in (await c.get(f"/scenes/{r.json()['id']}")).json()["states"]}
        assert ("mqtt:zzeditlamp", "on_off") in got
        assert ("mqtt:zzeditcfg", "number") not in got, "config is not part of a scene"

        # Scoped capture takes exactly what was asked for.
        r = await c.post("/scenes", json={"name": "zzedit_one",
                                          "entity_ids": ["mqtt:zzeditlamp"]})
        sid = r.json()["id"]
        detail = (await c.get(f"/scenes/{sid}")).json()
        assert detail["name"] == "zzedit_one"
        assert {s["capability"] for s in detail["states"]} == {"on_off", "brightness"}

        # Edit: rename, drop a row, change a value.
        r = await c.put(f"/scenes/{sid}", json={
            "name": "zzedit_renamed",
            "states": [{"entity_id": "mqtt:zzeditlamp", "capability": "brightness", "value": 15}],
        })
        assert r.status_code == 200 and r.json()["count"] == 1
        detail = (await c.get(f"/scenes/{sid}")).json()
        assert detail["name"] == "zzedit_renamed"
        assert detail["states"] == [
            {"entity_id": "mqtt:zzeditlamp", "capability": "brightness", "value": 15}]

        # An edited value is validated exactly like a captured one — brightness is
        # 0..100, so an out-of-range edit is refused rather than stored and later
        # dropped at recall time.
        r = await c.put(f"/scenes/{sid}", json={
            "name": "zzedit_renamed",
            "states": [{"entity_id": "mqtt:zzeditlamp", "capability": "brightness", "value": 900}],
        })
        assert r.status_code == 400, "an out-of-range value cannot be stored"

        # A capability with nothing settable can't be put in a scene either.
        r = await c.put(f"/scenes/{sid}", json={
            "name": "zzedit_renamed",
            "states": [{"entity_id": "mqtt:zzeditlamp", "capability": "temperature", "value": 21}],
        })
        assert r.status_code == 400, "a sensor reading is not a scene entry"

        # The rejected edits left the stored scene untouched.
        detail = (await c.get(f"/scenes/{sid}")).json()
        assert detail["states"][0]["value"] == 15

        # Recall drives the edited value, not the captured one.
        bus.commands.clear()
        r = await c.post(f"/scenes/{sid}/recall")
        assert r.json() == {"applied": 1, "skipped": 0}
        assert bus.commands[0].command == "set_brightness"
        assert bus.commands[0].args == {"value": 15}

        assert (await c.put("/scenes/999999999",
                            json={"name": "ghost", "states": []})).status_code == 404
        assert (await c.get("/scenes/999999999")).status_code == 404

    await pool.execute("DELETE FROM scenes WHERE lower(name) LIKE 'zzedit%'")
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'mqtt:zzedit%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'mqtt:zzedit%'")
    await pool.execute("DELETE FROM users WHERE username = 'scneditor'")
    await pool.close()
