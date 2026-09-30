"""Integration tests — the API's per-user access boundary on surfaces the audit
found leaky, end to end against a real Postgres:

- METADATA/CONFIG list endpoints that leak house structure to a narrow login are
  now admin-only: GET /automations + /automations/runs (a rule definition names
  every entity it touches, incl. ones hidden from the caller) and GET
  /notify/targets (enumerates every user with a push subscription). A non-admin
  gets 403; an admin still gets the full list; an anonymous request gets 401.

- radio:tuner is a RELAY: a /command to the synthetic radio:tuner entity
  republishes play_media onto the configured player. Because the tuner has no
  registry row its own is_hidden/require_control are permissive, so /command now
  re-runs the control boundary on the RESOLVED target — a user denied control of
  the real player can no longer start it through the tuner (privilege escalation).

Wires app.state directly (bypassing the lifespan) like the sibling suites;
rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_state(self, *a, **k):
        pass

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass

    async def publish_raw(self, *a, **k):
        pass


async def _pool():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    return pool


def _wire(pool):
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "boundary-more-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    appmod.app.state.ch = None


async def _mk_user(pool, username, password, role, can_control=True):
    """A user for the boundary tests below. can_control is STATED, not inherited: the
    column now defaults to false (least privilege — migration 0032), while these tests
    are about the OTHER boundaries (hidden entities, the tuner relay, scene targets)
    and their subject is an ordinary user who may control the house EXCEPT where a
    flip rule says otherwise. Inheriting the default would silently turn each of them
    into "a user who can control nothing" and assert the right status codes for
    entirely the wrong reason."""
    await pool.execute("DELETE FROM users WHERE username = $1", username)
    return await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) VALUES ($1, $2, $3, $4) RETURNING id",
        username, await hash_password(password), role, can_control,
    )


async def _login(c, username, password):
    r = await c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username}"


async def test_metadata_list_endpoints_are_admin_only():
    pool = await _pool()
    _wire(pool)
    names = ("zzbndadm", "zzbnduser")
    await pool.execute("DELETE FROM automations WHERE name = 'zzbnd_rule'")
    for n in names:
        await _mk_user(pool, n, "password123", "admin" if n.endswith("adm") else "user")
    uid = await pool.fetchval("SELECT id FROM users WHERE username = 'zzbnduser'")
    # A rule + a push subscription so the admin lists are non-empty (proving the
    # non-admin block hides real content, not just an empty response).
    await pool.execute(
        "INSERT INTO automations (name, enabled, definition) VALUES "
        "('zzbnd_rule', true, $1::jsonb)",
        '{"trigger": {"entity_id": "mqtt:zzbnd_door", "capability": "contact", "to": true}, '
        '"actions": [{"entity_id": "mqtt:zzbnd_siren", "capability": "on_off", "command": "turn_on"}]}',
    )
    await pool.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
        "VALUES ($1, 'https://push.example.test/zzbnd', 'p', 'a')", uid,
    )
    try:
        # --- non-admin: every metadata list is 403
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as nc:
            await _login(nc, "zzbnduser", "password123")
            assert (await nc.get("/automations")).status_code == 403, "non-admin can't list automation rules"
            assert (await nc.get("/automations/runs")).status_code == 403, "non-admin can't list automation runs"
            assert (await nc.get("/notify/targets")).status_code == 403, "non-admin can't list notify targets"

        # --- admin: still sees the full lists (behaviour preserved)
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as ac:
            await _login(ac, "zzbndadm", "password123")
            r = await ac.get("/automations")
            assert r.status_code == 200 and any(a["name"] == "zzbnd_rule" for a in r.json()), "admin lists rules"
            assert (await ac.get("/automations/runs")).status_code == 200, "admin lists runs"
            r = await ac.get("/notify/targets")
            assert r.status_code == 200, "admin lists notify targets"
            assert any(t["entity_id"] == "notify:zzbnduser" for t in r.json()), "the subscribed user is a target"

        # --- unauthenticated: 401
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as anon:
            assert (await anon.get("/automations")).status_code == 401, "anon is refused"
            assert (await anon.get("/notify/targets")).status_code == 401, "anon is refused"
    finally:
        await pool.execute("DELETE FROM push_subscriptions WHERE endpoint = 'https://push.example.test/zzbnd'")
        await pool.execute("DELETE FROM automations WHERE name = 'zzbnd_rule'")
        await pool.execute("DELETE FROM users WHERE username = ANY($1)", list(names))
        await pool.close()


async def test_radio_tuner_relay_respects_target_control_boundary():
    pool = await _pool()
    _wire(pool)
    names = ("zzradadm", "zzraduser")
    target = "heos:zztarget"
    await pool.execute("DELETE FROM entities WHERE entity_id = $1", target)
    for n in names:
        await _mk_user(pool, n, "password123", "admin" if n.endswith("adm") else "user")
    uid = await pool.fetchval("SELECT id FROM users WHERE username = 'zzraduser'")
    # The tuner drives this configured player.
    await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ($1, 'heos')", target)
    await pool.execute(
        "INSERT INTO app_settings (key, value) VALUES ('radio_player', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", target,
    )
    # Deny this user control of the REAL player (baseline can_control=true, a
    # matching flip rule denies it) — but leave radio:tuner ungoverned.
    await pool.execute(
        "INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'control', 'entity', $2)",
        uid, target,
    )
    cmd = {"entity_id": "radio:tuner", "capability": "media_transport", "command": "next"}
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as nc:
            await _login(nc, "zzraduser", "password123")
            # denied on the target → the relay is refused (was: allowed, escalation)
            r = await nc.post("/command", json=cmd)
            assert r.status_code == 403, "control-denied on the target can't relay through radio:tuner"

            # lift the deny → the same user may now drive the tuner (targeted fix,
            # not a blanket block on radio:tuner)
            await pool.execute(
                "DELETE FROM user_access_rules WHERE user_id = $1 AND kind = 'control' AND ref = $2",
                uid, target,
            )
            r = await nc.post("/command", json=cmd)
            assert r.status_code == 200, "with control of the target, the tuner works"

        # admin always bypasses the boundary
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as ac:
            await _login(ac, "zzradadm", "password123")
            assert (await ac.post("/command", json=cmd)).status_code == 200, "admin drives the tuner"
    finally:
        await pool.execute("DELETE FROM entities WHERE entity_id = $1", target)
        await pool.execute("DELETE FROM app_settings WHERE key = 'radio_player'")
        await pool.execute("DELETE FROM users WHERE username = ANY($1)", list(names))
        await pool.close()


async def test_radio_tuner_without_a_player_is_refused():
    pool = await _pool()
    _wire(pool)
    await pool.execute("DELETE FROM app_settings WHERE key = 'radio_player'")
    await _mk_user(pool, "zzradnone", "password123", "admin")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "zzradnone", "password123")
            r = await c.post("/command", json={"entity_id": "radio:tuner",
                                               "capability": "media_transport", "command": "play"})
            assert r.status_code == 409, "a tuner with nowhere to play says so instead of accepting"
    finally:
        await pool.execute("DELETE FROM users WHERE username = 'zzradnone'")
        await pool.close()
