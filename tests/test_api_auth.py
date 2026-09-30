"""Integration test — the auth HTTP surface end to end against a real Postgres:
login (argon2 verify + JWT cookie mint), the cookie-authenticated /auth/me, and
the require_admin boundary (a non-admin is 403'd off an admin route). Drives the
REAL FastAPI app + a real asyncpg pool (stub bus); the runner supplies the
ephemeral Postgres (see tests/run.sh "integration").

Runs in the api image (dida_api + httpx). It BYPASSES the app lifespan — which
would need NATS + ClickHouse — by wiring app.state directly: the routes read
app.state.pool / .secret_key, so that is all they need.
"""
import dida_api.app as appmod
from dida_api.auth import SESSION_COOKIE
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_auth_http_surface():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('itadmin', 'itbob')")  # idempotent re-runs
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, 'admin')", "itadmin", await hash_password("admin-pw")
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, 'user')", "itbob", await hash_password("bob-pw")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "integration-test-secret-key-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # wrong password → vague 401 (never says which of user/pass was wrong)
        r = await c.post("/auth/login", json={"username": "itadmin", "password": "nope"})
        assert r.status_code == 401, "wrong password rejected 401"
        # unknown username → the SAME 401 (constant-time dummy-verify path, no enumeration oracle)
        r = await c.post("/auth/login", json={"username": "ghost", "password": "x"})
        assert r.status_code == 401, "unknown username rejected 401"
        # a protected route with no cookie → 401
        r = await c.get("/auth/me")
        assert r.status_code == 401, "/auth/me without a cookie → 401"

        # admin login → 200 and a session cookie
        r = await c.post("/auth/login", json={"username": "itadmin", "password": "admin-pw"})
        assert r.status_code == 200 and r.json()["role"] == "admin", "admin login → 200"
        assert SESSION_COOKIE.name in c.cookies, "login set the session cookie"
        # the cookie now authenticates /auth/me
        r = await c.get("/auth/me")
        assert r.status_code == 200 and r.json()["username"] == "itadmin", "/auth/me returns the logged-in admin"
        # admin may reach an admin-only route
        r = await c.get("/users")
        assert r.status_code == 200, "admin allowed on /users (require_admin)"

        # switch identities cleanly, then log in as the non-admin
        c.cookies.clear()
        r = await c.post("/auth/login", json={"username": "itbob", "password": "bob-pw"})
        assert r.status_code == 200 and r.json()["role"] == "user", "non-admin login → 200"
        r = await c.get("/auth/me")
        assert r.status_code == 200 and r.json()["username"] == "itbob", "/auth/me returns the non-admin"
        # the require_admin boundary blocks the non-admin — the server-side gate, not the nav
        r = await c.get("/users")
        assert r.status_code == 403, "non-admin blocked from the admin route (require_admin)"

        # --- UI preferences (theme + language) round-trip (self-service) ----
        r = await c.get("/auth/me")
        assert r.json()["theme"] is None and r.json()["locale"] is None, "fresh user: no stored prefs"
        # partial write: only the theme — locale stays untouched
        r = await c.patch("/auth/prefs", json={"theme": "light"})
        assert r.status_code == 204, "theme-only prefs write accepted"
        r = await c.get("/auth/me")
        assert r.json()["theme"] == "light" and r.json()["locale"] is None, "theme saved, locale untouched"
        # the other field independently
        r = await c.patch("/auth/prefs", json={"locale": "en"})
        assert r.status_code == 204, "locale-only prefs write accepted"
        r = await c.get("/auth/me")
        assert r.json()["theme"] == "light" and r.json()["locale"] == "en", "locale saved, theme kept"
        # both at once; a fresh login echoes the stored prefs (apply-on-login path)
        r = await c.patch("/auth/prefs", json={"theme": "system", "locale": "hr"})
        assert r.status_code == 204
        c.cookies.clear()
        r = await c.post("/auth/login", json={"username": "itbob", "password": "bob-pw"})
        assert r.json()["theme"] == "system" and r.json()["locale"] == "hr", "login returns saved prefs"
        # an unknown value is rejected by the Literal validator, an empty body is a no-op
        r = await c.patch("/auth/prefs", json={"theme": "neon"})
        assert r.status_code == 422, "unknown theme rejected"
        r = await c.patch("/auth/prefs", json={})
        assert r.status_code == 204, "empty prefs body is a harmless no-op"
        # and the route is session-gated
        c.cookies.clear()
        r = await c.patch("/auth/prefs", json={"theme": "dark"})
        assert r.status_code == 401, "prefs require a session"

    await pool.execute("DELETE FROM users WHERE username IN ('itadmin', 'itbob')")
    await pool.close()


class FakeSocket:
    def __init__(self, headers: dict[str, str], token: str | None = None):
        self.headers = headers
        self.cookies = {SESSION_COOKIE.name: token} if token else {}
        self.events: list[object] = []

    async def accept(self):
        self.events.append("accept")

    async def close(self, code: int = 1000):
        self.events.append(code)


async def test_signing_out_ends_that_session_and_no_other():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username = 'itcarol'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, 'user')", "itcarol", await hash_password("carol-pw")
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "integration-test-secret-key-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    transport = ASGITransport(app=appmod.app)
    async with AsyncClient(transport=transport, base_url="http://itest") as laptop, \
            AsyncClient(transport=transport, base_url="http://itest") as phone:
        for device in (laptop, phone):
            r = await device.post("/auth/login", json={"username": "itcarol", "password": "carol-pw"})
            assert r.status_code == 200
        signed_out = laptop.cookies[SESSION_COOKIE.name]

        r = await laptop.post("/auth/logout")
        assert r.status_code == 204 and SESSION_COOKIE.name not in laptop.cookies
        r = await laptop.get("/auth/me", headers={"Authorization": f"Bearer {signed_out}"})
        assert r.status_code == 401, "a signed-out token replayed is refused"
        r = await phone.get("/auth/me")
        assert r.status_code == 200, "the phone stays signed in"

        r = await laptop.post("/auth/logout", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 204, "signing out a broken session still clears the cookie"

        phone_token = phone.cookies[SESSION_COOKIE.name]
        foreign = FakeSocket({"origin": "https://evil.example", "host": "api:8090",
                              "x-forwarded-host": "dida.example"}, phone_token)
        assert await appmod._authenticate_ws(foreign) is None
        assert foreign.events == [4403], "another site's page is refused at the handshake"

        own = FakeSocket({"origin": "https://dida.example", "host": "api:8090",
                          "x-forwarded-host": "dida.example"}, phone_token)
        user = await appmod._authenticate_ws(own)
        assert user is not None and user.username == "itcarol" and own.events == []

        headless = FakeSocket({"host": "api:8090"}, phone_token)
        assert await appmod._authenticate_ws(headless) is not None

        revoked = FakeSocket({"origin": "https://dida.example", "host": "api:8090",
                              "x-forwarded-host": "dida.example"}, signed_out)
        assert await appmod._authenticate_ws(revoked) is None
        assert revoked.events == ["accept", 4401], "the browser must see 4401 to go to sign-in"

    await pool.execute("DELETE FROM users WHERE username = 'itcarol'")
    await pool.close()


async def test_the_api_names_itself_on_version():
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.get("/version")
    assert r.status_code == 200 and r.json()["product"] == "dida" and "version" in r.json()
