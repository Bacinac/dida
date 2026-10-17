"""Integration test — Settings → AI (home_core.llm over DIDA's ai_settings) end to
end against a real Postgres: the key goes in sealed and comes back masked, a
model is required for every role, saving without the key keeps the stored one,
Remove really removes it, and each way the assistant can be unready is said in
one sentence. Also the 0099 hand-over: the key the house already had moves from
app_settings to ai_settings with a model per role.

Runs in the api image with DIDA_SECRET_KEY set; the runner supplies the
ephemeral Postgres (see tests/run.sh). Bypasses the lifespan by wiring app.state.
"""
import os
from pathlib import Path

import dida_api.app as appmod
import pytest
from dida_api.ai import ai_settings
from dida_core import apply_migrations, encrypt_secret, jsonb_init, pg_pool
from home_core.auth import hash_password
from home_core.llm import AiNotConfigured
from httpx import ASGITransport, AsyncClient

KEY = "sk-ant-api03-REAL-SECRET-0123456789-wxyz"
MODELS = {"chat": "claude-sonnet-5-5", "author": "claude-opus-5-5"}


async def _setup():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM ai_settings")
    await pool.execute("DELETE FROM users WHERE username IN ('aiadmin', 'aiuser')")
    for name, role in (("aiadmin", "admin"), ("aiuser", "user")):
        await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
                           name, await hash_password("password12"), role)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "ai-settings-test-secret-0123456789abcdef"
    return pool


async def _teardown(pool):
    await pool.execute("DELETE FROM ai_settings")
    await pool.execute("DELETE FROM users WHERE username IN ('aiadmin', 'aiuser')")
    await pool.close()


async def _login(c, username):
    r = await c.post("/auth/login", json={"username": username, "password": "password12"})
    assert r.status_code == 200


async def test_the_key_and_models_round_trip_and_remove_removes():
    pool = await _setup()
    ai = ai_settings(pool)
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "aiadmin")

            r = await c.get("/ai/settings")
            assert r.json() == {"providers": ["anthropic"], "roles": ["chat", "author"], "settings": []}
            with pytest.raises(AiNotConfigured, match="no AI provider is configured"):
                await ai.pick("chat")

            r = await c.put("/ai/settings", json={"provider": "anthropic", "models": MODELS})
            assert (r.status_code, r.json()["detail"]) == (400, "an API key is required for a provider not yet configured")

            r = await c.put("/ai/settings", json={"provider": "anthropic", "api_key": KEY,
                                                  "models": {"chat": "claude-sonnet-5-5"}})
            assert r.status_code == 400 and "author" in r.json()["detail"], "every role needs a model"

            r = await c.put("/ai/settings", json={"provider": "anthropic", "api_key": f"  {KEY}\n", "models": MODELS})
            assert r.status_code == 200
            assert r.json()["api_key_masked"] == "sk-a…wxyz"
            assert KEY not in r.text
            sealed = await pool.fetchval("SELECT api_key_encrypted FROM ai_settings WHERE provider = 'anthropic'")
            assert KEY.encode() not in bytes(sealed), "the key is sealed at rest"
            assert (await ai.pick("author")).model == "claude-opus-5-5"

            r = await c.put("/ai/settings", json={"provider": "anthropic",
                                                  "models": {**MODELS, "chat": "claude-opus-5-5"}})
            assert r.status_code == 200, "a model changes without the key pasted again"
            pick = await ai.pick("chat")
            assert (pick.api_key, pick.model) == (KEY, "claude-opus-5-5")
            assert KEY not in (await c.get("/ai/settings")).text

            r = await c.delete("/ai/settings/anthropic")
            assert r.status_code == 204
            assert await pool.fetchval("SELECT count(*) FROM ai_settings") == 0
            r = await c.delete("/ai/settings/anthropic")
            assert (r.status_code, r.json()["detail"]) == (404, "provider not configured")

            r = await c.post("/automations/1/explain")
            assert (r.status_code, r.json()["detail"]) == (503, "no AI provider is configured — open Settings → AI")
    finally:
        await _teardown(pool)


async def test_only_an_admin_reaches_the_key():
    pool = await _setup()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "aiuser")
            assert (await c.get("/ai/settings")).status_code == 403
            r = await c.put("/ai/settings", json={"provider": "anthropic", "api_key": KEY, "models": MODELS})
            assert r.status_code == 403
            assert (await c.delete("/ai/settings/anthropic")).status_code == 403
    finally:
        await _teardown(pool)


async def test_each_unready_state_is_named():
    pool = await _setup()
    ai = ai_settings(pool)
    try:
        await ai.put("anthropic", api_key=KEY, enabled=True, models={"chat": "claude-sonnet-5-5"})
        with pytest.raises(AiNotConfigured, match="no Anthropic model is chosen for author"):
            await ai.pick("author")

        await pool.execute("UPDATE ai_settings SET api_key_encrypted = $1",
                           encrypt_secret("a-different-secret-key-0123456789ab", KEY).encode())
        with pytest.raises(AiNotConfigured, match="will not decrypt"):
            await ai.pick("chat")

        await pool.execute("UPDATE ai_settings SET enabled = false")
        with pytest.raises(AiNotConfigured, match="no AI provider is configured"):
            await ai.pick("chat")
    finally:
        await _teardown(pool)


async def test_the_key_the_house_had_moves_to_settings_ai():
    pool = await _setup()
    try:
        await pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ('anthropic_api_key', $1), ('openai_api_key', 'x')",
            encrypt_secret(os.environ["DIDA_SECRET_KEY"], KEY),
        )
        await pool.execute(Path("db/migrations/0099_ai_settings.sql").read_text())
        for role, model in MODELS.items():
            pick = await ai_settings(pool).pick(role)
            assert (pick.api_key, pick.model) == (KEY, model)
        assert await pool.fetchval(
            "SELECT count(*) FROM app_settings WHERE key IN ('anthropic_api_key', 'openai_api_key')") == 0
    finally:
        await _teardown(pool)
