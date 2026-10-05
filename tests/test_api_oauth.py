import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import dida_api.app as appmod
import jwt
import pytest
from dida_api import contacts, oauth, smartthings
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def oauth_flow(monkeypatch):
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM adapter_config WHERE adapter IN ('contacts', 'smartthings') AND key = '_oauth'")
    await pool.execute("DELETE FROM users WHERE username IN ('oauthadmin', 'oauthguest')")
    for username, role in (("oauthadmin", "admin"), ("oauthguest", "user")):
        await pool.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
            username, await hash_password("oauthpw123"), role,
        )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "oauth-test-secret-0123456789abcdef"
    exchanges = []
    writes = []

    def configure(integration):
        async def cfg(request):
            return {"client_id": "client", "client_secret": "secret", "connected": False,
                    "redirect_uri": f"https://dida.example/api/{integration}/callback"}
        return cfg

    class TokenClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            exchanges.append((url, kwargs))
            return type("TokenResponse", (), {
                "status_code": 200,
                "json": lambda self: {"refresh_token": "refresh", "access_token": "access"},
            })()

    for integration, module in (("contacts", contacts), ("smartthings", smartthings)):
        monkeypatch.setattr(module, "_cfg", configure(integration))
        monkeypatch.setattr(module, "encrypt_secret", lambda secret, blob: writes.append(json.loads(blob)) or "encrypted")
    monkeypatch.setenv("DIDA_SECRET_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(smartthings.httpx, "AsyncClient", TokenClient)
    yield pool, exchanges, writes
    await pool.execute("DELETE FROM adapter_config WHERE adapter IN ('contacts', 'smartthings') AND key = '_oauth'")
    await pool.execute("DELETE FROM users WHERE username IN ('oauthadmin', 'oauthguest')")
    await pool.close()


async def _login(client, username="oauthadmin"):
    assert (await client.post("/auth/login", json={"username": username, "password": "oauthpw123"})).status_code == 200


async def _state(client, integration):
    response = await client.get(f"/{integration}/login")
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["url"]).query)["state"][0]


def _result(response, integration):
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query.get("reason", query.get(integration, [""]))[0]


@pytest.mark.parametrize("integration", ["contacts", "smartthings"])
async def test_session_tokens_never_authorize_oauth(oauth_flow, integration):
    _, exchanges, writes = oauth_flow
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        for username in ("oauthguest", "oauthadmin"):
            client.cookies.clear()
            await _login(client, username)
            token = (await client.post("/me/car-token")).json()["token"]
            response = await client.get(f"/{integration}/callback", params={"code": "code", "state": token})
            assert _result(response, integration) == "state"
        assert exchanges == [] and writes == []


@pytest.mark.parametrize("integration", ["contacts", "smartthings"])
async def test_oauth_is_integration_scoped_and_consumed_once(oauth_flow, integration):
    pool, exchanges, writes = oauth_flow
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        await _login(client, "oauthguest")
        assert (await client.get(f"/{integration}/login")).status_code == 403
        client.cookies.clear()
        await _login(client)
        state = await _state(client, integration)
        other = "contacts" if integration == "smartthings" else "smartthings"
        response = await client.get(f"/{other}/callback", params={"code": "code", "state": state})
        assert _result(response, other) == "state"
        assert exchanges == [] and writes == []
        client.cookies.clear()
        responses = await asyncio.gather(*[
            client.get(f"/{integration}/callback", params={"code": "code", "state": state})
            for _ in range(2)
        ])
        assert sorted(_result(r, integration) for r in responses) == ["connected", "state"]
        assert len(exchanges) == len(writes) == 1
        assert await pool.fetchval("SELECT count(*) FROM oauth_states WHERE integration = $1", integration) == 0
        response = await client.get(f"/{integration}/callback", params={"code": "code", "state": state})
        assert _result(response, integration) == "state"
        assert len(exchanges) == 1


@pytest.mark.parametrize("integration", ["contacts", "smartthings"])
@pytest.mark.parametrize("revocation", ["logout", "password", "demote", "delete", "expired", "missing"])
async def test_oauth_rechecks_the_initiating_principal(oauth_flow, integration, revocation):
    pool, exchanges, writes = oauth_flow
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        await _login(client)
        state = await _state(client, integration)
        if revocation == "logout":
            assert (await client.post("/auth/logout")).status_code == 204
        elif revocation == "password":
            await pool.execute("UPDATE users SET token_version = token_version + 1 WHERE username = 'oauthadmin'")
        elif revocation == "demote":
            await pool.execute("UPDATE users SET role = 'user' WHERE username = 'oauthadmin'")
        elif revocation == "delete":
            await pool.execute("DELETE FROM users WHERE username = 'oauthadmin'")
        elif revocation == "expired":
            await pool.execute("UPDATE oauth_states SET expires_at = now() - interval '1 second'")
        else:
            await pool.execute("DELETE FROM oauth_states")
        response = await client.get(f"/{integration}/callback", params={"code": "code", "state": state})
        assert _result(response, integration) == "state"
        assert exchanges == [] and writes == []


@pytest.mark.parametrize("claim,value", [("a", "other"), ("n", ""), ("n", None), ("tv", True), ("sub", "bad"), ("exp", 1), ("aud", "session")])
async def test_oauth_rejects_invalid_claims(oauth_flow, claim, value):
    _, exchanges, writes = oauth_flow
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        await _login(client)
        state = await _state(client, "contacts")
        key = oauth._signing_key(appmod.app.state.secret_key)
        claims = jwt.decode(state, key, algorithms=["HS256"], audience="dida.oauth")
        claims[claim] = value
        state = jwt.encode(claims, key, algorithm="HS256")
        response = await client.get("/contacts/callback", params={"code": "code", "state": state})
        assert _result(response, "contacts") == "state"
        assert exchanges == [] and writes == []


@pytest.mark.parametrize("claim", ["aud", "a", "n", "sub", "tv", "iat", "exp"])
async def test_oauth_requires_every_flow_claim(oauth_flow, claim):
    _, exchanges, writes = oauth_flow
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        await _login(client)
        state = await _state(client, "contacts")
        key = oauth._signing_key(appmod.app.state.secret_key)
        claims = jwt.decode(state, key, algorithms=["HS256"], audience="dida.oauth")
        del claims[claim]
        response = await client.get("/contacts/callback", params={
            "code": "code", "state": jwt.encode(claims, key, algorithm="HS256"),
        })
        assert _result(response, "contacts") == "state"
        assert exchanges == [] and writes == []
