"""Integration test — the alert HTTP surface against a real Postgres: the seeded
rule set (migration 0028) is readable + tunable by an admin, the tuning guards
hold, and /system/alerts returns the live/active shape. Runs in the api image; the
runner supplies the ephemeral Postgres (see tests/run.sh).
"""
import dida_api.app as appmod
import pytest
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

# The rules the platform ships with. Asserted against the live seed below, and
# reused by the catalogue check — so a rule added without a label fails HERE
# rather than printing its raw key at an admin who is trying to read the page.
SEEDED_RULES = {"adapter_offline", "consumer_lag", "buffer_drop", "breaker_open",
                "disk_low", "device_unreachable", "backup_stale", "alert_unrouted"}



class StubBus:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.nc = self

    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass

    async def request(self, subject, data, timeout):
        import json

        return type("Msg", (), {"data": json.dumps({"routes": self.routes}).encode()})()


async def _login(c, user, pw):
    r = await c.post("/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, f"login {user}"


async def test_alert_rules_read_and_tune():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('aladmin', 'aluser')")
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('aladmin', $1, 'admin')", await hash_password("pw12"))
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('aluser', $1, 'user')", await hash_password("pw12"))

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "alerts-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    # No evaluator + no CH wired → /system/alerts is the empty live shape.
    if hasattr(appmod.app.state, "alert_evaluator"):
        del appmod.app.state.alert_evaluator
    appmod.app.state.ch = None

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        # /system/alerts — any signed-in user, empty active + history here
        await _login(c, "aluser", "pw12")
        r = await c.get("/system/alerts")
        assert r.status_code == 200, "any user reads /system/alerts"
        assert r.json() == {"active": [], "history": []}, "no evaluator + no CH → empty live shape"

        # a non-admin cannot read or tune the rules
        assert (await c.get("/system/alert-rules")).status_code == 403, "rules are admin-only"
        assert (await c.put("/system/alert-rules/consumer_lag", json={"threshold": 5})).status_code == 403, "tuning is admin-only"

        # admin sees the seeded rules
        c.cookies.clear()
        await _login(c, "aladmin", "pw12")
        r = await c.get("/system/alert-rules")
        assert r.status_code == 200, "admin reads the rules"
        rules = {row["key"]: row for row in r.json()}
        assert set(rules) == SEEDED_RULES, "seeded rules"
        assert rules["consumer_lag"]["severity"] == "critical" and rules["consumer_lag"]["threshold"] == 1000, "lag seed"
        assert rules["disk_low"]["severity"] == "warning" and rules["disk_low"]["enabled"] is True, "disk seed"
        assert rules["device_unreachable"]["severity"] == "warning" and rules["device_unreachable"]["hold_s"] == 300, "unreachable seed"
        # 36 h: past one missed daily run so a hiccup does not cry wolf, well short
        # of a second. No hold — a backup that is a day and a half late has already
        # been late for a day and a half.
        assert rules["backup_stale"]["threshold"] == 36 and rules["backup_stale"]["hold_s"] == 0, "backup seed"

        # tune one rule — only the sent fields change
        r = await c.put("/system/alert-rules/consumer_lag", json={"threshold": 2500, "hold_s": 30})
        assert r.status_code == 204, "admin tunes a rule"
        rules = {row["key"]: row for row in (await c.get("/system/alert-rules")).json()}
        assert rules["consumer_lag"]["threshold"] == 2500 and rules["consumer_lag"]["hold_s"] == 30, "tuning persisted"
        assert rules["consumer_lag"]["enabled"] is True, "untouched field unchanged"

        # disable a rule
        r = await c.put("/system/alert-rules/disk_low", json={"enabled": False})
        assert r.status_code == 204, "a rule can be disabled"
        rules = {row["key"]: row for row in (await c.get("/system/alert-rules")).json()}
        assert rules["disk_low"]["enabled"] is False, "disable persisted"

        # empty patch is a 400; unknown rule is a 404
        assert (await c.put("/system/alert-rules/consumer_lag", json={})).status_code == 400, "empty patch rejected"
        assert (await c.put("/system/alert-rules/nope", json={"enabled": True})).status_code == 404, "unknown rule is 404"

    await pool.execute("DELETE FROM users WHERE username IN ('aladmin', 'aluser')")
    await pool.close()


def test_a_backup_that_stopped_arriving_is_measured_from_the_files():
    """The one failure that says nothing until the day it is needed.

    A reboot raced the NAS, the NFS mount timed out, `nofail` let the machine
    carry on, and for 23 hours nothing left the box — announced only by a log line
    every ten minutes. Every other way it breaks looks the same from outside, so
    the measure is the age of the newest backup FILE: a scheduler that believes it
    ran and a file that exists are different claims, and it was the second that
    stopped being true."""
    import os
    import time

    from dida_api import alerts as mod

    def age(files, tmp):
        for name, ago_h in files:
            path = os.path.join(tmp, name)
            with open(path, "wb") as fh:
                fh.write(b"x")
            when = time.time() - ago_h * 3600
            os.utime(path, (when, when))
        old = mod._BACKUP_DIR
        try:
            mod._BACKUP_DIR = tmp
            return mod._newest_backup_age_h()
        finally:
            mod._BACKUP_DIR = old

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # An empty share is not "fresh" — it is the worst case, and must not read
        # as zero hours old.
        assert age([], tmp) is None
    with tempfile.TemporaryDirectory() as tmp:
        assert age([("dida-config-1.dump", 40), ("dida-history-2.tar.gz", 5)], tmp) == \
            pytest.approx(5, abs=0.1), "the NEWEST file is the answer"
    with tempfile.TemporaryDirectory() as tmp:
        # Anything that is not a backup does not count as one.
        assert age([("notes.txt", 1), ("dida-config-1.dump", 30)], tmp) == \
            pytest.approx(30, abs=0.1)
    # A missing directory is the mount being gone, which is the alarm itself.
    mod._BACKUP_DIR, old = "/nonexistent-share", mod._BACKUP_DIR
    try:
        assert mod._newest_backup_age_h() is None
    finally:
        mod._BACKUP_DIR = old


def test_every_alert_rule_has_a_name_in_both_catalogues():
    """`device_unreachable` shipped on 2026-08-09 with no label and printed its own
    key on the Alerts page for three weeks, next to six rules with proper titles.
    Nothing caught it: the lookup builds `alerts.rule.${key}` at runtime and casts
    the result, and a cast is exactly where the type checker stops looking. So the
    catalogues are checked here, against the same set the seed is checked against."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for lang in ("hr", "en"):
        text = (root / "ui" / "src" / "lib" / "i18n" / f"{lang}.ts").read_text()
        missing = [k for k in sorted(SEEDED_RULES) if f"'alerts.rule.{k}'" not in text]
        assert not missing, f"{lang}.ts has no name for alert rule(s): {', '.join(missing)}"


def test_alert_history_says_which_zone_its_clock_is_in():
    """ClickHouse hands the API a naive datetime that is UTC. Passed on as-is, the
    browser parses it as local time and the Alerts page dates every event two hours
    early in summer — which is exactly how it read on the wall: an alert that fired at
    06:45 CEST listed as 04:45. Every other ClickHouse endpoint ships epoch millis and
    never has to answer this; this one is a string and must."""
    import datetime as dt

    from dida_api.alerts import _utc_iso

    naive = dt.datetime(2026, 8, 29, 4, 45, 51)
    assert _utc_iso(naive) == "2026-08-29T04:45:51+00:00", "a naive value is stamped UTC"
    aware = naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert _utc_iso(aware) == "2026-08-29T04:45:51+02:00", "one that already knows is left alone"


async def test_recipients_and_silences_over_http():
    """Recipients are chosen among the notify targets that exist, the page learns
    which of them can reach a device, and a silence is set and lifted per alert."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('aladmin', 'aluser')")
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('aladmin', $1, 'admin')", await hash_password("pw12"))
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('aluser', $1, 'user')", await hash_password("pw12"))
    await pool.execute("DELETE FROM entities WHERE entity_id IN ('notify:zzivo', 'notify:zzdesk', 'notify:all')")
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, capabilities) VALUES "
        "('notify:zzivo', 'notify', 'Zzivo', '[\"notify\"]'), ('notify:zzdesk', 'notify', 'Zzdesk', '[\"notify\"]'), "
        "('notify:all', 'notify', 'Svi', '[\"notify\"]')"
    )
    before = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'alert_recipients'")
    await pool.execute("DELETE FROM alert_silences")

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "alerts-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus(routes=["notify:zzivo"])
    if hasattr(appmod.app.state, "alert_evaluator"):
        del appmod.app.state.alert_evaluator
    appmod.app.state.ch = None
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            await _login(c, "aluser", "pw12")
            assert (await c.get("/system/alert-recipients")).status_code == 403
            assert (await c.post("/system/alerts/silence", json={"key": "adapter_offline"})).status_code == 403

            c.cookies.clear()
            await _login(c, "aladmin", "pw12")
            r = (await c.get("/system/alert-recipients")).json()
            ids = [t["entity_id"] for t in r["targets"]]
            assert "notify:zzivo" in ids and "notify:zzdesk" in ids
            assert "notify:all" not in ids, "a broadcast is not a recipient"
            assert r["routes"] == ["notify:zzivo"]

            assert (await c.put("/system/alert-recipients", json={"recipients": ["notify:nobody"]})).status_code == 400
            assert (await c.put("/system/alert-recipients", json={"recipients": ["notify:zzivo", "notify:zzivo"]})).status_code == 204
            assert (await c.get("/system/alert-recipients")).json()["recipients"] == ["notify:zzivo"]

            assert (await c.post("/system/alerts/silence", json={"key": "nope"})).status_code == 404
            assert (await c.post("/system/alerts/silence", json={"key": "adapter_offline", "scope": "peer", "hours": 0})).status_code == 422
            assert (await c.post("/system/alerts/silence", json={"key": "adapter_offline", "scope": "peer", "hours": 8})).status_code == 204
            row = await pool.fetchrow("SELECT until, created_by FROM alert_silences WHERE key = 'adapter_offline' AND scope = 'peer'")
            assert row["until"] is not None and row["created_by"] == "aladmin"
            assert (await c.post("/system/alerts/silence", json={"key": "adapter_offline", "scope": "peer"})).status_code == 204
            assert await pool.fetchval("SELECT until FROM alert_silences WHERE key = 'adapter_offline' AND scope = 'peer'") is None, \
                "no hours = until it resolves"
            assert (await c.delete("/system/alerts/silence", params={"key": "adapter_offline", "scope": "peer"})).status_code == 204
            assert await pool.fetchval("SELECT count(*) FROM alert_silences") == 0
    finally:
        await pool.execute("DELETE FROM alert_silences")
        await pool.execute("DELETE FROM entities WHERE entity_id IN ('notify:zzivo', 'notify:zzdesk', 'notify:all')")
        if before is None:
            await pool.execute("DELETE FROM app_settings WHERE key = 'alert_recipients'")
        else:
            await pool.execute("UPDATE app_settings SET value = $1 WHERE key = 'alert_recipients'", before)
        await pool.execute("DELETE FROM users WHERE username IN ('aladmin', 'aluser')")
        await pool.close()
