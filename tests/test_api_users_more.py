"""Integration tests — deepening dida_api.users coverage against a real Postgres.
Complements tests/test_api_users.py (create/dup/list/patch-role/delete guards) by
driving the branches it skips: role validation, per-user page visibility
(null/subset/unknown/empty/404), can_control, scoped control + view access rules
(dedupe/clear/bad-scope/404), the password-reset session sever, the last-admin
demotion guard, the require_admin/401 boundaries, OwnTracks token revoke, and the
phone-setup access-token lifecycle (mint / reuse / expire / rotate / revoke).

Wires app.state directly (bypassing the lifespan) like the sibling suite;
rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import os

import dida_api.app as appmod
from dida_api.seed import seed_from_env
from dida_core import apply_migrations, jsonb_init, pg_pool
from dida_core.db import _setting_cache
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

GONE = 999_000_111  # a user id no test row will ever occupy


class StubBus:
    async def publish_state(self, *a, **k):
        pass

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _pool():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    return pool


def _wire(pool):
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "users-more-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()


async def _mk_admin(pool, username, password):
    await pool.execute("DELETE FROM users WHERE username = $1", username)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, 'admin')",
        username, await hash_password(password),
    )


async def _login(c, username, password):
    r = await c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username}"


def _find(rows, username):
    return next(u for u in rows if u["username"] == username)


async def test_users_pages_control_and_rules():
    pool = await _pool()
    _wire(pool)
    await pool.execute("DELETE FROM users WHERE username IN ('zzruleadm', 'zzruletgt')")
    await _mk_admin(pool, "zzruleadm", "adminpw123")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "zzruleadm", "adminpw123")

            # create with an unknown role -> 400 (_norm_role)
            r = await c.post("/users", json={"username": "zzruletgt", "password": "password123", "role": "wizard"})
            assert r.status_code == 400, "an unknown role is rejected at create"

            r = await c.post("/users", json={"username": "zzruletgt", "password": "password123", "role": "user"})
            assert r.status_code == 201
            tid = int(r.json()["id"])

            # patch to an unknown role -> 400; role patch on a gone user -> 404
            assert (await c.patch(f"/users/{tid}", json={"role": "wizard"})).status_code == 400
            assert (await c.patch(f"/users/{GONE}", json={"role": "user"})).status_code == 404

            # allowed_pages: unknown key -> 400; empty list -> 400
            assert (await c.patch(f"/users/{tid}", json={"allowed_pages": ["devices", "atlantis"]})).status_code == 400
            assert (await c.patch(f"/users/{tid}", json={"allowed_pages": []})).status_code == 400

            # allowed_pages: a valid subset is stored in canonical PAGE_ORDER
            r = await c.patch(f"/users/{tid}", json={"allowed_pages": ["media", "floorplan", "devices"]})
            assert r.status_code == 204
            row = _find((await c.get("/users")).json(), "zzruletgt")
            assert row["allowed_pages"] == ["floorplan", "devices", "media"], "pages reordered to PAGE_ORDER"

            # allowed_pages: explicit null restores full access (stored NULL)
            assert (await c.patch(f"/users/{tid}", json={"allowed_pages": None})).status_code == 204
            assert _find((await c.get("/users")).json(), "zzruletgt")["allowed_pages"] is None

            # allowed_pages on a gone user -> 404
            assert (await c.patch(f"/users/{GONE}", json={"allowed_pages": ["devices"]})).status_code == 404

            # can_control toggled off, and 404 for a gone user
            assert (await c.patch(f"/users/{tid}", json={"can_control": False})).status_code == 204
            assert _find((await c.get("/users")).json(), "zzruletgt")["can_control"] is False
            assert (await c.patch(f"/users/{GONE}", json={"can_control": True})).status_code == 404

            # control_rules deduped + sorted, view_hides set in the same patch
            r = await c.patch(f"/users/{tid}", json={
                "control_rules": [
                    {"scope": "entity", "ref": "mqtt:lamp"},
                    {"scope": "entity", "ref": "mqtt:lamp"},   # duplicate -> deduped
                    {"scope": "area", "ref": "5"},
                ],
                "view_hides": [{"scope": "capability", "ref": "brightness"}],
            })
            assert r.status_code == 204
            row = _find((await c.get("/users")).json(), "zzruletgt")
            assert row["control_rules"] == [
                {"scope": "area", "ref": "5"}, {"scope": "entity", "ref": "mqtt:lamp"},
            ], "control rules deduped + sorted"
            assert row["view_hides"] == [{"scope": "capability", "ref": "brightness"}]

            # an empty rule list clears that kind
            assert (await c.patch(f"/users/{tid}", json={"control_rules": []})).status_code == 204
            assert _find((await c.get("/users")).json(), "zzruletgt")["control_rules"] == []

            # a bad rule scope -> 400; a valid-scope rule on a gone user -> 404
            assert (await c.patch(f"/users/{tid}",
                                  json={"control_rules": [{"scope": "planet", "ref": "x"}]})).status_code == 400
            assert (await c.patch(f"/users/{GONE}",
                                  json={"control_rules": [{"scope": "entity", "ref": "a"}]})).status_code == 404

            # a password reset bumps token_version (severs live sessions)
            tv0 = await pool.fetchval("SELECT token_version FROM users WHERE id = $1", tid)
            assert (await c.patch(f"/users/{tid}", json={"password": "brandnewpw9"})).status_code == 204
            tv1 = await pool.fetchval("SELECT token_version FROM users WHERE id = $1", tid)
            assert tv1 == tv0 + 1, "password reset revokes sessions"
    finally:
        await pool.execute("DELETE FROM users WHERE username IN ('zzruleadm', 'zzruletgt')")
        await pool.close()


async def test_users_last_admin_and_auth_boundaries():
    pool = await _pool()
    _wire(pool)
    await pool.execute("DELETE FROM users WHERE username IN ('zzsoloadm', 'zzadm2', 'zznonadm')")
    await _mk_admin(pool, "zzsoloadm", "adminpw123")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "zzsoloadm", "adminpw123")
            my_id = int((await c.get("/auth/me")).json()["id"])

            # the sole admin can't demote itself -> 400 (last-admin guard)
            assert (await c.patch(f"/users/{my_id}", json={"role": "user"})).status_code == 400

            # add a second admin; demoting a NON-last admin is allowed
            r = await c.post("/users", json={"username": "zzadm2", "password": "password123", "role": "admin"})
            assert r.status_code == 201
            aid2 = int(r.json()["id"])
            assert (await c.patch(f"/users/{aid2}", json={"role": "user"})).status_code == 204
            assert _find((await c.get("/users")).json(), "zzadm2")["role"] == "user"

            # auth boundary: a non-admin session is refused the admin CRUD (403)
            assert (await c.post("/users", json={"username": "zznonadm", "password": "password123"})).status_code == 201
            async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as nc:
                await _login(nc, "zznonadm", "password123")
                assert (await nc.get("/users")).status_code == 403, "non-admin can't list users"
                assert (await nc.post("/users",
                                      json={"username": "zzx9", "password": "password123"})).status_code == 403
                assert (await nc.delete(f"/users/{aid2}")).status_code == 403

            # no session at all -> 401
            async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as anon:
                assert (await anon.get("/users")).status_code == 401, "an unauthenticated request is 401"
    finally:
        await pool.execute("DELETE FROM users WHERE username IN ('zzsoloadm', 'zzadm2', 'zznonadm')")
        await pool.close()


async def test_users_password_patch_on_gone_user_is_404():
    """A password-only PATCH on a missing user must 404, not silently 204. The
    password branch now checks the affected row count like the role / allowed_pages
    / can_control siblings (previously it ran the UPDATE blind and returned 204)."""
    pool = await _pool()
    _wire(pool)
    await pool.execute("DELETE FROM users WHERE username = 'zzpwadm'")
    await _mk_admin(pool, "zzpwadm", "adminpw123")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "zzpwadm", "adminpw123")
            r = await c.patch(f"/users/{GONE}", json={"password": "brandnewpw9"})
            assert r.status_code == 404, "password reset on a non-existent user is 404"
    finally:
        await pool.execute("DELETE FROM users WHERE username = 'zzpwadm'")
        await pool.close()


async def test_users_owntracks_revoke_and_access_lifecycle():
    pool = await _pool()
    _wire(pool)
    await pool.execute("DELETE FROM users WHERE username IN ('zzaccadm', 'zzacctgt')")
    await _mk_admin(pool, "zzaccadm", "adminpw123")
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "zzaccadm", "adminpw123")
            r = await c.post("/users", json={"username": "zzacctgt", "password": "password123", "role": "user"})
            assert r.status_code == 201
            tid = int(r.json()["id"])

            # --- OwnTracks location-token revoke (+ 404 for a gone user)
            await pool.execute("UPDATE users SET owntracks_token = 'zztok-to-revoke' WHERE id = $1", tid)
            assert (await c.delete(f"/users/{tid}/owntracks")).status_code == 204
            assert await pool.fetchval("SELECT owntracks_token FROM users WHERE id = $1", tid) is None
            assert (await c.delete(f"/users/{GONE}/owntracks")).status_code == 404

            # --- access setup mints a login token on first open
            assert await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid) is None
            r = await c.get(f"/users/{tid}/access")
            assert r.status_code == 200
            body = r.json()
            assert body["username"] == "zzacctgt"
            assert {"url", "public_url", "reachable", "qr_svg"} <= set(body)
            minted = await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid)
            assert minted, "a login token was minted"

            # opening again reuses the still-valid token (no re-mint)
            assert (await c.get(f"/users/{tid}/access")).status_code == 200
            assert await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid) == minted

            # an expired token is re-minted on open
            await pool.execute(
                "UPDATE users SET login_token_expires_at = now() - interval '1 day' WHERE id = $1", tid
            )
            assert (await c.get(f"/users/{tid}/access")).status_code == 200
            reminted = await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid)
            assert reminted and reminted != minted, "an expired token is replaced"

            # access setup on a gone user -> 404
            assert (await c.get(f"/users/{GONE}/access")).status_code == 404

            # --- rotate issues a fresh token; with a public URL the payload carries a QR
            old = os.environ.get("DIDA_PUBLIC_URL")
            os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test"
            await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
            _setting_cache.clear()
            await seed_from_env(pool)
            try:
                r = await c.post(f"/users/{tid}/access")
                assert r.status_code == 200
                pl = r.json()
                assert pl["url"].startswith("https://dida.example.test/api/auth/link?k=")
                assert pl["reachable"] is True and "<svg" in pl["qr_svg"]
            finally:
                if old is None:
                    os.environ.pop("DIDA_PUBLIC_URL", None)
                else:
                    os.environ["DIDA_PUBLIC_URL"] = old
            rotated = await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid)
            assert rotated and rotated != reminted, "rotate mints a new token"
            assert (await c.post(f"/users/{GONE}/access")).status_code == 404

            # --- revoke nulls the token (+ 404 for a gone user)
            assert (await c.delete(f"/users/{tid}/access")).status_code == 204
            assert await pool.fetchval("SELECT login_token FROM users WHERE id = $1", tid) is None
            assert (await c.delete(f"/users/{GONE}/access")).status_code == 404
    finally:
        await pool.execute("DELETE FROM users WHERE username IN ('zzaccadm', 'zzacctgt')")
        await pool.close()
