"""Integration test — the adapter-config surface (dida_api.adapters) end to end
against a real Postgres: config-schema listing with the live per-adapter status
fan-out (bus.nc.request — both the "answered" and the "not answering, fail-loud
None" paths), and the PUT config round-trip (plain field upsert/clear, secret
field encrypt-at-rest / masked-in-response / untouched-on-blank). All require_admin.
/discover-all is covered against a stubbed bus (which adapter answered, which did
not and why); the adapters' own network scans are out of scope.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. DIDA_SECRET_KEY is set in the gate env (adapter secrets are
Fernet-encrypted against it). rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import json
import os
from types import SimpleNamespace

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool, status_subject
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class _StubNC:
    """Only 'mqtt' answers its status subject; every other adapter's request
    times out — exercises both branches _adapter_status fans out across the
    whole config schema (answered vs. not-answering/None, never a fake 'ok')."""

    async def publish(self, subject, payload):
        pass

    async def request(self, subject, payload=b"", *, timeout=None):
        if subject == status_subject("mqtt"):
            return SimpleNamespace(data=json.dumps({"state": "ok", "detail": "", "since": 0}).encode())
        raise TimeoutError("no responder")


class StubBus:
    def __init__(self):
        self.nc = _StubNC()

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _cleanup(pool):
    await pool.execute("DELETE FROM adapter_config WHERE adapter = 'mqtt'")
    await pool.execute("DELETE FROM users WHERE username = 'adpadmin'")


async def test_adapter_config_crud():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('adpadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "adapters-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "adpadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # list: every UI-configurable adapter appears, mqtt's fields start at
        # their defaults/unconfigured, live status fanned out concurrently
        r = await c.get("/adapters")
        assert r.status_code == 200
        rows = {row["adapter"]: row for row in r.json()}
        assert "mqtt" in rows, "mqtt is in the config schema"
        mqtt_fields = {f["key"]: f for f in rows["mqtt"]["fields"]}
        assert mqtt_fields["mqtt_url"]["value"] == "" and not mqtt_fields["mqtt_url"]["configured"], (
            "unconfigured plain field starts blank"
        )
        assert mqtt_fields["password"]["secret"] is True
        assert mqtt_fields["password"]["value"] == "", "a secret field's value never leaves the server"
        assert rows["mqtt"]["status"] == {"state": "ok", "detail": "", "since": 0}, "mqtt answered its status subject"
        assert rows["ecowitt"]["status"] is None, "an adapter that doesn't respond reports no status, not a guess"

        # put config: plain + secret fields together
        r = await c.put(
            "/adapters/mqtt/config",
            json={
                "values": {
                    "mqtt_url": "mqtt://test-broker:1883",
                    "username": "testuser",
                    "password": "supersecret123",
                    "topic_prefix": "zztest",
                }
            },
        )
        assert r.status_code == 204

        r = await c.get("/adapters")
        mqtt_fields = {f["key"]: f for f in next(row for row in r.json() if row["adapter"] == "mqtt")["fields"]}
        assert mqtt_fields["mqtt_url"]["value"] == "mqtt://test-broker:1883", "plain field value is reflected back"
        assert mqtt_fields["mqtt_url"]["configured"] is True
        assert mqtt_fields["topic_prefix"]["value"] == "zztest"
        assert mqtt_fields["password"]["configured"] is True, "secret is marked configured"
        assert mqtt_fields["password"]["value"] == "", "…but still never shown, even after being set"
        stored = await pool.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = 'mqtt' AND key = 'password'"
        )
        assert stored != "supersecret123", "the secret is encrypted at rest, not stored raw"

        # a blank secret on a later PUT leaves the stored value untouched
        r = await c.put("/adapters/mqtt/config", json={"values": {"password": ""}})
        assert r.status_code == 204
        still = await pool.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = 'mqtt' AND key = 'password'"
        )
        assert still == stored, "a blank secret field doesn't clear the stored secret"

        # a blank plain field clears it (falls back to the field default)
        r = await c.put("/adapters/mqtt/config", json={"values": {"mqtt_url": ""}})
        assert r.status_code == 204
        r = await c.get("/adapters")
        mqtt_fields = {f["key"]: f for f in next(row for row in r.json() if row["adapter"] == "mqtt")["fields"]}
        assert mqtt_fields["mqtt_url"]["configured"] is False, "a blank plain field clears it"
        assert mqtt_fields["mqtt_url"]["value"] == "", "…and falls back to the field default"

        # unknown adapter -> 404
        r = await c.put("/adapters/zzbogus/config", json={"values": {"x": "y"}})
        assert r.status_code == 404, "an unknown adapter is rejected"

    await _cleanup(pool)
    await pool.close()


async def test_baba_location_crud_and_camera_cleanup():
    """The location editor, end to end: add two BABA installs, read them back
    with the secrets masked, then remove one and prove its cameras go with it.

    That last part is the whole reason removal exists, and it is easy to get
    silently wrong: a camera descriptor is published as a JSON *string*, so
    `value->>'site'` on the jsonb column reads NULL and the delete matches
    nothing while still reporting success. The other location's camera must
    survive untouched — deleting across installs is the failure this endpoint
    is meant to make impossible."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")

    async def clean():
        await pool.execute("DELETE FROM adapter_config WHERE adapter = 'baba'")
        await pool.execute("DELETE FROM users WHERE username = 'babaadmin'")
        await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'baba:loc-%'")
        await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'baba:loc-%'")

    await clean()
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('babaadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "adapters-sites-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "babaadmin", "password": "adminpw12"})
        assert r.status_code == 200

        for name, host, key in (("Kuća", "198.51.100.11", "HOMEKEY"), ("Cabin", "cabin.example", "FARKEY")):
            r = await c.put(
                f"/adapters/baba/sites/{name}",
                json={"name": name, "nats_url": f"nats://{host}:4222",
                      "go2rtc": f"http://{host}:11984", "go2rtc_user": "baba",
                      "go2rtc_password": "g2pw", "api_url": f"http://{host}:8080",
                      "peer_key": key},
            )
            assert r.status_code == 200, f"added {name}"

        sites = {s["name"]: s for s in (await c.get("/adapters/baba/sites")).json()["sites"]}
        assert set(sites) == {"Kuća", "Cabin"}
        assert sites["Cabin"]["has_peer_key"] and sites["Cabin"]["has_go2rtc_password"]
        assert "peer_key" not in sites["Cabin"], "a secret never leaves the server, not even to an admin"
        stored = await pool.fetchval("SELECT value FROM adapter_config WHERE adapter='baba' AND key='sites'")
        assert "FARKEY" not in stored, "the location list is encrypted at rest"

        # one camera per location, published the way the adapter publishes them:
        # the descriptor is a JSON document stored as a jsonb STRING
        for eid, site in (("baba:loc-home-cam", "Kuća"), ("baba:loc-far-cam", "Cabin")):
            await pool.execute(
                "INSERT INTO entities (entity_id, adapter, device_key) VALUES ($1, 'baba', $1) "
                "ON CONFLICT (entity_id) DO NOTHING", eid)
            await pool.execute(
                "INSERT INTO current_state (entity_id, capability, value, updated_at) "
                "VALUES ($1, 'camera', to_jsonb($2::text), now()) "
                "ON CONFLICT (entity_id, capability) DO UPDATE SET value = EXCLUDED.value",
                eid, json.dumps({"stream": "x", "site": site}))

        r = await c.delete("/adapters/baba/sites/cabin")
        assert r.status_code == 200
        assert r.json()["removed_cameras"] == 1, "the removed location's camera went with it"

        left = [row["name"] for row in (await c.get("/adapters/baba/sites")).json()["sites"]]
        assert left == ["Kuća"]
        survivors = [r["entity_id"] for r in await pool.fetch(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'baba:loc-%'")]
        assert survivors == ["baba:loc-home-cam"], "the other location's camera is untouched"

    await clean()
    await pool.close()


async def test_baba_nats_credentials_have_their_own_masked_fields():
    """A NATS password inside the URL came back to the browser in the location
    form and, with no name given, became the location's name. It is refused in
    the URL, stored in its own field and masked; a stored URL that still carries
    one is lifted out at boot byte for byte, since nats-py sends it undecoded."""
    from dida_api.adapters import lift_baba_nats_credentials
    from dida_core import decrypt_secret, encrypt_secret

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")

    async def clean():
        await pool.execute("DELETE FROM adapter_config WHERE adapter = 'baba'")
        await pool.execute("DELETE FROM users WHERE username = 'natsadmin'")

    await clean()
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('natsadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "adapters-sites-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "natsadmin", "password": "adminpw12"})
        assert r.status_code == 200

        r = await c.put("/adapters/baba/sites/new", json={"nats_url": "nats://dida:S3CRET@198.51.100.11:4222"})
        assert r.status_code == 400

        r = await c.put("/adapters/baba/sites/new", json={
            "nats_url": "nats://198.51.100.11:4222", "nats_user": "dida", "nats_password": "S3CRET"})
        assert r.status_code == 200
        [site] = (await c.get("/adapters/baba/sites")).json()["sites"]
        assert site["name"] == "198.51.100.11"
        assert site["nats_user"] == "dida" and site["has_nats_password"]
        assert "S3CRET" not in json.dumps(site)

        r = await c.put(f"/adapters/baba/sites/{site['key']}", json={
            "name": "Kuća", "nats_url": "nats://198.51.100.11:4222", "nats_user": "dida"})
        assert r.status_code == 200
        [site] = (await c.get("/adapters/baba/sites")).json()["sites"]
        assert site["has_nats_password"], "a blank password on edit keeps the stored one"

    key = os.environ["DIDA_SECRET_KEY"]
    legacy = [{"name": "Cabin", "nats_url": "wss://dida:p%40ss@cabin.example/nats"}]
    await pool.execute(
        "UPDATE adapter_config SET value = $1 WHERE adapter = 'baba' AND key = 'sites'",
        encrypt_secret(key, json.dumps(legacy)))
    await lift_baba_nats_credentials(pool)
    await lift_baba_nats_credentials(pool)
    [lifted] = json.loads(decrypt_secret(key, await pool.fetchval(
        "SELECT value FROM adapter_config WHERE adapter = 'baba' AND key = 'sites'")))
    assert lifted["nats_url"] == "wss://cabin.example/nats"
    assert (lifted["nats_user"], lifted["nats_password"]) == ("dida", "p%40ss")

    await clean()
    await pool.close()


class _CloudflareNC(_StubNC):
    """Answers the cloudflare control plane so the endpoint can get past it; the
    route store itself lives adapter-side and is not what this test is about."""

    async def request(self, subject, payload=b"", *, timeout=None):
        if subject == "dida.cloudflare.ctl":
            return SimpleNamespace(data=json.dumps({"ok": True}).encode())
        return await super().request(subject, payload, timeout=timeout)


async def test_cloudflare_route_removal_takes_its_entity():
    """Removing an ingress route must take its registry entity with it.

    The entity is a mirror of a row the adapter no longer has, and the adapter
    only ever publishes routes that still exist — so nothing downstream can ever
    reconcile the leftover. It has to die here or it lives forever (a real one
    outlived a route removal on the Cabin install). The neighbouring route must
    survive: the cleanup is keyed by hostname slug, and a LIKE-shaped match would
    take both."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")

    async def clean():
        await pool.execute("DELETE FROM users WHERE username = 'cfadmin'")
        await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'cloudflare:cftest%'")
        await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'cloudflare:cftest%'")

    await clean()
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('cfadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "adapters-cf-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    appmod.app.state.bus.nc = _CloudflareNC()

    for eid in ("cloudflare:cftest_example_com", "cloudflare:cftest2_example_com"):
        await pool.execute(
            "INSERT INTO entities (entity_id, adapter, device_key) VALUES ($1, 'cloudflare', $1) "
            "ON CONFLICT (entity_id) DO NOTHING", eid)
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value, updated_at) "
            "VALUES ($1, 'text', to_jsonb('http://198.51.100.7:80'::text), now()) "
            "ON CONFLICT (entity_id, capability) DO UPDATE SET value = EXCLUDED.value", eid)

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "cfadmin", "password": "adminpw12"})
        assert r.status_code == 200

        r = await c.delete("/adapters/cloudflare/routes/cftest.example.com")
        assert r.status_code == 200

        left = [row["entity_id"] for row in await pool.fetch(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'cloudflare:cftest%' ORDER BY 1")]
        assert left == ["cloudflare:cftest2_example_com"], "the removed route's entity is gone, the other stays"
        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = 'cloudflare:cftest_example_com'"
        ) == 0, "its state row goes too"
        assert await pool.fetchval(
            "SELECT count(*) FROM removed_devices WHERE key = 'cloudflare:cftest_example_com'"
        ) == 0, "no tombstone — re-adding the same hostname must work"

    await clean()
    await pool.close()


class _DiscoverNC:
    """Runner reports shelly/broadlink/esphome enabled and baba disabled; shelly
    finds a device, broadlink answers with an error, esphome never answers."""

    def __init__(self):
        self.asked: list[str] = []

    async def publish(self, subject, payload):
        pass

    async def request(self, subject, payload=b"", *, timeout=None):
        if subject == "dida.runner.ctl":
            profiles = {a: {"enabled": a != "baba"} for a in ("shelly", "broadlink", "esphome", "baba")}
            return SimpleNamespace(data=json.dumps({"profiles": profiles}).encode())
        self.asked.append(subject)
        if subject == "dida.discover.shelly":
            return SimpleNamespace(data=json.dumps(
                {"devices": [{"label": "shelly1 (198.51.100.9)", "appendCsv": {"hosts": "198.51.100.9"}}]}
            ).encode())
        if subject == "dida.discover.broadlink":
            return SimpleNamespace(data=json.dumps({"error": "bind failed"}).encode())
        raise TimeoutError("nats: timeout")


async def test_discover_all_reports_failed_adapters():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username = 'discadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('discadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    nc = _DiscoverNC()
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "adapters-discover-test-secret-0123456789ab"
    appmod.app.state.bus = StubBus()
    appmod.app.state.bus.nc = nc

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "discadmin", "password": "adminpw12"})
        assert r.status_code == 200

        r = await c.get("/adapters/discover-all")
        assert r.status_code == 200
        body = r.json()
        assert "dida.discover.baba" not in nc.asked, "a disabled adapter is not scanned"
        assert body["found"]["shelly"][0]["label"] == "shelly1 (198.51.100.9)"
        assert body["failed"] == {"broadlink": "bind failed", "esphome": "nats: timeout"}, (
            "an adapter's error reply and a missing answer both reach the UI"
        )
        assert "baba" not in body["found"] and "baba" not in body["failed"]

    await pool.execute("DELETE FROM users WHERE username = 'discadmin'")
    await pool.close()
