"""Integration test — the admin user-management CRUD (dida_api.users) end to end
against a real Postgres: create (+ duplicate 409), list, patch role, and delete
with its lockout guards (no self-delete, no deleting the last admin, 404 for a gone
user). All require_admin.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_users_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('usradmin', 'newbie')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('usradmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "users-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "usradmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # create → 201
        r = await c.post("/users", json={"username": "newbie", "password": "password123", "role": "user"})
        assert r.status_code == 201, "admin creates a user"
        nid = r.json()["id"]
        assert r.json()["username"] == "newbie" and r.json()["role"] == "user", "create echoes the new user"

        # duplicate username → 409
        r = await c.post("/users", json={"username": "newbie", "password": "password123", "role": "user"})
        assert r.status_code == 409, "duplicate username is rejected"

        # list contains the new user
        r = await c.get("/users")
        assert r.status_code == 200 and any(u["username"] == "newbie" for u in r.json()), "new user appears in the list"

        # patch role → admin (204), reflected in the list
        r = await c.patch(f"/users/{nid}", json={"role": "admin"})
        assert r.status_code == 204, "role patch"
        r = await c.get("/users")
        assert next(u for u in r.json() if u["username"] == "newbie")["role"] == "admin", "role update persisted"

        # an admin can't delete their own account (lockout guard) → 400
        my_id = (await c.get("/auth/me")).json()["id"]
        r = await c.delete(f"/users/{my_id}")
        assert r.status_code == 400, "an admin can't delete their own account"

        # delete the created user (now an admin, but usradmin remains → allowed) → 204
        r = await c.delete(f"/users/{nid}")
        assert r.status_code == 204, "delete user"
        r = await c.get("/users")
        assert not any(u["username"] == "newbie" for u in r.json()), "user is gone after delete"

        # deleting an already-gone user → 404
        r = await c.delete(f"/users/{nid}")
        assert r.status_code == 404, "deleting a non-existent user is 404"

    await pool.execute("DELETE FROM users WHERE username IN ('usradmin', 'newbie')")
    await pool.close()


async def test_a_new_user_cannot_control_anything_until_granted():
    """Least privilege by default (migration 0032). This is the security-relevant
    half of user creation: `allowed_pages` scopes what a user SEES, and /command
    never consults it — so if control were on by default, an admin cutting a guest
    down to the /entry page would create a boundary that does not exist, and the
    guest would quietly hold the whole house. The admin grants control deliberately.
    """
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('lpadmin', 'guest')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('lpadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "users-lp-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "lpadmin", "password": "adminpw12"})).status_code == 200
        r = await c.post("/users", json={"username": "guest", "password": "password123", "role": "user"})
        assert r.status_code == 201, "admin creates a guest"

    granted = await pool.fetchval("SELECT can_control FROM users WHERE username = 'guest'")
    assert granted is False, "a freshly created user controls NOTHING until an admin says otherwise"

    await pool.execute("DELETE FROM users WHERE username IN ('lpadmin', 'guest')")
    await pool.close()


async def test_the_wall_panel_states_its_own_control_right():
    """The panel exists to operate the house, so it must not inherit the (now
    least-privilege) column default — it says can_control itself. Without this a
    fresh install would cast a panel that can only look at the house."""
    from dida_api.auth import WALLPANEL_USERNAME, ensure_wallpanel

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username = $1", WALLPANEL_USERNAME)

    await ensure_wallpanel(pool)
    row = await pool.fetchrow(
        "SELECT role, can_control FROM users WHERE username = $1", WALLPANEL_USERNAME
    )
    assert row["can_control"] is True, "the wall panel can operate the house"
    assert row["role"] == "user", "...but is still never an admin"

    await pool.close()
