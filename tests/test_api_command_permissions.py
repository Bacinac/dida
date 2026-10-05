"""Integration test — the /command permission gate end to end against a real
Postgres: an admin may command any device, a non-admin whose can_control baseline
is false is 403'd (require_control), and an entity-scoped control grant flips the
baseline back to allowed. The security boundary the whole per-user policy rests on.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly — the routes read app.state.pool/.secret_key/.bus.
"""
import json

import dida_api.app as appmod
import pytest
from dida_api import assistant, radio_tuner
from dida_api.auth import AuthUser
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


@pytest.mark.parametrize("access", ["allowed", "denied", "hidden", "missing_player", "empty_stations", "opus_unavailable"])
async def test_rest_and_assistant_authorize_the_same_radio_player(monkeypatch, access):
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username = 'radioperm'")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'opus:permplayer'")
    uid = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) "
        "VALUES ('radioperm', $1, 'user', true) RETURNING id", await hash_password("pw"),
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, capabilities) "
        "VALUES ('opus:permplayer', 'opus', '[\"media_transport\"]')"
    )
    if access in ("denied", "hidden"):
        await pool.execute(
            "INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, $2, 'entity', 'opus:permplayer')",
            uid, "control" if access == "denied" else "view",
        )
    stations = []

    async def resolve_target(pool):
        return None if access == "missing_player" else "opus:permplayer"

    async def opus_stations(pool):
        stations.append(True)
        if access == "opus_unavailable":
            raise radio_tuner.opus.OpusUnavailable("OPUS is unavailable")
        if access == "empty_stations":
            return []
        return [{"id": 1, "name": "Test FM", "url": "https://stream.example/radio"}]

    monkeypatch.setattr(radio_tuner, "resolve_target", resolve_target)
    monkeypatch.setattr(radio_tuner.opus, "stations", opus_stations)
    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "radio-perm-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus
    command = {"entity_id": "radio:tuner", "capability": "media_transport", "command": "play"}
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        await _login(client, "radioperm", "pw")
        response = await client.post("/command", json=command)
    ctx = assistant.AssistantCtx(client=None, pool=pool, bus=bus, ch=None,
                                 user=AuthUser(id=uid, username="radioperm", role="user"))
    output = json.loads(await assistant._execute_tool(ctx, "send_command", command))
    if access == "allowed":
        assert response.status_code == 200 and output["ok"] is True
        assert [(c.entity_id, c.command, c.source) for c in bus.commands] == [
            ("opus:permplayer", "play_media", "user:radioperm"),
            ("opus:permplayer", "play_media", "assistant:radioperm"),
        ]
        assert len(stations) == 2
        assert len(ctx.actions) == 1
    else:
        assert response.status_code == {
            "denied": 403, "hidden": 404, "missing_player": 409, "empty_stations": 400, "opus_unavailable": 503,
        }[access]
        assert "error" in output
        if access == "hidden":
            assert output["error"] == "rejected (capability): unknown device"
        assert bus.commands == [] and ctx.actions == []
        assert len(stations) == (2 if access in ("empty_stations", "opus_unavailable") else 0)
    await pool.execute("DELETE FROM users WHERE username = 'radioperm'")
    await pool.execute("DELETE FROM entities WHERE entity_id = 'opus:permplayer'")
    await pool.close()


async def test_radio_dispatch_keeps_the_authorized_target(monkeypatch):
    from dida_api import commands

    checked = []

    async def require_control(pool, user, entity_id, capability):
        checked.append(entity_id)

    target_calls = []

    async def resolve_target(pool):
        target_calls.append(True)
        return "opus:authorized" if len(target_calls) == 1 else "opus:forbidden"

    async def opus_stations(pool):
        return [{"id": 1, "name": "Test FM", "url": "https://stream.example/radio"}]

    async def app_setting(pool, key):
        return None

    async def set_current(pool, station_id):
        pass

    async def can_view(pool, user, entity_id):
        return True

    monkeypatch.setattr(commands, "require_control", require_control)
    monkeypatch.setattr(commands, "can_view_entity", can_view)
    monkeypatch.setattr(radio_tuner, "resolve_target", resolve_target)
    monkeypatch.setattr(radio_tuner.opus, "stations", opus_stations)
    monkeypatch.setattr(radio_tuner, "app_setting", app_setting)
    monkeypatch.setattr(radio_tuner, "_set_current", set_current)
    bus = StubBus()
    await commands.dispatch_command(None, bus, AuthUser(id=1, username="user", role="user"),
                                    "radio:tuner", "media_transport", "play", {}, source="user:user")
    assert checked == ["radio:tuner", "opus:authorized"]
    assert len(target_calls) == 1
    assert [c.entity_id for c in bus.commands] == ["opus:authorized"]
