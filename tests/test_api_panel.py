"""Integration test — wall-panel plumbing (dida_api.panel) end to end against a
real Postgres: telemetry (/panel/event) allow-lists its event kinds, and the
display config (/panel/config) returns shipped defaults, merges a partial admin
write, range/enum-validates on write, stores only the fields it knows, and is
admin-only to change but readable by the wallpanel.

Runs in the api image; the runner supplies the ephemeral Postgres (tests/run.sh).
Bypasses the app lifespan by wiring app.state directly.
"""
import dida_api.app as appmod
from dida_api.panel import DEFAULTS
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


async def _cleanup(pool):
    await pool.execute("DELETE FROM app_settings WHERE key IN ('panel_config', 'panel_seen_at')")
    await pool.execute("DELETE FROM users WHERE username IN ('panadmin', 'panuser', 'wallpanel')")


async def test_panel_event_and_config():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('panadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('panuser', $1, 'user')",
        await hash_password("userpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "panel-test-secret-0123456789abcdef"

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "panadmin", "password": "adminpw12"})
        assert r.status_code == 200

        # ── telemetry: allow-listed kinds only ──
        r = await c.post("/panel/event", json={"kind": "saver"})
        assert r.status_code == 204
        r = await c.post("/panel/event", json={"kind": "reboot-fleet"})
        assert r.status_code == 400, "unknown event kinds are rejected"

        # ── config: unset → the shipped defaults (2 min idle, kenburns) + the
        #    read-only room_area_id hint (None without a cast panel device) ──
        r = await c.get("/panel/config")
        assert r.status_code == 200
        got = r.json()
        assert got.pop("room_area_id") is None, "no cast panel device → unknown room"
        assert got == DEFAULTS
        assert DEFAULTS["idle_s"] == 120 and DEFAULTS["transition"] == "kenburns"

        # ── partial write MERGES, leaving other fields at their defaults ──
        r = await c.put("/panel/config", json={"idle_s": 90, "transition": "fade"})
        assert r.status_code == 204
        r = await c.get("/panel/config")
        cfg = r.json()
        assert cfg["idle_s"] == 90 and cfg["transition"] == "fade"
        assert cfg["photo_interval_s"] == DEFAULTS["photo_interval_s"], "untouched field kept its default"
        assert cfg["photo_fit"] == "blur"

        # ── validation: range, enum and entity shape all rejected loud ──
        assert (await c.put("/panel/config", json={"idle_s": 5})).status_code == 422, "below the 30 s floor"
        assert (await c.put("/panel/config", json={"photo_interval_s": 9999})).status_code == 422
        assert (await c.put("/panel/config", json={"transition": "wipe"})).status_code == 400, "unknown transition"
        assert (await c.put("/panel/config", json={"photo_fit": "squish"})).status_code == 400, "unknown fit"
        assert (await c.put("/panel/config", json={"presence_entity": "bad id!"})).status_code == 400, "bad presence entity"
        # a rejected write left the prior value intact
        assert (await c.get("/panel/config")).json()["idle_s"] == 90

        # ── a field the panel no longer has (the Immich album) is not kept ──
        assert (await c.put("/panel/config", json={"album_id": "x", "idle_s": 90})).status_code == 204
        assert "album_id" not in (await c.get("/panel/config")).json()

        # ── presence gating: a valid entity id, then cleared back to always-on ──
        assert (await c.put("/panel/config", json={"presence_entity": "homekit:pislivingroom:presence_sensor_1"})).status_code == 204
        assert (await c.get("/panel/config")).json()["presence_entity"] == "homekit:pislivingroom:presence_sensor_1"
        assert (await c.put("/panel/config", json={"presence_entity": ""})).status_code == 204
        assert (await c.get("/panel/config")).json()["presence_entity"] == ""

        # ── only known keys are stored — an unexpected field never rides in ──
        row = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'panel_config'")
        import json
        assert set(json.loads(row)) == set(DEFAULTS), "stored blob is exactly the known fields"

    # ── the wallpanel (non-admin) reads config but cannot change it ──
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "panuser", "password": "userpw12"})
        assert r.status_code == 200
        r = await c.get("/panel/config")
        assert r.status_code == 200 and r.json()["idle_s"] == 90, "the panel reads the live config"
        r = await c.put("/panel/config", json={"idle_s": 300})
        assert r.status_code == 403, "only an admin changes the panel config"
        r = await c.post("/panel/event", json={"kind": "boot"})
        assert r.status_code == 204, "the wallpanel logs its own telemetry"

    await _cleanup(pool)
    await pool.close()


async def test_panel_alive_heartbeat():
    """The liveness beat the cast adapter watches: only the kiosk user stamps it,
    and every beat REFRESHES the stamp (the adapter reads its age). An admin's own
    /panel tab must not feed the watchdog — otherwise a desktop browser left open
    keeps a dead Nest Hub looking alive forever."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    for name, role in (("panuser", "user"), ("wallpanel", "user")):
        await pool.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
            name, await hash_password("userpw12"), role,
        )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "panel-test-secret-0123456789abcdef"

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "panuser", "password": "userpw12"})).status_code == 200
        assert (await c.post("/panel/alive")).status_code == 204
        seen = await pool.fetchval("SELECT updated_at FROM app_settings WHERE key = 'panel_seen_at'")
        assert seen is None, "a non-kiosk user's beat never feeds the watchdog"

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "wallpanel", "password": "userpw12"})).status_code == 200
        assert (await c.post("/panel/alive")).status_code == 204
        first = await pool.fetchval("SELECT updated_at FROM app_settings WHERE key = 'panel_seen_at'")
        assert first is not None, "the kiosk's beat is recorded"

        # Age the stamp past the adapter's 300 s window, then beat again: the row
        # must come back FRESH, or a live panel would be re-cast every 5 minutes.
        await pool.execute(
            "UPDATE app_settings SET updated_at = now() - interval '10 minutes' WHERE key = 'panel_seen_at'"
        )
        stale_age = await pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) FROM app_settings WHERE key = 'panel_seen_at'"
        )
        assert float(stale_age) > 300, "the aged stamp is what the adapter would call dead"
        assert (await c.post("/panel/alive")).status_code == 204
        fresh_age = await pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) FROM app_settings WHERE key = 'panel_seen_at'"
        )
        assert float(fresh_age) < 5, "each beat refreshes the stamp"

    # ── unauthenticated: the beat is not an open write ──
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/panel/alive")).status_code == 401

    await _cleanup(pool)
    await pool.close()
