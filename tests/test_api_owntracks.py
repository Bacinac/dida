"""Integration test — the OwnTracks HTTP receiver (dida_api.owntracks) end to
end against a real Postgres: HTTP Basic auth against `users.owntracks_token`
(a plaintext per-user location token, NOT the login password — compared with
secrets.compare_digest, never Fernet-decrypted, unlike the separate encrypted-
frame passphrase), a location frame resolving to a geofence and being published
on the bus, and the auth-reject paths (missing header, wrong token).

This endpoint bypasses the login/JWT stack entirely — no /auth/login here, it's
driven straight off Basic creds — so only app.state.pool/.bus need wiring.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. The receiver's own auth rate limiter (module-level in
dida_api.owntracks, capacity 5, separate from rate_limit.LOGIN and never reset
between tests) is why this test only makes a handful of Basic-auth attempts.
"""
import base64

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from httpx import ASGITransport, AsyncClient


class StubBus:
    """publish_state is what dida_api.presence.publish_report calls (via
    app.state.bus) for every capability facet of a report — record each call so
    the test can assert what a location report resolved to without a real
    NATS/engine round trip."""

    def __init__(self):
        self.calls = []

    async def publish_state(self, update):
        self.calls.append(update)

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


def _basic(username: str, token: str) -> dict:
    creds = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


async def _cleanup(pool):
    await pool.execute("DELETE FROM zones WHERE name = 'zztest_owntrack_zone'")
    await pool.execute("DELETE FROM users WHERE username = 'zzotuser'")


async def test_owntracks_location_report():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    token = "zzot-test-token-0123456789abcdef"
    await pool.execute(
        "INSERT INTO users (username, password_hash, role, owntracks_token) VALUES ('zzotuser', $1, 'user', $2)",
        "not-a-real-hash", token,
    )
    # A geofence sized around the reported fix, so the resolved zone is
    # deterministic regardless of any other zones already in the database.
    lat, lon = 45.327, 14.442
    await pool.execute(
        "INSERT INTO zones (name, latitude, longitude, radius_m) VALUES ('zztest_owntrack_zone', $1, $2, 50)",
        lat, lon,
    )

    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "owntracks-test-secret-0123456789abcdef"
    appmod.app.state.bus = bus

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # no Authorization header at all -> 401, no limiter/DB hit
        r = await c.post("/owntracks", json={"_type": "location", "lat": lat, "lon": lon})
        assert r.status_code == 401, "a missing Authorization header is rejected"

        # well-formed Basic creds, wrong token -> 401, nothing published
        r = await c.post(
            "/owntracks", json={"_type": "location", "lat": lat, "lon": lon},
            headers=_basic("zzotuser", "wrong-token"),
        )
        assert r.status_code == 401, "a bad location token is rejected"
        assert bus.calls == [], "a rejected report never reaches the bus"

        # valid Basic creds + a minimal OwnTracks location frame -> accepted
        r = await c.post(
            "/owntracks",
            json={"_type": "location", "lat": lat, "lon": lon, "tst": 1735689600, "acc": 10, "batt": 87},
            headers=_basic("zzotuser", token),
        )
        assert r.status_code == 200, "a valid location report is accepted"
        assert isinstance(r.json(), list), "OwnTracks expects a JSON array reply"

        published = {u.capability: u for u in bus.calls}
        assert published["location"].entity_id == "presence:zzotuser"
        assert published["location"].value == "zztest_owntrack_zone", "the fix resolved to our geofence"
        assert published["latitude"].value == round(lat, 6)
        assert published["longitude"].value == round(lon, 6)
        assert published["battery"].value == 87.0

        # username matching is case-insensitive (phone keyboards auto-capitalize)
        bus.calls.clear()
        r = await c.post(
            "/owntracks", json={"_type": "location", "lat": lat, "lon": lon},
            headers=_basic("ZZOtUser", token),
        )
        assert r.status_code == 200, "username match is case-insensitive"
        assert bus.calls[0].entity_id == "presence:zzotuser", "resolves to the canonical stored username"

        # a non-location frame (e.g. a phone's periodic 'waypoints' dump) is
        # acknowledged, not published
        bus.calls.clear()
        r = await c.post("/owntracks", json={"_type": "waypoints"}, headers=_basic("zzotuser", token))
        assert r.status_code == 200
        assert bus.calls == [], "a non-location frame is acknowledged, never published"

    await _cleanup(pool)
    await pool.close()
