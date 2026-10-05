"""The SmartThings connect flow — the callback nobody can authenticate.

`/api/smartthings/callback` is the one route in this module with no auth
dependency, and it cannot have one: it is a top-level browser redirect back from
SmartThings, so there is no callback cookie to rely on. Its state resolves the
initiating administrator's session through the database. What it
does on success is write a long-lived refresh token for the owner's SmartThings
account into the database. The signed `state` is therefore not a nicety — it is
the only thing standing between that write and anyone who can reach the tunnel.

The other half is what gets stored. A personal access token dies after 24 hours,
which is why this flow exists at all: the refresh token is the only durable path,
and it is a credential to someone's whole SmartThings account. It has to land
encrypted, and the flow has to refuse to run at all when there is no key to
encrypt it with — writing it in the clear "for now" is the failure that looks
like success.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import jwt
import pytest
from dida_api import smartthings as mod
from dida_api.auth import AuthUser
from dida_api.oauth import _signing_key
from fastapi import HTTPException

SECRET = "test-app-secret-long-enough-for-hs256-x"
KEY = "0123456789abcdef0123456789abcdef"
ADMIN = AuthUser(id=1, username="oauthadmin", role="admin")


class _Pool:
    def __init__(self, cfg=None) -> None:
        self.cfg = cfg if cfg is not None else {}
        self.writes: list[tuple] = []
        self.val = None
        self.states = {}

    async def fetch(self, sql, *a):
        return [{"key": k, "value": v} for k, v in self.cfg.items()]

    async def fetchval(self, sql, *a):
        if "DELETE FROM oauth_states" in sql:
            state = self.states.pop(a[0], None)
            return state[2] if state and (state[1], state[2], state[3]) == a[1:] else None
        return self.val

    async def execute(self, sql, *a):
        if "INSERT INTO oauth_states" in sql:
            self.states[a[0]] = a
            return
        self.writes.append((sql, a))


class _Request:
    def __init__(self, pool) -> None:
        self.cookies = {"dida_session": "initiating-session"}
        self.headers = {}
        self.app = type("A", (), {"state": type("S", (), {
            "pool": pool, "secret_key": SECRET})()})()


class _Resp:
    def __init__(self, status, payload=None, text="") -> None:
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DIDA_SECRET_KEY", KEY)


@pytest.fixture
def configured(monkeypatch):
    """A fully set-up adapter: client creds present, redirect explicit."""
    def _dec(secret, blob, adapter=None, key=None):
        return "client-secret"
    monkeypatch.setattr(mod, "decrypt_secret", _dec)
    return _Pool({"client_id": "cid", "client_secret": "enc",
                  "redirect_uri": "https://dida.example/api/smartthings/callback"})


def _token_post(monkeypatch, resp):
    """Stand in for the code exchange, capturing how the client secret travelled."""
    seen: dict = {}

    class _CX:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, auth=None):
            seen["url"], seen["data"], seen["auth"] = url, data or {}, auth
            return resp
    monkeypatch.setattr(mod.httpx, "AsyncClient", _CX)
    return seen


def _reason(r):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
    return q.get("reason", [""])[0] or q.get("smartthings", [""])[0]


async def _state(pool):
    out = await mod.login(_Request(pool), ADMIN)
    return urllib.parse.parse_qs(urllib.parse.urlsplit(out["url"]).query)["state"][0]


# --- refusing to run without a key to encrypt with ------------------------------


def test_the_flow_refuses_when_there_is_no_encryption_key(monkeypatch):
    """Not a warning and a plaintext write. The refresh token is a credential to
    the owner's whole SmartThings account; storing it readable is worse than not
    connecting at all, and it would look like success."""
    monkeypatch.setenv("DIDA_SECRET_KEY", "")
    with pytest.raises(HTTPException) as e:
        mod._secret()
    assert e.value.status_code == 500


def test_a_whitespace_key_counts_as_no_key(monkeypatch):
    monkeypatch.setenv("DIDA_SECRET_KEY", "   ")
    with pytest.raises(HTTPException):
        mod._secret()


# --- the state is the whole gate ------------------------------------------------


async def test_the_login_state_is_signed_and_expires(configured):
    out = await mod.login(_Request(configured), ADMIN)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(out["url"]).query)
    claims = jwt.decode(q["state"][0], _signing_key(SECRET), algorithms=["HS256"], audience="dida.oauth")
    assert claims["a"] == "smartthings"
    assert claims["sub"] == "1"
    assert len(configured.states) == 1
    assert 0 < claims["exp"] - time.time() <= 600


async def test_two_logins_do_not_reuse_the_state(configured):
    a = await mod.login(_Request(configured), ADMIN)
    b = await mod.login(_Request(configured), ADMIN)
    assert a["url"] != b["url"]


async def test_login_refuses_before_the_adapter_is_configured():
    """A 503 with a sentence, not an authorize URL that fails at SmartThings."""
    with pytest.raises(HTTPException) as e:
        await mod.login(_Request(_Pool({})))
    assert e.value.status_code == 503


@pytest.mark.parametrize("state", [
    "not-a-jwt",
    jwt.encode({"a": "smartthings", "exp": int(time.time()) + 600},
               "wrong-secret-long-enough-for-hs256-too", algorithm="HS256"),
    jwt.encode({"a": "smartthings", "exp": int(time.time()) - 1}, SECRET, algorithm="HS256"),
    jwt.encode({"a": "smartthings", "exp": int(time.time()) + 600}, key="", algorithm="none"),
])
async def test_a_state_that_is_not_ours_writes_no_tokens(configured, monkeypatch, state):
    """Forged, expired, and unsigned. The token exchange must not even be attempted
    — reaching SmartThings with an attacker's `code` is the whole attack."""
    seen = _token_post(monkeypatch, _Resp(200, {"access_token": "a", "refresh_token": "r"}))
    r = await mod.callback(_Request(configured), code="c", state=state)
    assert _reason(r) == "state"
    assert seen == {}, "the code was exchanged before the state was checked"
    assert configured.writes == []


async def test_a_missing_state_is_refused(configured, monkeypatch):
    seen = _token_post(monkeypatch, _Resp(200, {}))
    r = await mod.callback(_Request(configured), code="c", state=None)
    assert _reason(r) == "config"
    assert seen == {}


async def test_smartthings_own_error_is_passed_back(configured):
    r = await mod.callback(_Request(configured), error="access_denied")
    assert _reason(r) == "access_denied"


# --- where the browser lands ----------------------------------------------------


async def test_the_redirect_origin_comes_from_stored_config_not_the_request(configured):
    """Nothing in the callback's query string may influence where the browser is
    sent — otherwise the connect link doubles as an open redirect on the tunnel."""
    r = await mod.callback(_Request(configured), error="x")
    assert r.headers["location"].startswith("https://dida.example/settings/adapters")


async def test_the_landing_origin_matches_the_origin_the_browser_is_already_on(configured):
    """The redirect_uri is the tunnel; sending the browser to a LAN app_url here
    would drop the user on a host their phone cannot reach."""
    origin = await mod._app_origin(configured, "https://tunnel.example/api/smartthings/callback")
    assert origin == "https://tunnel.example"


async def test_a_missing_redirect_falls_back_to_the_configured_app_url(monkeypatch):
    """Only for the error-before-config case — there is no origin to inherit yet."""
    async def _host_setting(pool, key):
        return "https://fallback.example/" if key == "app_url" else ""
    monkeypatch.setattr("dida_core.host_setting", _host_setting)
    assert await mod._app_origin(_Pool(), "") == "https://fallback.example"


# --- what gets stored -----------------------------------------------------------


async def test_the_tokens_are_stored_encrypted(configured, monkeypatch):
    monkeypatch.setattr(mod, "encrypt_secret", lambda secret, blob: f"ENC({blob})")
    _token_post(monkeypatch, _Resp(200, {
        "access_token": "at", "refresh_token": "rt", "expires_in": 86400}))
    r = await mod.callback(_Request(configured), code="c", state=await _state(configured))
    assert _reason(r) == "connected"
    assert len(configured.writes) == 1
    stored = configured.writes[0][1][0]
    assert stored.startswith("ENC("), "the refresh token was written in the clear"


async def test_the_stored_blob_carries_a_refresh_margin(configured, monkeypatch):
    """`expires_at` is set a minute EARLY on purpose: a token treated as valid up to
    its exact expiry means the refresh always races the request that needed it."""
    captured: dict = {}

    def _enc(secret, blob):
        captured.update(json.loads(blob))
        return "enc"
    monkeypatch.setattr(mod, "encrypt_secret", _enc)
    _token_post(monkeypatch, _Resp(200, {
        "access_token": "at", "refresh_token": "rt", "expires_in": 86400}))
    before = time.time()
    await mod.callback(_Request(configured), code="c", state=await _state(configured))
    assert captured["refresh_token"] == "rt"
    # Anchored to a timestamp taken BEFORE the call, and to a real margin: asserting
    # `< time.time() + 86400` after the fact is true with no margin at all, because
    # the clock moved on between the two reads. That version passed the sabotage.
    assert captured["expires_at"] <= before + 86400 - 30


async def test_the_client_secret_travels_as_basic_auth_not_in_the_body(configured, monkeypatch):
    """In the form body it ends up in every proxy access log between here and
    SmartThings; in the Authorization header it does not."""
    monkeypatch.setattr(mod, "encrypt_secret", lambda s, b: "enc")
    seen = _token_post(monkeypatch, _Resp(200, {
        "access_token": "at", "refresh_token": "rt"}))
    await mod.callback(_Request(configured), code="c", state=await _state(configured))
    assert seen["auth"] == ("cid", "client-secret")
    assert "client_secret" not in seen["data"]


async def test_a_failed_exchange_stores_nothing(configured, monkeypatch):
    _token_post(monkeypatch, _Resp(401, {}, text="bad client"))
    r = await mod.callback(_Request(configured), code="c", state=await _state(configured))
    assert _reason(r) == "token"
    assert configured.writes == []


async def test_a_token_response_missing_the_refresh_token_does_not_half_connect(
        configured, monkeypatch):
    """A blob without a refresh token expires in 24 h and the connection dies
    silently — the same end state as never connecting, reached a day later."""
    monkeypatch.setattr(mod, "encrypt_secret", lambda s, b: "enc")
    _token_post(monkeypatch, _Resp(200, {"access_token": "at"}))
    r = await mod.callback(_Request(configured), code="c", state=await _state(configured))
    assert _reason(r) == "token"
    assert configured.writes == []


# --- disconnect -----------------------------------------------------------------


async def test_disconnect_removes_only_the_tokens(configured):
    """Client id and secret survive, or reconnecting means retyping credentials
    that were registered at SmartThings against this exact redirect."""
    await mod.disconnect(_Request(configured))
    sql = configured.writes[0][0]
    assert "DELETE" in sql
    assert "'_oauth'" in sql
    assert "client_id" not in sql


async def test_status_reports_configured_and_connected_separately(configured):
    """"Not working" has two causes with different fixes — no credentials yet, or
    credentials that were never taken through the consent screen."""
    out = await mod.status(_Request(configured))
    assert out == {"configured": True, "connected": False}
    configured.cfg["_oauth"] = "enc"
    assert (await mod.status(_Request(configured)))["connected"] is True


async def test_a_corrupt_locations_setting_does_not_break_the_card(configured):
    configured.val = "{not json"
    assert await mod.locations(_Request(configured)) == {"locations": []}
