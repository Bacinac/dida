"""Integration test — admin settings (dida_api.settings) end to end against a
real Postgres: GET reflects the app_settings key/value store (with sane
defaults when a key is absent), PUT writes round-trip through GET for a plain
setting (announce_lang) and a validated one (discovery_subnets — an invalid
CIDR is rejected loud, without clobbering the previously stored value), and a
secret (anthropic_api_key) is never echoed back — only a last-4 hint once
configured, gone once cleared. Also covers the require_admin boundary, and the
uniform /settings/test surface for opus (stubbed upstream, typed OR stored
url+token) and owntracks (freshest received presence fix).

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly — the routes read app.state.pool/.secret_key/.bus.
"""
import time

import dida_api.app as appmod
import dida_api.settings as settingsmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_settings_get_put_roundtrip():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute(
        "DELETE FROM app_settings WHERE key IN ('announce_lang', 'discovery_subnets', 'anthropic_api_key')"
    )
    await pool.execute("DELETE FROM users WHERE username IN ('stgadmin', 'stguser')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('stgadmin', $1, 'admin')", await hash_password("adminpw12")
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('stguser', $1, 'user')", await hash_password("userpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "settings-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "stgadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # a non-admin is refused (require_admin)
        c2 = AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest")
        r = await c2.post("/auth/login", json={"username": "stguser", "password": "userpw12"})
        assert r.status_code == 200, "non-admin login"
        r = await c2.get("/settings")
        assert r.status_code == 403, "a non-admin may not read settings"
        await c2.aclose()

        # GET with no app_settings rows → defaults
        r = await c.get("/settings")
        assert r.status_code == 200
        body = r.json()
        assert body["announce_lang"] == "hr", "no stored/env lang → 'hr' default"
        assert body["discovery_subnets"] == ""
        assert body["anthropic_configured"] is False and body["anthropic_hint"] is None

        # plain setting round-trips
        r = await c.put("/settings", json={"announce_lang": "en"})
        assert r.status_code == 204
        r = await c.get("/settings")
        assert r.json()["announce_lang"] == "en", "announce_lang PUT reflected on GET"

        # discovery_subnets round-trips too
        r = await c.put("/settings", json={"discovery_subnets": "192.0.2.0/24"})
        assert r.status_code == 204
        r = await c.get("/settings")
        assert r.json()["discovery_subnets"] == "192.0.2.0/24"

        # an invalid CIDR is rejected loud, and does NOT clobber the stored value
        r = await c.put("/settings", json={"discovery_subnets": "not-a-cidr"})
        assert r.status_code == 400, "a malformed subnet is rejected"
        r = await c.get("/settings")
        assert r.json()["discovery_subnets"] == "192.0.2.0/24", "the rejected write left the prior value intact"

        # a secret key: write succeeds, GET never echoes it back — only a last-4 hint
        r = await c.put("/settings", json={"anthropic_api_key": "sk-test-abcd1234"})
        assert r.status_code == 204
        r = await c.get("/settings")
        body = r.json()
        assert body["anthropic_configured"] is True
        assert body["anthropic_hint"] == "1234", "only the last 4 chars are ever exposed"
        assert "sk-test-abcd1234" not in r.text, "the raw key is never returned"

        # "" clears it back to unconfigured
        r = await c.put("/settings", json={"anthropic_api_key": ""})
        assert r.status_code == 204
        r = await c.get("/settings")
        body = r.json()
        assert body["anthropic_configured"] is False and body["anthropic_hint"] is None, "clearing removes the key"

    await pool.execute(
        "DELETE FROM app_settings WHERE key IN ('announce_lang', 'discovery_subnets', 'anthropic_api_key')"
    )
    await pool.execute("DELETE FROM users WHERE username IN ('stgadmin', 'stguser')")
    await pool.close()


class _StubResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _StubUpstream:
    """Stands in for httpx.AsyncClient inside dida_api.settings — records the
    last call and serves per-suffix canned responses."""

    last = None
    opus_status = 200

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        _StubUpstream.last = ("get", url, kw)
        if url.endswith("/api/radio/stations"):
            return _StubResponse(status_code=_StubUpstream.opus_status, json_data={"stations": []})
        return _StubResponse(status_code=404)


async def test_settings_test_uniform_surface(monkeypatch):
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute(
        "DELETE FROM app_settings WHERE key IN ('opus_url', 'opus_token')"
    )
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'presence:zztest%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'presence:zztest%'")
    await pool.execute("DELETE FROM users WHERE username = 'stgadmin2'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('stgadmin2', $1, 'admin')", await hash_password("adminpw12")
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "settings-test-secret-0123456789abcdef"
    monkeypatch.setattr(settingsmod.httpx, "AsyncClient", _StubUpstream)

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "stgadmin2", "password": "adminpw12"})
        assert r.status_code == 200

        r = await c.post("/settings/test", json={"provider": "nonsuch"})
        assert r.status_code == 400, "unknown provider rejected loud"

        r = await c.post("/settings/test", json={"provider": "immich"})
        assert r.status_code == 400, "a retired provider is unknown, not silently ok"

        # opus without any url/token → honest "URL nije upisan"
        r = await c.post("/settings/test", json={"provider": "opus"})
        assert r.status_code == 200 and r.json() == {"ok": False, "detail": "URL nije upisan"}

        # typed values validate WITHOUT storing; the upstream sees them verbatim
        _StubUpstream.opus_status = 200
        r = await c.post("/settings/test", json={"provider": "opus", "url": "http://opus.test/", "api_key": "optoken"})
        assert r.json() == {"ok": True, "detail": "radi"}
        _, url, kw = _StubUpstream.last
        assert url == "http://opus.test/api/radio/stations" and kw["headers"]["X-OPUS-Token"] == "optoken"
        _StubUpstream.opus_status = 401
        r = await c.post("/settings/test", json={"provider": "opus", "url": "http://opus.test", "api_key": "bad"})
        assert r.json() == {"ok": False, "detail": "token odbijen"}

        # stored url+token resolve when nothing is typed (the post-save re-test path)
        _StubUpstream.opus_status = 200
        r = await c.put("/settings", json={"opus_url": "http://opus.test", "opus_token": "storedtoken"})
        assert r.status_code == 204
        r = await c.post("/settings/test", json={"provider": "opus"})
        assert r.json() == {"ok": True, "detail": "radi"}
        _, url, kw = _StubUpstream.last
        assert url == "http://opus.test/api/radio/stations"
        assert kw["headers"]["X-OPUS-Token"] == "storedtoken", "stored token decrypted and used"

        # owntracks: no presence data → fail loud; a fresh fix → ok with its age.
        # (The ephemeral pg is shared across the apiint group, so only assert the
        # empty branch when the table really is empty of presence rows.)
        have = await pool.fetchval("SELECT count(*) FROM current_state WHERE entity_id LIKE 'presence:%'")
        r = await c.post("/settings/test", json={"provider": "owntracks"})
        if not have:
            assert r.json()["ok"] is False
        await pool.execute(
            "INSERT INTO entities (entity_id, adapter, capabilities) VALUES ('presence:zztest', 'presence', '[\"occupancy\"]'::jsonb)"
        )
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value, ts_ns) VALUES ('presence:zztest', 'occupancy', 'true'::jsonb, $1)",
            time.time_ns(),
        )
        r = await c.post("/settings/test", json={"provider": "owntracks"})
        assert r.json()["ok"] is True and "prije 0 min" in r.json()["detail"]

    await pool.execute(
        "DELETE FROM app_settings WHERE key IN ('opus_url', 'opus_token')"
    )
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'presence:zztest%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'presence:zztest%'")
    await pool.execute("DELETE FROM users WHERE username = 'stgadmin2'")
    await pool.close()


def test_the_zigbee_token_is_read_from_the_console_own_config(tmp_path, monkeypatch):
    """The admin needs the console's token to open a console DIDA just linked them
    to, and hunting for it in a file on the host is the friction the link was meant
    to remove. It is read from where it LIVES — the console's own config, mounted
    read-only — so rotating it there is the whole of rotating it. Copying it into
    app_settings would make a second place to rotate and forget."""
    import re

    from dida_api.settings import _Z2M_TOKEN

    cfg = (
        "mqtt:\n  server: mqtt://mosquitto:1883\n"
        "frontend:\n  auth_token: s3cr3t-token\n  port: 8080\n"
    )
    assert _Z2M_TOKEN.search(cfg).group(1) == "s3cr3t-token"
    # a console with no token configured is OPEN, not broken — and must not be
    # reported as a token of empty string
    assert _Z2M_TOKEN.search("frontend:\n  port: 8080\n") is None
    assert isinstance(_Z2M_TOKEN, re.Pattern)


async def test_radio_player_is_chosen_in_settings():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    player, lamp = "zzradio:player", "zzradio:lamp"
    await pool.execute("DELETE FROM app_settings WHERE key = 'radio_player'")
    await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1)", [player, lamp])
    await pool.execute("DELETE FROM users WHERE username = 'stgradio'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('stgradio', $1, 'admin')", await hash_password("adminpw12")
    )
    for eid, cap, value in ((player, "media_transport", "stopped"), (lamp, "on_off", True)):
        await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ($1, 'zzradio')", eid)
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value) VALUES ($1, $2, $3)", eid, cap, value
        )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "settings-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            assert (await c.post("/auth/login", json={"username": "stgradio", "password": "adminpw12"})).status_code == 200
            assert (await c.get("/settings")).json()["radio_player"] == ""

            assert (await c.put("/settings", json={"radio_player": player})).status_code == 204
            assert (await c.get("/settings")).json()["radio_player"] == player
            assert await pool.fetchval("SELECT value FROM app_settings WHERE key = 'radio_player'") == player, \
                "stored as the bare entity id the tuner and the rename rewrite read"

            r = await c.put("/settings", json={"radio_player": lamp})
            assert r.status_code == 400, "a lamp cannot play the radio"
            r = await c.put("/settings", json={"radio_player": "zzradio:nothing"})
            assert r.status_code == 400, "an entity that does not exist cannot either"
            assert (await c.get("/settings")).json()["radio_player"] == player, "a refused choice keeps the old one"

            assert (await c.put("/settings", json={"radio_player": ""})).status_code == 204
            assert (await c.get("/settings")).json()["radio_player"] == ""
    finally:
        await pool.execute("DELETE FROM app_settings WHERE key = 'radio_player'")
        await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1)", [player, lamp])
        await pool.execute("DELETE FROM users WHERE username = 'stgradio'")
        await pool.close()
