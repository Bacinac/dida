"""Integration test — the /command permission gate end to end against a real
Postgres: an admin may command any device, a non-admin whose can_control baseline
is false is 403'd (require_control), and an entity-scoped control grant flips the
baseline back to allowed. The security boundary the whole per-user policy rests on.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly — the routes read app.state.pool/.secret_key/.bus.
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


async def _login(c, username, password):
    r = await c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username}"


async def test_command_permission_gate():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM user_access_rules WHERE ref = 'mqtt:permlamp'")
    await pool.execute("DELETE FROM users WHERE username IN ('cmdadmin', 'cmduser')")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'mqtt:permlamp'")
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('cmdadmin', $1, 'admin')", await hash_password("pw"))
    user_id = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) VALUES ('cmduser', $1, 'user', false) RETURNING id",
        await hash_password("pw"),
    )
    await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ('mqtt:permlamp', 'mqtt')")

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "cmd-perm-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus

    cmd = {"entity_id": "mqtt:permlamp", "capability": "on_off", "command": "turn_on"}

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # admin bypasses the gate → command reaches the bus
        await _login(c, "cmdadmin", "pw")
        r = await c.post("/command", json=cmd)
        assert r.status_code == 200, "admin may command any device"
        assert len(bus.commands) == 1, "admin's command reached the bus"

        # a can_control=false user with no grant is blocked BEFORE the bus
        c.cookies.clear()
        await _login(c, "cmduser", "pw")
        r = await c.post("/command", json=cmd)
        assert r.status_code == 403, "can_control=false + no grant → 403 (require_control)"
        assert len(bus.commands) == 1, "the blocked command never reached the bus"

        # an entity-scoped control grant flips the baseline → now allowed (same cookie,
        # because current_user re-reads can_control + rules on every request)
        await pool.execute(
            "INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'control', 'entity', 'mqtt:permlamp')",
            user_id,
        )
        r = await c.post("/command", json=cmd)
        assert r.status_code == 200, "an entity control grant flips can_control=false to allowed"
        assert len(bus.commands) == 2, "the granted command reached the bus"

    await pool.execute("DELETE FROM user_access_rules WHERE ref = 'mqtt:permlamp'")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'mqtt:permlamp'")
    await pool.execute("DELETE FROM users WHERE username IN ('cmdadmin', 'cmduser')")
    await pool.close()
