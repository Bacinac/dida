"""The bus logins the keys service writes: one per identity, each narrowed to its own name.

The server enforces what dida_core.identity says, so a loosened table is a loosened
house even with every test of the server green. These pin the table itself; the
gate's bus boundary suite tries it against the real server.
"""

from __future__ import annotations

import json

import pytest
from dida_core import identity
from dida_core.crypto import adapter_key, bus_password
from dida_core.events import CORE

ROOT = "test-root-key-for-the-bus-logins"
CORE_TOKENS = {"automation", "camera", "cfg", "command", "discover", "engine", "entity",
               "events", "heartbeat", "journal", "logs", "radio", "reachability", "runner",
               "state", "status", "vacuum"}


def _users() -> dict[str, dict]:
    conf = identity.nats_users(ROOT)
    assert conf.startswith("authorization ")
    return {u["user"]: u for u in json.loads(conf.removeprefix("authorization "))["users"]}


def test_every_identity_has_its_own_password():
    names = identity.BUS_CLIENTS | {CORE}
    assert len({bus_password(ROOT, n) for n in names}) == len(names)
    assert bus_password(ROOT, "mqtt") == bus_password(ROOT, "mqtt"), \
        "deterministic: rerunning the keys service on every `up` changes no login"
    assert bus_password(ROOT, "mqtt") != adapter_key(ROOT, "mqtt").rstrip("=")
    with pytest.raises(ValueError):
        bus_password("", "mqtt")


def test_the_server_list_is_core_plus_every_bus_client():
    users = _users()
    assert set(users) == identity.BUS_CLIENTS | {CORE}
    assert all(u["password"] == bus_password(ROOT, n) for n, u in users.items())
    assert users[CORE]["permissions"] == {"publish": {"allow": [">"]}, "subscribe": {"allow": [">"]}}


def test_no_client_is_named_like_a_core_subject():
    """`dida.<name>.ctl` and `dida.discover.<name>` are granted by name: an adapter
    called `runner` would hear the runner's control channel."""
    assert not (identity.BUS_CLIENTS | {CORE}) & CORE_TOKENS


def test_a_client_publishes_under_its_own_name_only():
    for name in identity.BUS_CLIENTS:
        allowed = identity.permissions(name)["publish"]["allow"]
        own = [s for s in allowed if s.split(".")[1] in
               {"state", "entity", "reachability", "heartbeat", "journal", "logs", "cfg"}]
        assert all(s.split(".")[2] == name for s in own), f"{name} speaks in another's name: {own}"
        commands = {s.split(".")[2] for s in allowed if s.startswith("dida.command.")}
        assert commands == set(identity.COMMANDS.get(name, ())), f"{name} commands {commands}"
        assert not any(s in (">", "dida.>") for s in allowed)


def test_a_client_hears_only_what_is_addressed_to_it():
    for name in identity.BUS_CLIENTS:
        perms = identity.permissions(name)
        heard = set(perms["subscribe"]["allow"]) - set(identity.HEARS.get(name, ()))
        assert heard == {f"dida.command.{name}.>", f"dida.status.{name}", f"dida.discover.{name}",
                         f"dida.{name}.ctl", f"_INBOX.{name}.>"}
        assert perms["allow_responses"] is True


def test_only_broker_clients_reach_the_broker():
    assert "dida.cfg.netmgr" not in identity.permissions("netmgr")["publish"]["allow"]
    assert "dida.cfg.mqtt" in identity.permissions("mqtt")["publish"]["allow"]


def test_the_keys_service_writes_every_login(tmp_path, monkeypatch):
    monkeypatch.setattr(identity.os, "chown", lambda p, u, g: None)
    identity.write_keys(ROOT, tmp_path)
    for name in identity.BUS_CLIENTS | {CORE}:
        path = tmp_path / name / "nats"
        assert path.read_text() == bus_password(ROOT, name)
        assert path.stat().st_mode & 0o777 == 0o400
    assert (tmp_path / "nats-server" / "users.conf").read_text() == identity.nats_users(ROOT)
