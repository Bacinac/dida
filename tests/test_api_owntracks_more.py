"""Integration tests — deepening dida_api.owntracks coverage against a real
Postgres. Complements tests/test_api_owntracks.py (the happy location report +
the two auth rejects) by driving the branches it skips: malformed Basic creds,
an empty username/token, the per-IP 429 limiter, the auth-cache eviction, a
non-JSON body, encrypted-frame handling (no key / undecryptable key / a real
libsodium secretbox frame), region transitions (enter + stale leave), lat/lon
validation, the low-accuracy skip, and the provisioning artifact builders
(public_base_url / is_reachable / build_config / config_inline_url / qr_svg /
provision_payload).

Wires app.state directly (bypassing the lifespan) exactly like the sibling
suite. owntracks module globals (_LIMITER / _auth_cache / _wp_pushed / _CACHE_MAX)
are process-wide singletons the conftest limiter reset does NOT touch, so each
block resets/​restores them itself to stay deterministic.
"""
import base64
import json
import os
import time

import dida_api.app as appmod
import dida_api.owntracks as ot
from dida_api.seed import seed_from_env
from dida_core import apply_migrations, encrypt_secret, jsonb_init, pg_pool
from dida_core.db import _setting_cache
from home_core.rate_limit import TokenBucketLimiter
from httpx import ASGITransport, AsyncClient

USER = "zzotmore"
TOKEN = "zzotmore-token-0123456789abcdefABCDEF"
ZONE = "zztest_owntrack_more_zone"


class StubBus:
    def __init__(self):
        self.calls = []
        self.nc = self

    async def flush(self):
        pass

    async def publish_state(self, update):
        self.calls.append(update)

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


def _basic(username: str, token: str) -> dict:
    creds = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


async def _setup_pool():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    return pool


async def _cleanup(pool):
    await pool.execute("DELETE FROM zones WHERE name = $1", ZONE)
    await pool.execute("DELETE FROM users WHERE username = $1", USER)
    await pool.execute("DELETE FROM app_settings WHERE key = 'owntracks_secret'")


def _reset_globals():
    ot._auth_cache.clear()
    ot._wp_pushed.clear()
    ot._LIMITER._buckets.clear()


async def test_owntracks_auth_edges_and_frames():
    pool = await _setup_pool()
    await _cleanup(pool)
    lat, lon = 45.500, 14.500
    await pool.execute(
        "INSERT INTO users (username, password_hash, role, owntracks_token) "
        "VALUES ($1, 'not-a-hash', 'user', $2)", USER, TOKEN,
    )
    await pool.execute(
        "INSERT INTO zones (name, latitude, longitude, radius_m) VALUES ($1, $2, $3, 60)",
        ZONE, lat, lon,
    )
    bus = StubBus()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "owntracks-more-secret-0123456789abcdef"
    appmod.app.state.bus = bus
    _reset_globals()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            # --- undecodable Basic credentials (bad base64) -> 401, no limiter/DB hit
            r = await c.post(
                "/owntracks", json={"_type": "location", "tst": time.time(), "lat": lat, "lon": lon},
                headers={"Authorization": "Basic zzzzz"},  # 5 chars -> b64decode raises
            )
            assert r.status_code == 401, "undecodable Basic creds are rejected"

            # --- well-formed base64 but no ':' (empty token) -> 401
            nocolon = base64.b64encode(b"onlyusername").decode()
            r = await c.post(
                "/owntracks", json={"_type": "location", "tst": time.time(), "lat": lat, "lon": lon},
                headers={"Authorization": f"Basic {nocolon}"},
            )
            assert r.status_code == 401, "creds without a token are rejected"
            assert bus.calls == [], "neither reject reached the bus"

            # --- 429: the per-IP limiter is drained (swap a zero-capacity bucket)
            _reset_globals()
            orig_limiter = ot._LIMITER
            ot._LIMITER = TokenBucketLimiter(capacity=0, refill_per_s=0.0)
            try:
                r = await c.post(
                    "/owntracks", json={"_type": "location", "tst": time.time(), "lat": lat, "lon": lon},
                    headers=_basic(USER, TOKEN),
                )
                assert r.status_code == 429, "a drained limiter returns 429"
                assert r.headers.get("Retry-After") == "15"
            finally:
                ot._LIMITER = orig_limiter

            # --- valid location report AND the auth-cache eviction path
            _reset_globals()
            ot._auth_cache["stale-precached-key"] = ("ghost", time.monotonic() - 1.0)
            orig_max = ot._CACHE_MAX
            ot._CACHE_MAX = 1
            try:
                bus.calls.clear()
                r = await c.post(
                    "/owntracks",
                    json={"_type": "location", "tst": time.time(), "lat": lat, "lon": lon, "acc": 12, "batt": 55},
                    headers=_basic(USER, TOKEN),
                )
                assert r.status_code == 200
                published = {u.capability: u for u in bus.calls}
                assert published["location"].value == ZONE, "the fix resolved to our geofence"
                assert published["battery"].value == 55.0
                assert "stale-precached-key" not in ot._auth_cache, "the expired cache entry was evicted"
                assert len(ot._auth_cache) == 1, "the fresh credential replaced it"
            finally:
                ot._CACHE_MAX = orig_max

            # --- a body that isn't JSON -> 400
            _reset_globals()
            r = await c.post(
                "/owntracks", content=b"this is not json at all",
                headers={**_basic(USER, TOKEN), "Content-Type": "application/json"},
            )
            assert r.status_code == 400, "a non-JSON body is rejected"

            # --- lat/lon missing -> 400
            r = await c.post("/owntracks", json={"_type": "location"}, headers=_basic(USER, TOKEN))
            assert r.status_code == 400, "a location frame without lat/lon is 400"

            # --- lat/lon out of range -> 400
            r = await c.post(
                "/owntracks", json={"_type": "location", "tst": time.time(), "lat": 200.0, "lon": 0.0},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 400, "an out-of-range latitude is 400"

            # --- lat present but non-numeric -> 400
            r = await c.post(
                "/owntracks", json={"_type": "location", "tst": time.time(), "lat": "abc", "lon": 5.0},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 400, "a non-numeric lat is 400"

            # --- a too-coarse fix is acknowledged (200) but not published (fail-loud skip)
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "location", "tst": time.time(), "lat": lat, "lon": lon, "acc": 99999},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200, "a too-coarse fix is acknowledged"
            assert bus.calls == [], "a low-accuracy fix is not published"

            # --- region transitions: an enter latches present, a stale leave is ignored
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "transition", "tst": time.time(), "event": "enter", "desc": ZONE},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200
            assert bus.calls and bus.calls[0].value == ZONE, "an enter transition latches the zone"
            assert bus.calls[0].entity_id == f"presence:{USER}"

            # --- the home waypoint's iOS mode-switch suffix ("Zone|1|2") is stripped
            #     before publishing, so the latched zone is the clean DB name
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "transition", "tst": time.time(), "event": "enter", "desc": f"{ZONE}|1|2"},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200
            assert bus.calls and bus.calls[0].value == ZONE, "the mode-switch suffix is stripped"

            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "transition", "tst": time.time(), "event": "leave", "desc": "Nowhere We Are"},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200
            assert bus.calls == [], "a leave for a zone we're not in is stale and ignored"

            # --- encrypted frame, no OwnTracks key configured -> dropped (acknowledged)
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "encrypted", "data": "irrelevant"},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200 and r.json() == []
            assert bus.calls == [], "an encrypted frame with no key set is dropped"

            # --- encrypted frame, key set but the stored secret is undecryptable -> dropped
            await pool.execute(
                "INSERT INTO app_settings (key, value) VALUES ('owntracks_secret', 'not-a-valid-fernet-token') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            )
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "encrypted", "data": "irrelevant"},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200 and r.json() == []
            assert bus.calls == [], "an undecryptable receiver key drops the frame"

            # --- encrypted frame, a real libsodium secretbox frame -> decrypts + publishes
            from nacl.secret import SecretBox

            passphrase = "familypassphrase"
            await pool.execute(
                "INSERT INTO app_settings (key, value) VALUES ('owntracks_secret', $1) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                encrypt_secret(os.environ["DIDA_SECRET_KEY"], passphrase),
            )
            # receiver key is fine, but the frame itself is corrupt (phone key
            # mismatch) -> the SecretBox open fails -> dropped, not published
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "encrypted", "data": base64.b64encode(b"\x00" * 40).decode()},
                headers=_basic(USER, TOKEN),
            )
            assert r.status_code == 200 and r.json() == []
            assert bus.calls == [], "a frame that fails to open is dropped"

            box = SecretBox(passphrase.encode()[:32].ljust(32, b"\0"))
            inner = json.dumps({"_type": "location", "tst": time.time(), "lat": lat, "lon": lon, "acc": 8, "batt": 42}).encode()
            data = base64.b64encode(bytes(box.encrypt(inner))).decode()
            bus.calls.clear()
            r = await c.post(
                "/owntracks", json={"_type": "encrypted", "data": data}, headers=_basic(USER, TOKEN)
            )
            assert r.status_code == 200
            published = {u.capability: u for u in bus.calls}
            assert published["location"].value == ZONE, "the decrypted fix resolved to our geofence"
            assert published["battery"].value == 42.0
    finally:
        _reset_globals()
        await _cleanup(pool)
        await pool.close()


async def test_owntracks_provisioning_helpers():
    pool = await _setup_pool()
    await _cleanup(pool)
    await pool.execute("DELETE FROM zones WHERE name IN ('zzwp_home', 'zzwp_shop')")
    await pool.execute(
        "INSERT INTO zones (name, latitude, longitude, radius_m) VALUES "
        "('zzwp_home', 45.1, 14.1, 100), ('zzwp_shop', 45.2, 14.2, 40)"
    )
    old = os.environ.get("DIDA_PUBLIC_URL")
    os.environ["DIDA_PUBLIC_URL"] = "https://dida.example.test/"
    await pool.execute("DELETE FROM app_settings WHERE key = 'public_url'")
    _setting_cache.clear()
    await seed_from_env(pool)
    try:
        assert await ot.public_base_url(pool) == "https://dida.example.test", "trailing slash stripped"
        assert ot.is_reachable("https://dida.example.test") is True
        assert ot.is_reachable("http://localhost:5273") is False
        assert ot.is_reachable("not-a-url") is False

        cfg = ot.build_config(
            public_url="https://dida.example.test", username="Marko", token="tok123",
            secret="sekret", waypoints=[{"_type": "waypoint", "desc": "Home"}],
        )
        assert cfg["_type"] == "configuration" and cfg["mode"] == 3
        assert cfg["url"] == "https://dida.example.test/api/owntracks"
        assert cfg["username"] == "Marko" and cfg["password"] == "tok123"
        assert cfg["tid"] == "MA", "tid = first two alnum of the username, upper-cased"
        assert cfg["encryptionKey"] == "sekret" and cfg["waypoints"], "secret + waypoints carried"

        # a username with no alnum chars falls back to a fixed tid, no secret/waypoints
        bare = ot.build_config(public_url="https://x.test", username="___", token="t", secret=None)
        assert bare["tid"] == "ID" and "encryptionKey" not in bare and "waypoints" not in bare

        inline = ot.config_inline_url(cfg)
        assert inline.startswith("owntracks:///config?inline=")
        decoded = json.loads(base64.urlsafe_b64decode(inline.split("inline=", 1)[1]))
        assert decoded == cfg, "the inline payload round-trips back to the config"

        svg = ot.qr_svg(inline)
        assert "<svg" in svg and "</svg>" in svg and "<?xml" not in svg

        payload = await ot.provision_payload(pool, "Marko", "tok123")
        assert payload["username"] == "Marko" and payload["token"] == "tok123"
        assert payload["url"] == "https://dida.example.test/api/owntracks"
        assert payload["reachable"] is True and payload["encrypted"] is False
        assert payload["config"]["waypoints"], "DIDA zones are pushed as phone waypoints"
        # The home zone (if one exists) carries the iOS mode-switch suffix; every
        # other zone's waypoint name stays the clean DB name.
        home = {r["name"] for r in await pool.fetch("SELECT name FROM zones WHERE is_home")}
        for w in await ot.zone_waypoints(pool):
            base = w["desc"].removesuffix("|1|2")
            if base in home:
                assert w["desc"] == base + "|1|2", "home zone carries the iOS mode-switch suffix"
            else:
                assert w["desc"] == base, "non-home zones stay clean"
        assert payload["inline_url"].startswith("owntracks:///config?inline=")
        assert "<svg" in payload["qr_svg"]
    finally:
        if old is None:
            os.environ.pop("DIDA_PUBLIC_URL", None)
        else:
            os.environ["DIDA_PUBLIC_URL"] = old
        await pool.execute("DELETE FROM zones WHERE name IN ('zzwp_home', 'zzwp_shop')")
        await pool.close()
