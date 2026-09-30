"""Integration test — the retention-policy admin routes (dida_api.retention) end
to end against a real Postgres: GET reflects the seeded defaults (0004_retention.sql
migration), PUT round-trips a class/capability/override edit, PUT validation (a
missing 'default' class, an unmapped capability/override class) is rejected before
any write, and the ClickHouse re-apply on save is exercised for both wirings:

  * app.state.ch = None            -> best-effort skip, still 200 (read the handler:
                                       `if ch is not None: try: await apply_retention...`)
  * app.state.ch = a stub client   -> apply_retention() runs for real against the
                                       stub, one `ch.command(...)` per tier in
                                       dida_core.retention._TIERS

NOTE: `apply_retention()` catches each tier's ClickHouse failure internally (logs
a warning, keeps going) — it never raises. So the route's `except Exception:
raise HTTPException(502, ...)` around it is dead in practice from a bad ch; a
raising `ch.command` is swallowed inside apply_retention, not by the route. We
don't chase that 502 with a fragile setup (e.g. killing the real pool) — the two
branches above are the ones actually reachable.

The whole-table PUT (retention_capability/retention_override are wholesale
DELETE+re-INSERT, retention_class deletes any row not resubmitted) is restored to
its original captured state at the end so it doesn't leave the shared seed data
mutated for any other suite.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""
import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from dida_core.retention import _TIERS
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


class StubCH:
    """Records the DDL `apply_retention()` issues; never raises."""

    def __init__(self):
        self.calls: list[str] = []

    async def command(self, sql):
        self.calls.append(sql)


async def _cleanup(pool):
    await pool.execute("DELETE FROM retention_override WHERE entity_id = 'zztest:sensor'")
    await pool.execute("DELETE FROM retention_capability WHERE capability = 'zztest_cap'")
    await pool.execute("DELETE FROM users WHERE username = 'retadmin'")


async def test_retention_get_put_roundtrip():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('retadmin', $1, 'admin')", await hash_password("adminpw12")
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "retention-crud-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    appmod.app.state.ch = None  # best-effort TTL apply is skipped when unset

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "retadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # GET reflects the 0004_retention.sql seed
        r = await c.get("/retention")
        assert r.status_code == 200
        original = r.json()
        assert isinstance(original["known_capabilities"], list), "known_capabilities is always a list"
        names = {cls["name"] for cls in original["classes"]}
        assert "default" in names, "the seeded 'default' class is present"
        assert {"solar", "utility", "climate", "presence", "discrete"} <= names, "all seeded classes are present"
        default_cls = next(cls for cls in original["classes"] if cls["name"] == "default")
        assert default_cls["days_raw"] == 90 and default_cls["days_1h"] == 365 and default_cls["days_1d"] == 365, (
            "default class matches the migration seed"
        )
        assert original["capabilities"].get("temperature") == "climate", "seeded capability->class mapping"
        assert original["overrides"] == [], "no overrides seeded by default"

        # --- PUT validation: rejected BEFORE any write ---

        # missing the required 'default' class
        classes_no_default = [cls for cls in original["classes"] if cls["name"] != "default"]
        r = await c.put(
            "/retention",
            json={"classes": classes_no_default, "capabilities": original["capabilities"], "overrides": []},
        )
        assert r.status_code == 400, "a class list without 'default' is rejected"

        # capability mapped to an unknown class
        r = await c.put(
            "/retention",
            json={"classes": original["classes"], "capabilities": {"temperature": "no_such_class"}, "overrides": []},
        )
        assert r.status_code == 400, "a capability mapped to an unknown class is rejected"

        # override mapped to an unknown class
        r = await c.put(
            "/retention",
            json={
                "classes": original["classes"],
                "capabilities": original["capabilities"],
                "overrides": [{"entity_id": "zztest:sensor", "capability": "temperature", "class_name": "no_such_class"}],
            },
        )
        assert r.status_code == 400, "an override mapped to an unknown class is rejected"

        # confirm none of the rejected PUTs touched the data
        r = await c.get("/retention")
        assert r.json()["classes"] == original["classes"], "rejected PUTs never wrote (validated before the transaction)"

        # --- PUT round-trip: change default's days_raw, add a capability mapping
        # and an override, ch unset -> best-effort skip, still 200 ---
        modified_classes = [
            {**cls, "days_raw": 123} if cls["name"] == "default" else cls for cls in original["classes"]
        ]
        modified_caps = {**original["capabilities"], "zztest_cap": "climate"}
        modified_overrides = [{"entity_id": "zztest:sensor", "capability": "temperature", "class_name": "climate"}]

        r = await c.put(
            "/retention",
            json={"classes": modified_classes, "capabilities": modified_caps, "overrides": modified_overrides},
        )
        assert r.status_code == 200 and r.json() == {"ok": True}, "PUT with ch=None still saves and 200s"

        r = await c.get("/retention")
        assert r.status_code == 200
        got = r.json()
        got_default = next(cls for cls in got["classes"] if cls["name"] == "default")
        assert got_default["days_raw"] == 123, "default class days_raw persisted"
        assert got["capabilities"]["zztest_cap"] == "climate", "new capability mapping persisted"
        assert {"entity_id": "zztest:sensor", "capability": "temperature", "class_name": "climate"} in got["overrides"], (
            "new override persisted"
        )
        assert {c["name"] for c in got["classes"]} == names, "no seeded class was dropped by the round-trip"

        # --- same PUT again, this time with a stub ClickHouse client wired: the
        # route must actually call apply_retention(), one ch.command per tier ---
        ch = StubCH()
        appmod.app.state.ch = ch
        r = await c.put(
            "/retention",
            json={"classes": modified_classes, "capabilities": modified_caps, "overrides": modified_overrides},
        )
        assert r.status_code == 200 and r.json() == {"ok": True}, "PUT with a working ch still 200s"
        assert len(ch.calls) == len(_TIERS), "apply_retention issues one MODIFY TTL per tier"
        for table, _tscol, _field in _TIERS:
            assert any(table in call for call in ch.calls), f"a TTL DDL was issued for {table}"

        # restore the pre-test state so this suite leaves no mutated seed data behind
        appmod.app.state.ch = None
        r = await c.put(
            "/retention",
            json={"classes": original["classes"], "capabilities": original["capabilities"], "overrides": []},
        )
        assert r.status_code == 200 and r.json() == {"ok": True}, "restore PUT"
        r = await c.get("/retention")
        restored = r.json()
        assert restored["classes"] == original["classes"], "classes restored"
        assert restored["capabilities"] == original["capabilities"], "capabilities restored"
        assert restored["overrides"] == [], "overrides restored"

    await _cleanup(pool)
    await pool.close()
