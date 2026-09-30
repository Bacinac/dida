"""Integration test — the system-observability surface (dida_api.system) end to
end against a real Postgres. `/system/stats` aggregates the engine's throughput
counters (request/reply on the bus), every adapter's live status badge, and
datastore reachability — any signed-in user may read it, an anonymous caller is
401. `/metrics` re-emits the same numbers as Prometheus text: disabled (404)
until DIDA_METRICS_TOKEN is set, then gated by that token (?token= or a Bearer
header), never echoing it.

The engine/adapter round-trips go over the bus (`bus.nc.request`): a StubBus fakes
NATS — it answers `dida.engine.stats` with a canned payload and lets every other
subject (the per-adapter probes) time out, so adapters read back 'offline'
(fail-loud) and the DB-backed / static branches stay real. Postgres is the real
ephemeral one the runner supplies (see tests/run.sh); ClickHouse is absent
(app.state.ch unset), so its reachability is exercised as down.

Runs in the api image (dida_api + httpx). Bypasses the app lifespan by wiring
app.state directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import json

import dida_api.app as appmod
from dida_core import ADAPTER_CONFIG, apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

# A representative engine-stats reply — the shape `_prom` reads (throughput
# counters + JetStream backlog + history-buffer health).
ENGINE_STATS = {
    "accepted": 1234, "rejected": 5, "stale": 2, "backlog": 0, "uptime_s": 3600,
    "history": {"buffered": 7, "dropped": 0, "connected": True},
}


class _StubMsg:
    def __init__(self, data: bytes):
        self.data = data


class _StubNc:
    """Fake NATS client. `request` answers the engine-stats subject with the
    configured payload; every other subject — the per-adapter status probes —
    raises, so each adapter reads back 'offline' (a silent/unreachable engine is
    the same path with engine=None)."""

    def __init__(self, engine: dict | None):
        self.engine = engine

    async def request(self, subject, payload, timeout=None):
        if subject == "dida.engine.stats" and self.engine is not None:
            return _StubMsg(json.dumps(self.engine).encode())
        raise TimeoutError("no reply")


class StubBus:
    def __init__(self, engine: dict | None = None):
        self.nc = _StubNc(engine)

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_system_stats():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username = 'sysadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('sysadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "system-stats-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus(engine=ENGINE_STATS)
    appmod.app.state.ch = None  # no ClickHouse in the test → reachability is 'down'

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # an anonymous caller is refused (current_user)
        r = await c.get("/system/stats")
        assert r.status_code == 401, "unauthenticated read is rejected"

        r = await c.post("/auth/login", json={"username": "sysadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # engine present → its counters flow straight through
        r = await c.get("/system/stats")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"engine", "adapters", "adapter_counts", "db"}, "the documented top-level shape"
        assert body["engine"] == ENGINE_STATS, "the engine reply is passed through verbatim"

        # every adapter is probed; none answer here → all 'offline', and the
        # per-state histogram sums to the adapter count.
        adapters = body["adapters"]
        assert len(adapters) == len(ADAPTER_CONFIG), "one badge per configured adapter"
        assert {a["name"] for a in adapters} == set(ADAPTER_CONFIG), "names cover the registry"
        assert all(a["state"] == "offline" for a in adapters), "no adapter answered → all offline"
        counts = body["adapter_counts"]
        assert counts["offline"] == len(adapters), "all counted as offline"
        assert sum(counts.values()) == len(adapters), "the histogram partitions every adapter"

        # datastore reachability: real Postgres is up, absent ClickHouse is down
        assert body["db"] == {"postgres": True, "clickhouse": False}

        # a silent engine (no reply) → engine is None, the rest still resolves
        appmod.app.state.bus = StubBus(engine=None)
        r = await c.get("/system/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["engine"] is None, "a silent engine reports as None, not an error"
        assert body["db"]["postgres"] is True, "Postgres reachability is independent of the engine"

    await pool.execute("DELETE FROM users WHERE username = 'sysadmin'")
    await pool.close()


async def test_metrics_endpoint(monkeypatch):
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "system-metrics-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus(engine=ENGINE_STATS)
    appmod.app.state.ch = None

    # No auth on /metrics — it's gated solely by DIDA_METRICS_TOKEN.
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # disabled by default (secure-by-default) → 404
        monkeypatch.delenv("DIDA_METRICS_TOKEN", raising=False)
        r = await c.get("/metrics")
        assert r.status_code == 404, "metrics are disabled until a token is set"

        # enabled: the wrong token / no token is 401
        monkeypatch.setenv("DIDA_METRICS_TOKEN", "s3cret-metrics-token")
        assert (await c.get("/metrics")).status_code == 401, "no token, but scraping is enabled → 401"
        assert (await c.get("/metrics", params={"token": "nope"})).status_code == 401, "wrong token → 401"

        # correct token via ?token= → Prometheus exposition with the engine metrics
        r = await c.get("/metrics", params={"token": "s3cret-metrics-token"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain"), "Prometheus text exposition"
        text = r.text
        assert "# TYPE dida_engine_up gauge" in text, "the engine-up gauge is emitted"
        assert "dida_engine_up 1" in text, "engine answered → up=1"
        assert "dida_engine_accepted_total 1234" in text, "the engine counter passes through"
        assert 'dida_datastore_up{db="postgres"} 1' in text, "Postgres reachability is exported"
        assert 'dida_datastore_up{db="clickhouse"} 0' in text, "absent ClickHouse is exported as down"
        assert "s3cret-metrics-token" not in text, "the token never appears in the body"

        # the Authorization: Bearer form is accepted too
        r = await c.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"})
        assert r.status_code == 200, "a Bearer token is accepted"

    await pool.close()
