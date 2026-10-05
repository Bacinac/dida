"""Integration tests — the Android companion app's server side (dida_api.mobile)
plus the sliding-session refresh in /auth/me, against a real Postgres.

Covers the three legs the app stands on:
  * /me/mobile-config — session → endpoint-scoped location credentials +
    waypoints (and the fail-loud 503 when DIDA_PUBLIC_URL is loopback);
  * /me/app-link — the browser→app handoff URL, carrying the user's ONE
    reusable login token (never minting over a live one — that killed the QR);
  * /app/apk.json + /app/dida.apk — sideload artifact serving (404 loud when
    the artifact is not mounted, correct bytes + content-type when it is);
  * /me/car-token — session → long-lived bearer credential for DIDA Auto,
    revoked by the same token_version bump as web sessions;
  * /app/location-status — a phone's answer to a wake push, on the location
    credential (a push arrives with no WebView session).

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (tests/run.sh). Lifespan is bypassed — app.state wired directly.
"""
import os
import time

import dida_api.app as appmod
import dida_api.mobile as mobile
import jwt
from dida_api.auth import SESSION_COOKIE, TOKEN_TTL
from dida_api.seed import seed_from_env
from dida_core import apply_migrations, jsonb_init, pg_pool, set_app_setting
from dida_core.db import _setting_cache
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


async def _reseed(pool) -> None:
    """First boot, on demand: drop the settings rows these tests drive through the
    environment and let the seeder write them again. Reading env at runtime is
    exactly what the code no longer does, so a test may not either."""
    from dida_core.db import _setting_cache

    await pool.execute("DELETE FROM app_settings WHERE key IN ('public_url', 'app_url')")
    _setting_cache.clear()
    await seed_from_env(pool)


SECRET = "mobile-test-secret-0123456789abcdef"
PW = "mobilepw12"


async def _cleanup(pool):
    await pool.execute("DELETE FROM zones WHERE name = 'zztest_mobile_zone'")
    await pool.execute("DELETE FROM users WHERE username = 'zzmobuser'")


async def _seed(pool):
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('zzmobuser', $1, 'user')",
        await hash_password(PW),
    )
    await pool.execute(
        "INSERT INTO zones (name, latitude, longitude, radius_m) "
        "VALUES ('zztest_mobile_zone', 45.1, 14.2, 120)",
    )


def _client():
    return AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest")


async def test_mobile_config_provisions_location_credentials():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test"
    await _reseed(pool)
    try:
        async with _client() as c:
            assert (await c.get("/me/mobile-config")).status_code == 401, "session required"

            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200

            r = await c.get("/me/mobile-config")
            assert r.status_code == 200
            cfg = r.json()
            assert cfg["username"] == "zzmobuser"
            assert cfg["user_id"] == (await c.get("/auth/me")).json()["id"]
            assert cfg["url"] == "https://dida.example.test/api/owntracks"
            stored = await pool.fetchval(
                "SELECT owntracks_token FROM users WHERE username = 'zzmobuser'"
            )
            assert cfg["token"] == stored, "token minted on first call and persisted"

            # Idempotent: a second call returns the SAME token (no silent rotation
            # that would strand the previously provisioned phone).
            assert (await c.get("/me/mobile-config")).json()["token"] == stored

            wps = {w["desc"]: w for w in cfg["waypoints"]}
            assert "zztest_mobile_zone" in wps
            assert wps["zztest_mobile_zone"]["rad"] == 120

            # Loopback public address → credentials would arm a reporter that can
            # never reach us from outside. Refused loudly, not handed out. Set it the
            # way an admin would (the stored setting), not through the environment.
            await set_app_setting(pool, "public_url", "http://localhost:5273")
            assert (await c.get("/me/mobile-config")).status_code == 503
    finally:
        os.environ.pop("DIDA_PUBLIC_URL", None)
        await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
        _setting_cache.clear()
        await _cleanup(pool)
        await pool.close()


async def test_app_link_handoff_signs_the_app_in():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    await set_app_setting(pool, "public_url", "https://dida.example.test")
    _setting_cache.clear()
    try:
        async with _client() as c:
            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200

            r = await c.post("/me/app-link")
            assert r.status_code == 200
            url = r.json()["url"]
            assert url.startswith("https://dida.example.test/api/auth/link?k=")
            k = url.split("k=")[1].split("&")[0]

            # Redeem in a FRESH client (the app's own cookie jar): 303 to
            # /onboard + a session cookie of its own.
            async with _client() as fresh:
                r = await fresh.get("/auth/link", params={"k": k, "next": "/onboard"})
                assert r.status_code == 303
                assert r.headers["location"] == "/onboard"
                assert SESSION_COOKIE.name in r.cookies, "handoff logs the app in"

                # Reusable (tokens are no longer one-shot — see /auth/link): a
                # bounced handoff can be retried without minting a new link.
                r = await fresh.get("/auth/link", params={"k": k, "next": "/onboard"})
                assert r.status_code == 303
                assert r.headers["location"] == "/onboard", "retry works"
                assert SESSION_COOKIE.name in r.cookies

            # ONE token per user: the handoff reuses the live login token, so
            # tapping the setup button can never kill the QR the admin is
            # showing (the exact live bug: every tap silently invalidated it).
            r = await c.post("/me/app-link")
            assert r.status_code == 200
            k2 = r.json()["url"].split("k=")[1].split("&")[0]
            assert k2 == k, "app-link must carry the SAME token, not mint a new one"
    finally:
        os.environ.pop("DIDA_PUBLIC_URL", None)
        await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
        _setting_cache.clear()
        await _cleanup(pool)
        await pool.close()


async def test_fcm_token_registration():
    """The Android app registers its FCM token for the signed-in user; re-
    registering the same token under another user MOVES it (handed-over phone)."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('zzmobuser2', $1, 'user')",
        await hash_password(PW),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    try:
        async with _client() as c:
            assert (await c.post("/me/fcm-token", json={"token": "tok-abcdefghijklmno"})).status_code == 401

            await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            r = await c.post("/me/fcm-token", json={"token": "tok-abcdefghijklmno"})
            assert r.status_code == 204
            owner = await pool.fetchval(
                "SELECT u.username FROM fcm_tokens f JOIN users u ON u.id = f.user_id "
                "WHERE f.token = 'tok-abcdefghijklmno'"
            )
            assert owner == "zzmobuser"

            # Same token, different user → moves (ON CONFLICT (token) DO UPDATE).
            async with _client() as c2:
                await c2.post("/auth/login", json={"username": "zzmobuser2", "password": PW})
                assert (await c2.post("/me/fcm-token", json={"token": "tok-abcdefghijklmno"})).status_code == 204
            owner = await pool.fetchval(
                "SELECT u.username FROM fcm_tokens f JOIN users u ON u.id = f.user_id "
                "WHERE f.token = 'tok-abcdefghijklmno'"
            )
            assert owner == "zzmobuser2", "token moved to the user who registered it last"

            assert (await c.post("/me/fcm-token", json={"token": "short"})).status_code == 422
    finally:
        await pool.execute("DELETE FROM fcm_tokens WHERE token = 'tok-abcdefghijklmno'")
        await pool.execute("DELETE FROM users WHERE username IN ('zzmobuser','zzmobuser2')")
        await pool.close()


async def test_apk_artifact_serving(tmp_path, monkeypatch):
    pool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    monkeypatch.setattr(mobile, "_APK_DIR", tmp_path)
    try:
        async with _client() as c:
            # Unauthenticated on purpose: the app's update check has no session.
            assert (await c.get("/app/apk.json")).status_code == 404, "no artifact → loud 404"
            assert (await c.get("/app/dida.apk")).status_code == 404

            (tmp_path / "apk.json").write_text('{"versionCode": 7, "versionName": "v0.1.7"}')
            (tmp_path / "dida.apk").write_bytes(b"not-a-real-apk")

            r = await c.get("/app/apk.json")
            assert r.status_code == 200
            assert r.json()["versionCode"] == 7

            r = await c.get("/app/dida.apk")
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/vnd.android.package-archive"
            assert r.content == b"not-a-real-apk"
            # Versioned + UNIQUE-per-download name: the version is visible, and the
            # random suffix means the browser never prompts "download again?".
            cd1 = r.headers["content-disposition"]
            assert cd1.startswith('attachment; filename="dida-v0.1.7-') and cd1.endswith('.apk"')
            cd2 = (await c.get("/app/dida.apk")).headers["content-disposition"]
            assert cd2 != cd1, "each download gets a distinct filename"
            # no-store: Cloudflare caches .apk by default; stale bytes after a
            # deploy would fail the app's sha256 check.
            assert r.headers["cache-control"] == "no-store"

            # The car app rides the same channel: loud 404 without the artifact
            # (both the APK and its meta — the account page gates its card on the
            # meta), versioned name + no-store with it.
            for stem, meta in (("dida-auto", "auto.json"),):
                assert (await c.get(f"/app/{stem}.apk")).status_code == 404
                assert (await c.get(f"/app/{meta}")).status_code == 404
                (tmp_path / meta).write_text('{"versionCode": 7, "versionName": "v0.1.7"}')
                (tmp_path / f"{stem}.apk").write_bytes(b"not-a-real-" + stem.encode())
                assert (await c.get(f"/app/{meta}")).json()["versionName"] == "v0.1.7"
                r = await c.get(f"/app/{stem}.apk")
                assert r.status_code == 200
                assert r.content == b"not-a-real-" + stem.encode()
                cd = r.headers["content-disposition"]
                assert cd.startswith(f'attachment; filename="{stem}-v0.1.7-') and cd.endswith('.apk"')
                assert r.headers["cache-control"] == "no-store"
    finally:
        await pool.close()


async def test_auth_me_slides_the_session_cookie():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    try:
        async with _client() as c:
            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200

            # Fresh cookie (< half TTL) → /auth/me must NOT re-issue it.
            r = await c.get("/auth/me")
            assert r.status_code == 200
            assert "set-cookie" not in r.headers, "young sessions are left alone"

            # Age the cookie past half TTL (same secret, same user) → renewed.
            uid = await pool.fetchval("SELECT id FROM users WHERE username = 'zzmobuser'")
            old_iat = int(time.time() - (TOKEN_TTL.total_seconds() / 2 + 3600))
            aged = jwt.encode(
                {"sub": str(uid), "iat": old_iat,
                 "exp": int(time.time() + 3600), "tv": 0},
                SECRET, algorithm="HS256",
            )
            c.cookies.set(SESSION_COOKIE.name, aged)
            r = await c.get("/auth/me")
            assert r.status_code == 200
            assert SESSION_COOKIE.name in r.headers.get("set-cookie", ""), "aged session is renewed"
            fresh = r.cookies[SESSION_COOKIE.name]
            claims = jwt.decode(fresh, SECRET, algorithms=["HS256"])
            assert claims["iat"] > old_iat, "renewed cookie carries a fresh iat"
    finally:
        await _cleanup(pool)
        await pool.close()


async def test_car_token_bearer_flow():
    """DIDA Auto's leg: session → /me/car-token → cookie-less bearer access,
    revoked by the same token_version bump that kills web sessions."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    try:
        async with _client() as c:
            assert (await c.post("/me/car-token")).status_code == 401, "session required"

            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200
            r = await c.post("/me/car-token")
            assert r.status_code == 200
            token = r.json()["token"]

            # Long-lived by design (the car cannot re-login interactively): its
            # own multi-year contract, independent of the browser session TTL.
            exp = jwt.decode(token, SECRET, algorithms=["HS256"])["exp"]
            assert exp - time.time() > 5 * 365 * 86400  # at least ~5 years out

            # A fresh client with NO cookie jar: the bearer header alone must
            # authenticate reads and be refused when absent.
            async with _client() as car:
                assert (await car.get("/state")).status_code == 401
                r = await car.get("/state", headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 200

            await pool.execute(
                "UPDATE users SET token_version = token_version + 1 "
                "WHERE username = 'zzmobuser'"
            )
            async with _client() as car:
                r = await car.get("/state", headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 401, "token_version bump revokes the car token"
    finally:
        await _cleanup(pool)
        await pool.close()


async def test_crash_report_lands_on_log_stream():
    """A phone crash uploaded on the next start becomes an ERROR LogRecord on
    the log stream (service = the app's label), rides the same auth as everything
    else, and rejects packages that are not ours."""
    from dida_core import LogRecord

    class StubBus:
        def __init__(self):
            self.published = []

        async def publish_log(self, record):
            self.published.append(record)

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    appmod.app.state.bus = bus
    report = {
        "app": "biz.boskovic.dida.auto",
        "version": "v0.1.751",
        "thread": "main",
        "stack": "java.lang.IllegalStateException: boom\n\tat biz.boskovic.dida.auto.CarService",
        "occurred_at_ms": int(time.time() * 1000) - 60_000,
        "device": "samsung SM-S918B / Android 15",
    }
    try:
        async with _client() as c:
            assert (await c.post("/app/crash-report", json=report)).status_code == 401

            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200
            token = (await c.post("/me/car-token")).json()["token"]

        # Bearer-only client — the path the car apps actually use.
        async with _client() as car:
            auth = {"Authorization": f"Bearer {token}"}
            r = await car.post("/app/crash-report", json=report, headers=auth)
            assert r.status_code == 204
            [record] = bus.published
            assert isinstance(record, LogRecord)
            assert record.service == "app-auto"
            assert record.level == "ERROR"
            assert record.logger == "crash"
            assert "v0.1.751" in record.message
            assert "zzmobuser" in record.message
            assert "IllegalStateException: boom" in record.message
            assert record.exc == report["stack"]
            assert record.ts_ns == report["occurred_at_ms"] * 1_000_000

            # A clock running ahead must not file rows in the future.
            bus.published.clear()
            ahead = dict(report, occurred_at_ms=int(time.time() * 1000) + 3_600_000)
            assert (
                await car.post("/app/crash-report", json=ahead, headers=auth)
            ).status_code == 204
            [record] = bus.published
            assert record.ts_ns <= time.time_ns()

            # Only our own packages may file reports.
            foreign = dict(report, app="com.example.other")
            r = await car.post("/app/crash-report", json=foreign, headers=auth)
            assert r.status_code == 422
    finally:
        await _cleanup(pool)
        await pool.close()


async def test_location_status_reports_why_a_phone_stayed_dark():
    """A wake push asks a silent phone to arm its location engine. Its answer is
    the only way to tell a phone that REFUSED (no background grant) from one
    that never got the message — from the server the two look identical, which
    is how a dead tracker went unnoticed for weeks. Auth is the location token,
    because a push arrives with no session."""
    import base64

    from dida_core import LogRecord

    class StubBus:
        def __init__(self):
            self.published = []

        async def publish_log(self, record):
            self.published.append(record)

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    appmod.app.state.bus = bus
    os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test"
    await _reseed(pool)
    try:
        async with _client() as c:
            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200
            token = (await c.get("/me/mobile-config")).json()["token"]

        creds = base64.b64encode(f"zzmobuser:{token}".encode()).decode()
        auth = {"Authorization": f"Basic {creds}"}
        body = {"outcome": "armed", "version": "v0.1.857", "device": "samsung SM-S721B"}

        async with _client() as phone:
            # No credential at all — this is not an anonymous write path.
            assert (await phone.post("/app/location-status", json=body)).status_code == 401

            wrong = base64.b64encode(b"zzmobuser:not-the-token").decode()
            assert (
                await phone.post("/app/location-status", json=body,
                                 headers={"Authorization": f"Basic {wrong}"})
            ).status_code == 401

            assert (
                await phone.post("/app/location-status", json=body, headers=auth)
            ).status_code == 204
            [record] = bus.published
            assert isinstance(record, LogRecord)
            assert record.service == "app-android"
            assert record.logger == "location"
            assert record.level == "INFO", "arming is the good outcome"
            assert "zzmobuser" in record.message and "armed" in record.message
            assert "v0.1.857" in record.message

            # A refusal must be louder than a success, or it reads as noise in
            # the log viewer and nobody acts on it.
            bus.published.clear()
            refused = dict(body, outcome="no-background-grant")
            assert (
                await phone.post("/app/location-status", json=refused, headers=auth)
            ).status_code == 204
            [record] = bus.published
            assert record.level == "WARNING"
            assert "no-background-grant" in record.message
    finally:
        os.environ.pop("DIDA_PUBLIC_URL", None)
        await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
        _setting_cache.clear()
        await _cleanup(pool)
        await pool.close()


async def test_a_lost_notification_picture_is_reported_by_the_phone():
    """The server mints the frame and sees it fetched, but cannot see which phone
    showed the text without it — the app drops the picture and keeps the message.
    Only the phone knows, so it says so, on the location credential a push has."""
    import base64

    class StubBus:
        def __init__(self):
            self.published = []

        async def publish_log(self, record):
            self.published.append(record)

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    appmod.app.state.bus = bus
    os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test"
    await _reseed(pool)
    try:
        async with _client() as c:
            r = await c.post("/auth/login", json={"username": "zzmobuser", "password": PW})
            assert r.status_code == 200
            token = (await c.get("/me/mobile-config")).json()["token"]

        auth = {"Authorization": "Basic " + base64.b64encode(f"zzmobuser:{token}".encode()).decode()}
        body = {"failure": "SocketTimeoutException: timeout", "version": "v0.1.871",
                "device": "samsung SM-S721B / Android 16"}

        async with _client() as phone:
            assert (await phone.post("/app/push-image", json=body)).status_code == 401
            assert (await phone.post("/app/push-image", json={**body, "failure": ""},
                                     headers=auth)).status_code == 422
            assert (await phone.post("/app/push-image", json=body, headers=auth)).status_code == 204
            [record] = bus.published
            assert (record.service, record.logger, record.level) == ("app-android", "push", "WARNING")
            assert "zzmobuser" in record.message and "SocketTimeoutException" in record.message
            assert "v0.1.871" in record.message and "SM-S721B" in record.message
    finally:
        os.environ.pop("DIDA_PUBLIC_URL", None)
        await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
        _setting_cache.clear()
        await _cleanup(pool)
        await pool.close()
