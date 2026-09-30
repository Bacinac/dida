"""End-to-end server proof of the family onboarding chain — the legs a real
device depends on, verified without one:

  1. an admin-minted setup-QR token (`mint_login_token`) redeemed at
     `/api/auth/link?k=…&next=/` signs the browser in AS THAT USER (auto-login
     under the assigned name, no password) and lands straight in the web app;
  2. the resulting session then provisions location under that same identity via
     `/me/mobile-config` (endpoint-scoped token + the zone as a waypoint);
  3. the QR token is ONE-TIME — a replay lands on the login screen, no cookie.

What this does NOT cover (needs the phone): the on-device permission walkthrough
(fine → background "all the time" → battery → notifications) and the App Link /
intent handoff. Those are OS-driven and are verified on a real device.
"""
import os

import dida_api.app as appmod
from dida_api.auth import SESSION_COOKIE
from dida_api.seed import seed_from_env
from dida_api.users import mint_login_token
from dida_core import apply_migrations, jsonb_init, pg_pool
from dida_core.db import _setting_cache
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

SECRET = "onboard-test-secret-0123456789abc"
NAME = "zzonboardee"


def _client():
    return AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest")


async def _seed(pool) -> int:
    await pool.execute("DELETE FROM users WHERE username = $1", NAME)
    await pool.execute("DELETE FROM zones WHERE name = 'zzonboard_zone'")
    uid = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, 'user') RETURNING id",
        NAME, await hash_password("irrelevant-never-typed"),
    )
    await pool.execute(
        "INSERT INTO zones (name, latitude, longitude, radius_m) "
        "VALUES ('zzonboard_zone', 45.3, 14.5, 150)",
    )
    return uid


async def test_setup_qr_auto_signs_in_as_the_assigned_name_and_provisions_location():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    uid = await _seed(pool)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test"
    await pool.execute("DELETE FROM app_settings WHERE key IN ('public_url', 'app_url')")
    _setting_cache.clear()
    await seed_from_env(pool)

    # The login QR encodes just k=<one-time token> — where it lands is decided by
    # the User-Agent, not a param. A plain browser (iPhone / no app yet) flies
    # straight into the web dashboard.
    token = await mint_login_token(pool, uid)
    try:
        # Do NOT auto-follow: we assert the redirect + Set-Cookie the scan produces.
        async with _client() as c:
            r = await c.get("/auth/link", params={"k": token}, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/", "a browser flies straight into the web"
            assert SESSION_COOKIE.name in r.cookies, "scan signs the browser in (session cookie set)"

            # The session IS the assigned user — no password was ever typed.
            me = await c.get("/auth/me")
            assert me.status_code == 200
            assert me.json()["username"] == NAME

            # …and location provisioning runs under that identity: an endpoint-scoped
            # token plus the zone handed over as a native geofence waypoint.
            cfg = (await c.get("/me/mobile-config")).json()
            assert cfg["username"] == NAME
            assert cfg["token"], "location token minted for this user"
            assert any(w["desc"] == "zzonboard_zone" for w in cfg["waypoints"])

        # The SAME link opened inside the native app (DIDA-App UA) lands on the
        # location walkthrough instead of the dashboard — same token, reusable.
        async with _client() as c3:
            r3 = await c3.get(
                "/auth/link", params={"k": token},
                headers={"user-agent": "Mozilla/5.0 DIDA-App/v0.1.757"},
                follow_redirects=False,
            )
            assert r3.status_code == 303
            assert r3.headers["location"] == "/onboard", "in the app → the walkthrough"
            assert SESSION_COOKIE.name in r3.cookies

        # REUSABLE by design (Marko): onboarding takes several scans in practice, so
        # a re-scan signs in again instead of dead-ending on a login screen. The
        # kill switches are explicit: rotation and revocation.
        async with _client() as c2:
            r2 = await c2.get("/auth/link", params={"k": token}, follow_redirects=False)
            assert r2.status_code == 303
            assert SESSION_COOKIE.name in r2.cookies, "a re-scan signs in again"

        # Rotation kills the old QR: mint a new token → the old value is gone.
        await mint_login_token(pool, uid)
        async with _client() as c4:
            r4 = await c4.get("/auth/link", params={"k": token}, follow_redirects=False)
            assert r4.status_code == 303
            assert SESSION_COOKIE.name not in r4.cookies, "a rotated-away QR gets no session"
    finally:
        os.environ.pop("DIDA_PUBLIC_URL", None)
        await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
        _setting_cache.clear()
        await pool.execute("DELETE FROM zones WHERE name = 'zzonboard_zone'")
        await pool.execute("DELETE FROM users WHERE username = $1", NAME)
        await pool.close()
