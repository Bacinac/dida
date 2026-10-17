"""Settings: the one endpoint that holds every key the installation owns.

Two invariants, and the second one has already been broken once in this codebase.

A secret goes in and never comes back. `GET /settings` answers with
`*_configured: bool`, because an admin needs to know a key is set, not what it is
— and a response that carries the key puts it in the browser, in the devtools
network tab, and in whatever the browser caches. The test enumerates the response
model rather than checking a few fields by hand: a new secret added to the
settings page without a `_configured` flag fails here.

And a secret is encrypted at rest. LLM keys once sat in `app_settings` in
plaintext while every peer secret was Fernet-encrypted, and rode out inside every
`pg_dump` config backup — the files this installation deliberately copies off-box
to NFS. A credential in a backup on another machine is the failure.
"""

from __future__ import annotations

import json

import dida_api.common as common
import pytest
from dida_api import settings as mod
from dida_core import encrypt_secret

SECRETS = {
    "owntracks_secret": "owntracks-REAL-SECRET",
    "opus_token": "opus-REAL-SECRET",
}


class _Pool:
    def __init__(self, rows=None) -> None:
        self.rows = dict(rows or {})
        self.writes: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    async def fetchval(self, sql, *a):
        return self.rows.get(a[0]) if a else None

    async def execute(self, sql, *a):
        self.deletes.append(sql)


class _Request:
    def __init__(self, pool) -> None:
        self.app = type("A", (), {"state": type("S", (), {"pool": pool})()})()


@pytest.fixture
def stored(monkeypatch):
    """A fully configured installation — every secret set to a recognisable value."""
    pool = _Pool({
        **SECRETS,
        "announce_lang": "hr",
        "discovery_subnets": "192.0.2.0/24",
        "lan_status": json.dumps({"address": "192.0.2.10", "hostname": "dida", "interfaces": [{"iface": "eth1", "address": "192.0.2.10/24", "mac": "", "lan": True, "default_route": True}]}),
        "vlan_status": "{}",
        "vlan_config": "{}",
        "managed_vlans": "",
    })

    async def _host(pool_, key):
        return {"lan_ip": "192.0.2.10",
                "opus_url": "http://opus.local:8098",
                "app_url": "https://dida.example",
                "net_parent": "eth0"}.get(key, "")
    monkeypatch.setattr(mod, "host_setting", _host)
    return pool


# --- nothing secret comes back out ----------------------------------------------


async def test_no_secret_value_appears_in_the_response(stored):
    """The blunt check, on the serialised body — the shape the browser receives."""
    out = await mod.get_settings(_Request(stored))
    body = out.model_dump_json()
    for name, value in SECRETS.items():
        assert value not in body, f"{name} was returned to the client"


async def test_every_secret_is_reported_as_a_flag_not_a_value(stored):
    """Enumerated from the response MODEL, so a secret added to the settings page
    without a `_configured` flag fails here rather than shipping."""
    for field, info in mod.SettingsOut.model_fields.items():
        if any(w in field for w in ("key", "secret", "token", "password")):
            assert field.endswith("_configured"), f"{field} looks like it carries a secret"
            assert info.annotation is bool, f"{field}: {info.annotation}"


async def test_a_configured_key_is_visible_as_configured(stored):
    """The flag has to be true for a set key, or the admin is told to re-enter a
    key that is already there."""
    out = await mod.get_settings(_Request(stored))
    assert out.owntracks_configured is True
    assert out.opus_configured is True


async def test_a_player_with_a_url_but_no_token_is_not_configured(stored, monkeypatch):
    """Half-configured is the state that produces "why is nothing loading" — url
    alone must not read as ready."""
    stored.rows.pop("opus_token")
    out = await mod.get_settings(_Request(stored))
    assert out.opus_configured is False
    assert out.opus_url == "http://opus.local:8098"


async def test_an_unconfigured_installation_reports_nothing(monkeypatch):
    async def _host(pool_, key):
        return ""
    monkeypatch.setattr(mod, "host_setting", _host)
    out = await mod.get_settings(_Request(_Pool()))
    assert out.owntracks_configured is False
    assert out.opus_configured is False


async def test_corrupt_status_json_does_not_take_the_settings_page_down(stored):
    """`lan_status` and the VLAN blobs are written by netmgr, not by a person. A
    half-written value must not make Settings unopenable — that is the page you go
    to in order to fix netmgr."""
    for key in ("lan_status", "vlan_status", "vlan_config"):
        stored.rows[key] = "{not json"
    out = await mod.get_settings(_Request(stored))
    assert out.lan_status == {} and out.vlan_status == {} and out.vlan_config == {}


# --- every secret is encrypted on the way in ------------------------------------


@pytest.fixture
def written(monkeypatch):
    """Captures what would actually land in `app_settings`."""
    rows: dict[str, str] = {}
    cleared: list[str] = []

    async def _set(pool, key, value):
        rows[key] = value

    async def _clear(pool, key):
        cleared.append(key)
    monkeypatch.setattr(mod, "set_app_setting", _set)
    monkeypatch.setattr(mod, "clear_app_setting", _clear)
    monkeypatch.setattr(mod, "encrypt_secret", lambda secret, val: f"ENC({val})")
    monkeypatch.setenv("DIDA_SECRET_KEY", "0123456789abcdef0123456789abcdef")
    return rows, cleared


@pytest.mark.parametrize("field,key", [
    ("owntracks_secret", "owntracks_secret"),
    ("opus_token", "opus_token"),
])
async def test_a_secret_is_encrypted_before_it_is_stored(field, key, written, monkeypatch):
    """Plaintext here rides out inside every pg_dump config backup — and those are
    deliberately copied off-box to NFS on this installation."""
    rows, _ = written
    pool = _Pool()
    await mod.put_settings(mod.SettingsIn(**{field: "REAL-SECRET"}), _Request(pool))
    assert key in rows, f"{key} was not written"
    assert rows[key] == "ENC(REAL-SECRET)", f"{key} stored as {rows[key]!r}"


async def test_a_setting_that_is_not_a_secret_is_stored_as_typed(written):
    """Encrypting the announce language would make it unreadable to the adapter
    that needs it — the distinction has to stay deliberate."""
    rows, _ = written
    await mod.put_settings(mod.SettingsIn(announce_lang="en"), _Request(_Pool()))
    assert rows.get("announce_lang") == "en"


async def test_an_omitted_field_leaves_the_stored_key_alone(written):
    """The settings form posts every field. If an untouched blank cleared the key,
    saving the language would delete the OwnTracks secret."""
    rows, cleared = written
    await mod.put_settings(mod.SettingsIn(announce_lang="hr"), _Request(_Pool()))
    assert "owntracks_secret" not in rows
    assert "owntracks_secret" not in cleared


async def test_an_explicitly_empty_field_clears_the_key(written):
    """The only way to remove a key from the UI — it has to actually remove it,
    not store the empty string, which would read as "configured"."""
    rows, cleared = written
    await mod.put_settings(mod.SettingsIn(owntracks_secret=""), _Request(_Pool()))
    assert "owntracks_secret" in cleared
    assert "owntracks_secret" not in rows


async def test_whitespace_only_counts_as_clearing_not_as_a_key(written):
    """A pasted blank line stored as a key produces `configured: true` and a
    receiver that fails on every report."""
    _, cleared = written
    await mod.put_settings(mod.SettingsIn(owntracks_secret="   "), _Request(_Pool()))
    assert "owntracks_secret" in cleared


async def test_a_key_is_stripped_before_it_is_stored(written):
    """Copy-paste from a dashboard brings a trailing newline, and the far end
    rejects it the way it rejects a wrong key."""
    rows, _ = written
    await mod.put_settings(mod.SettingsIn(opus_token="  opus-real\n"), _Request(_Pool()))
    assert rows["opus_token"] == "ENC(opus-real)"


def test_the_write_model_bounds_every_field():
    """These are stored and some are interpolated into URLs; an unbounded string is
    an unbounded row."""
    for field, info in mod.SettingsIn.model_fields.items():
        if info.annotation in (str | None,):
            assert any(getattr(m, "max_length", None) for m in info.metadata), \
                f"{field} has no length bound"


async def test_a_stored_secret_that_will_not_decrypt_is_never_handed_back_raw(monkeypatch, caplog):
    """A rotated DIDA_SECRET_KEY or a corrupt row: that ciphertext must not leave
    as a token and come back as an opaque 401 from the far end. None, loudly."""
    secret = "test-secret-for-key-encryption-0123456789"
    monkeypatch.setenv("DIDA_SECRET_KEY", secret)
    store = {"opus_token": encrypt_secret(secret, "opus-live"),
             "owntracks_secret": encrypt_secret("a-different-secret-key-0123456789ab", "x")}

    async def fake_get(pool, key):
        return store.get(key)
    monkeypatch.setattr(common, "get_setting", fake_get)
    assert await common.stored_api_key(None, "opus_token") == "opus-live"
    assert await common.stored_api_key(None, "panel_token") is None
    assert await common.stored_api_key(None, "owntracks_secret") is None
    assert "undecryptable" in caplog.text
