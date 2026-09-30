"""The broker's seal: who an adapter is on the bus.

An adapter holds no database role and no root key. It asks the api, sealing each
request with a key derived for it alone, and the answer — its secrets — comes back
under the same key. These pin what makes that an identity rather than a convention:
the keys are distinct and deterministic, the api drops a request sealed with the
wrong key or replayed past the TTL, an op is refused to an adapter it is not open to
before any database is touched, and the client waits out an api restart.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from cryptography.fernet import Fernet, InvalidToken
from dida_api import broker as api
from dida_core import identity
from dida_core.broker import TTL, Broker, BrokerError
from dida_core.crypto import adapter_key, bus_password, db_password
from dida_core.db_roles import ROLES
from dida_core.events import CORE
from nats.errors import NoRespondersError

ROOT = "test-root-key-for-the-broker-seal"


def _fernet(adapter: str) -> Fernet:
    return Fernet(adapter_key(ROOT, adapter))


def _seal(adapter: str, op: str, at: float | None = None, **args) -> bytes:
    body = json.dumps({"op": op, "args": args}).encode()
    f = _fernet(adapter)
    return f.encrypt(body) if at is None else f.encrypt_at_time(body, int(at))


# --- the keys ------------------------------------------------------------------


def test_every_client_has_its_own_key():
    keys = {adapter_key(ROOT, a) for a in identity.CLIENTS}
    assert len(keys) == len(identity.CLIENTS)
    assert not identity.ADAPTERS & identity.SERVICES
    assert adapter_key(ROOT, "mqtt") == adapter_key(ROOT, "mqtt"), \
        "deterministic: rerunning the keys service on every `up` changes nothing"
    assert adapter_key(ROOT + "x", "mqtt") != adapter_key(ROOT, "mqtt")


def test_no_root_key_no_adapter_keys():
    with pytest.raises(ValueError):
        adapter_key("", "mqtt")


def test_the_keys_service_writes_one_private_file_per_identity_and_role(tmp_path, monkeypatch):
    owners = []
    monkeypatch.setattr(identity.os, "chown", lambda p, u, g: owners.append((u, g)))
    identity.write_keys(ROOT, tmp_path)
    identity.write_keys(ROOT, tmp_path)  # over its own 0400 files, as every `up` does
    written = {(a, "key"): adapter_key(ROOT, a) for a in identity.CLIENTS}
    written |= {(n, "nats"): bus_password(ROOT, n) for n in identity.BUS_CLIENTS | {CORE}}
    written |= {("nats-server", "users.conf"): identity.nats_users(ROOT)}
    written |= {(f"db-{r}", "password"): db_password(ROOT, r) for r in ROLES}
    for (d, f), value in written.items():
        assert (tmp_path / d / f).read_text() == value
        assert (tmp_path / d / f).stat().st_mode & 0o777 == 0o400
        assert (tmp_path / d).stat().st_mode & 0o777 == 0o700
    assert {p.name for p in tmp_path.iterdir()} == {d for d, _ in written}
    assert set(owners) == {(1000, 1000)}, "readable by the service user, nobody else"


def test_each_role_has_its_own_password_and_none_without_the_root_key():
    assert len({db_password(ROOT, r) for r in ROLES}) == len(ROLES)
    assert db_password(ROOT, "dida_app") != adapter_key(ROOT, "dida_app").rstrip("=")
    with pytest.raises(ValueError):
        db_password("", "dida_app")


def test_an_adapter_without_its_key_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_FILE", tmp_path / "absent")
    with pytest.raises(RuntimeError, match="keys service"):
        identity.own_key()


# --- the api's side ------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.nc = self
        self.cb = None

    async def subscribe(self, subject, queue=None, cb=None):
        self.subject, self.queue, self.cb = subject, queue, cb


class _Msg:
    def __init__(self, subject: str, data: bytes) -> None:
        self.subject, self.data, self.replies = subject, data, []

    async def respond(self, data: bytes) -> None:
        self.replies.append(data)


@pytest.fixture
async def served(monkeypatch):
    monkeypatch.setenv("DIDA_SECRET_KEY", ROOT)
    bus = _Bus()
    await api.serve_broker(bus, pool=None)
    assert (bus.subject, bus.queue) == ("dida.cfg.*", "broker")
    return bus


async def _ask(bus, adapter: str, data: bytes) -> _Msg:
    msg = _Msg(f"dida.cfg.{adapter}", data)
    await bus.cb(msg)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if msg.replies:
            break
    return msg


async def test_the_answer_opens_only_with_the_askers_key(served):
    msg = await _ask(served, "mqtt", _seal("mqtt", "no-such-op"))
    [reply] = msg.replies
    assert json.loads(_fernet("mqtt").decrypt(reply)) == {"error": "unknown op 'no-such-op'"}
    with pytest.raises(InvalidToken):
        _fernet("tuya").decrypt(reply)


async def test_a_request_in_another_adapters_name_is_dropped(served):
    msg = await _ask(served, "tuya", _seal("mqtt", "config"))
    assert msg.replies == [], "mqtt's key cannot speak for tuya"


async def test_a_replayed_request_is_dropped(served):
    msg = await _ask(served, "mqtt", _seal("mqtt", "config", at=time.time() - TTL - 5))
    assert msg.replies == []


async def test_a_request_for_no_adapter_is_dropped(served):
    msg = await _ask(served, "nobody", _fernet("mqtt").encrypt(b"{}"))
    assert msg.replies == []


@pytest.mark.parametrize(("adapter", "op", "args"), [
    ("mqtt", "push_routes", {}),                                   # notify's alone
    ("mqtt", "setting", {"key": "opus_token"}),                    # sealed, opus's alone
    ("mqtt", "setting", {"key": "panel_token"}),                   # owned by cast
    ("mqtt", "set_setting", {"key": "lan_ip", "value": "x"}),      # readable is not writable
    ("mqtt", "forget", {"entity_ids": ["tuya:plug"]}),             # another namespace
    ("mqtt", "forget", {"prefix": "tuya:"}),
    ("volumio", "media_output", {"entity_id": "dlna:x", "probe_url": "u"}),
    ("mqtt", "state", {}),                                         # no filter = everything
    ("mqtt", "voice", {}),                                         # the Matter bridge's alone
    ("matter-bridge", "config", {}),                               # a service is no adapter
    ("matter-bridge", "setting", {"key": "lan_ip"}),
])
async def test_what_is_not_an_adapters_is_refused_before_the_database(adapter, op, args):
    reply = await api.answer(api.Ctx(pool=None, secret=ROOT), adapter, {"op": op, "args": args})
    assert "error" in reply and "result" not in reply
    assert "AttributeError" not in reply["error"], "refused by rule, not by tripping over the missing pool"


# --- the adapter's side --------------------------------------------------------


class _Nc:
    def __init__(self, *answers) -> None:
        self.answers, self.bodies = list(answers), []

    async def request(self, subject, body, timeout):
        self.subject = subject
        self.bodies.append(body)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return type("M", (), {"data": _fernet("mqtt").encrypt(json.dumps(answer).encode())})()


def _broker(nc) -> Broker:
    return Broker(type("B", (), {"nc": nc})(), "mqtt", key=adapter_key(ROOT, "mqtt"))


async def test_the_client_asks_on_its_own_subject_and_returns_the_result():
    nc = _Nc({"result": {"host": "10.0.0.2"}})
    assert await _broker(nc).call("config") == {"host": "10.0.0.2"}
    assert nc.subject == "dida.cfg.mqtt"
    assert json.loads(_fernet("mqtt").decrypt(nc.bodies[0])) == {"op": "config", "args": {}}


async def test_a_refusal_is_an_error_not_a_retry():
    nc = _Nc({"error": "push_routes is not open to mqtt"})
    with pytest.raises(BrokerError, match="not open"):
        await _broker(nc).call("push_routes")
    assert len(nc.bodies) == 1


async def test_an_api_restart_is_waited_out_with_a_fresh_seal():
    nc = _Nc(NoRespondersError(), {"result": 1})
    assert await _broker(nc).call("house") == 1
    assert len(nc.bodies) == 2 and nc.bodies[0] != nc.bodies[1], \
        "each retry is sealed anew, or a long wait would outlive the TTL"


async def test_patience_runs_out_loudly():
    nc = _Nc(NoRespondersError(), NoRespondersError(), NoRespondersError())
    with pytest.raises(NoRespondersError):
        await _broker(nc).call("house", patience=0.6)
