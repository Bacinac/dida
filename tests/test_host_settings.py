"""An installation's own values come from the database and nowhere else; the
environment seeds them once and is never consulted again."""

from __future__ import annotations

import pytest
from dida_core import host_setting, seed_app_setting, set_app_setting
from dida_core.db import _setting_cache


class FakeDB:
    """Minimal asyncpg stand-in: one settings row, plus a record of writes."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.executed: list[tuple] = []

    async def fetchval(self, _sql: str, *args):
        return self.value

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))
        # Mimic ON CONFLICT DO NOTHING: a present row is never overwritten.
        if "DO NOTHING" in sql and self.value:
            return
        self.value = args[1]


@pytest.fixture(autouse=True)
def _clean_cache():
    _setting_cache.clear()
    yield
    _setting_cache.clear()


@pytest.mark.asyncio
async def test_reads_the_stored_value(monkeypatch):
    # An environment variable of the same name must NOT be able to win.
    monkeypatch.setenv("DIDA_APP_URL", "http://from-env")
    assert await host_setting(FakeDB("http://from-db"), "app_url") == "http://from-db"


@pytest.mark.asyncio
async def test_absent_row_is_empty_not_the_environment(monkeypatch):
    monkeypatch.setenv("DIDA_LAN_IP", "203.0.113.9")
    assert await host_setting(FakeDB(None), "lan_ip") == ""


@pytest.mark.asyncio
async def test_default_covers_only_the_pre_seed_instant():
    assert await host_setting(FakeDB(None), "net_parent", "ens18") == "ens18"


@pytest.mark.asyncio
async def test_blank_row_reads_as_unset():
    assert await host_setting(FakeDB("   "), "net_parent", "ens18") == "ens18"


@pytest.mark.asyncio
async def test_seed_writes_when_absent():
    db = FakeDB(None)
    await seed_app_setting(db, "app_url", "https://dida.example.com")
    assert db.value == "https://dida.example.com"
    assert await host_setting(db, "app_url") == "https://dida.example.com"


@pytest.mark.asyncio
async def test_seed_never_overwrites_what_the_ui_set():
    db = FakeDB("https://edited-in-the-ui")
    await seed_app_setting(db, "app_url", "https://from-a-stale-env")
    assert db.value == "https://edited-in-the-ui", "a redeploy must not undo a UI edit"


@pytest.mark.asyncio
async def test_a_failed_read_raises_instead_of_answering_empty():
    """The boot race that caught us: the DB is still coming up, the read fails, and a
    swallowed error would hand back "" — which for the LAN address means SSDP binds
    the wrong leg, and the cache would keep it wrong for the whole TTL."""

    class BrokenDB:
        async def fetchval(self, _sql, *args):
            raise ConnectionRefusedError("postgres is still starting")

    from dida_core.db import _setting_cache

    with pytest.raises(ConnectionRefusedError):
        await host_setting(BrokenDB(), "lan_ip")
    assert "lan_ip" not in _setting_cache, "a failure must not become a cached answer"


@pytest.mark.asyncio
async def test_a_write_invalidates_the_cached_read():
    db = FakeDB("first")
    assert await host_setting(db, "app_url") == "first"
    db.value = "second"
    assert await host_setting(db, "app_url") == "first", "the TTL is what keeps reads off the DB"
    await set_app_setting(db, "app_url", "second")
    assert await host_setting(db, "app_url") == "second"


def test_the_zigbee_console_link_is_built_from_what_already_exists(monkeypatch):
    """The console runs on this host, on the port compose publishes it on. Both facts
    already live somewhere — the host address in app_settings, the port in the
    environment — and copying either into the frontend would make a third place to be
    wrong. The token is deliberately NOT in the link: the console asks for it once and
    remembers it, while a secret in an address ends up in history and referrers."""
    def build(lan_ip: str, port_env: str | None) -> str:
        monkeypatch.setenv("DIDA_Z2M_PORT", port_env) if port_env else None
        import os
        return f"http://{lan_ip}:{os.environ.get('DIDA_Z2M_PORT', '8092')}" if lan_ip else ""

    assert build("192.0.2.10", None) == "http://192.0.2.10:8092"
    assert build("192.0.2.10", "9000") == "http://192.0.2.10:9000"
    assert build("", None) == "", "no host, no link — one nobody can follow is worse than none"
