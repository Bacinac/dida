"""Settings: the one endpoint that holds every key the installation owns.

Two invariants, and the second one has already been broken once in this codebase.

A secret goes in and never comes back. `GET /settings` answers with
`*_configured: bool` and a four-character hint, because an admin needs to know
WHICH key is set, not what it is — and a response that carries the key puts it in
the browser, in the devtools network tab, and in whatever the browser caches. The
test enumerates the response model rather than checking a few fields by hand: a
new secret added to the settings page without a `_configured` flag fails here.

And a secret is encrypted at rest. The anthropic and openai keys used to sit in
`app_settings` in plaintext while every peer secret — opus, owntracks, adapter
credentials — was Fernet-encrypted. They rode out inside every `pg_dump` config
backup, which are the files this installation deliberately copies off-box to NFS.
A billing credential in a backup on another machine is the failure; the fix was
one line, and this is what keeps it.
"""

from __future__ import annotations

import json

import pytest
from dida_api import settings as mod
from dida_api.common import key_hint

SECRETS = {
    "anthropic_api_key": "sk-ant-REAL-SECRET-abcd",
    "openai_api_key": "sk-openai-REAL-SECRET-wxyz",
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

    async def _resolved(pool_, provider):
        return SECRETS[f"{provider}_api_key"]

    async def _host(pool_, key):
        return {"lan_ip": "192.0.2.10",
                "opus_url": "http://opus.local:8098",
                "app_url": "https://dida.example",
                "net_parent": "eth0"}.get(key, "")
    monkeypatch.setattr(mod, "resolved_key", _resolved)
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
            assert field.endswith(("_configured", "_hint")), \
                f"{field} looks like it carries a secret"
            assert info.annotation in (bool, str | None), f"{field}: {info.annotation}"


async def test_a_configured_key_is_visible_as_configured(stored):
    """The flag has to be true for a set key, or the admin is told to re-enter a
    key that is already there."""
    out = await mod.get_settings(_Request(stored))
    assert out.anthropic_configured is True
    assert out.openai_configured is True
    assert out.owntracks_configured is True
    assert out.opus_configured is True


async def test_a_player_with_a_url_but_no_token_is_not_configured(stored, monkeypatch):
    """Half-configured is the state that produces "why is nothing loading" — url
    alone must not read as ready."""
    stored.rows.pop("opus_token")
    out = await mod.get_settings(_Request(stored))
    assert out.opus_configured is False
    assert out.opus_url == "http://opus.local:8098"


async def test_the_hint_is_four_characters_and_not_the_key(stored):
    out = await mod.get_settings(_Request(stored))
    assert out.anthropic_hint == SECRETS["anthropic_api_key"][-4:]
    assert len(out.anthropic_hint) == 4


@pytest.mark.parametrize("value,expect", [
    (None, None),
    ("", None),
    ("ab", "ab"),
    ("sk-ant-0123456789", "6789"),
])
def test_the_hint_never_returns_more_than_the_tail(value, expect):
    """A short value is its own tail — which is fine, because a two-character
    secret is not a secret. What must never happen is a hint that grows with the
    key."""
    assert key_hint(value) == expect
    assert key_hint(value) is None or len(key_hint(value)) <= 4


async def test_an_unconfigured_installation_reports_nothing_rather_than_empty_strings(
        monkeypatch):
    """Fresh install: flags false, hints absent — not `""`, which the UI would
    render as a key that is set to nothing."""
    async def _none(pool_, provider):
        return None

    async def _host(pool_, key):
        return ""
    monkeypatch.setattr(mod, "resolved_key", _none)
    monkeypatch.setattr(mod, "host_setting", _host)
    out = await mod.get_settings(_Request(_Pool()))
    assert out.anthropic_configured is False
    assert out.anthropic_hint is None
    assert out.owntracks_configured is False


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
    ("anthropic_api_key", "anthropic_api_key"),
    ("openai_api_key", "openai_api_key"),
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
    saving the language would delete the Anthropic credential."""
    rows, cleared = written
    await mod.put_settings(mod.SettingsIn(announce_lang="hr"), _Request(_Pool()))
    assert "anthropic_api_key" not in rows
    assert "anthropic_api_key" not in cleared


async def test_an_explicitly_empty_field_clears_the_key(written):
    """The only way to remove a key from the UI — it has to actually remove it,
    not store the empty string, which would read as "configured"."""
    rows, cleared = written
    await mod.put_settings(mod.SettingsIn(anthropic_api_key=""), _Request(_Pool()))
    assert "anthropic_api_key" in cleared
    assert "anthropic_api_key" not in rows


async def test_whitespace_only_counts_as_clearing_not_as_a_key(written):
    """A pasted blank line stored as a key produces `configured: true` and an
    assistant that fails on every call with an authentication error."""
    _, cleared = written
    await mod.put_settings(mod.SettingsIn(anthropic_api_key="   "), _Request(_Pool()))
    assert "anthropic_api_key" in cleared


async def test_a_key_is_stripped_before_it_is_stored(written):
    """Copy-paste from a dashboard brings a trailing newline, and the provider
    rejects it with the same 401 a wrong key gets."""
    rows, _ = written
    await mod.put_settings(mod.SettingsIn(anthropic_api_key="  sk-ant-real\n"),
                           _Request(_Pool()))
    assert rows["anthropic_api_key"] == "ENC(sk-ant-real)"


def test_the_write_model_bounds_every_field():
    """These are stored and some are interpolated into URLs; an unbounded string is
    an unbounded row."""
    for field, info in mod.SettingsIn.model_fields.items():
        if info.annotation in (str | None,):
            assert any(getattr(m, "max_length", None) for m in info.metadata), \
                f"{field} has no length bound"
