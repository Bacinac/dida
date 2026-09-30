"""Every bus identity, tried against the real server with the logins the keys service wrote.

An adapter is the most exposed process in the house: it parses whatever a device or
a cloud sends it. What it may do on the bus is therefore decided by the server, not
by the adapter's good manners — and a boundary nobody attacks is a boundary nobody
knows is still closed. Each case here tries something one identity must not do and
checks that it did not happen, next to a permitted twin that must, so a server that
drops everything cannot pass for one that enforces the rules.

The gate runs the keys service into a throwaway volume, starts the server the way
compose does (docker/nats.conf + docker/nats-run.sh + the written users.conf), and
runs this in an adapter image with that volume mounted at DIDA_ACL_KEYS.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import nats
import pytest
from nats.js.api import StreamConfig

SERVER = f"nats://{os.environ['DIDA_ACL_NATS']}"
KEYS = Path(os.environ["DIDA_ACL_KEYS"])
SETTLE_S = 0.3


def _password(identity: str) -> str:
    return (KEYS / identity / "nats").read_text().strip()


class Client:
    def __init__(self, nc, errors: list[str]):
        self.nc, self.errors = nc, errors

    def refused(self, what: str) -> bool:
        return any(f"permissions violation for {what}" in e.lower() for e in self.errors)


async def _login(identity: str, *, password: str | None = None) -> Client:
    errors: list[str] = []

    async def on_error(exc: Exception) -> None:
        errors.append(str(exc))

    nc = await nats.connect(
        SERVER, user=identity, password=password or _password(identity),
        inbox_prefix=f"_INBOX.{identity}", error_cb=on_error, allow_reconnect=False)
    return Client(nc, errors)


class Heard(list):
    def __init__(self) -> None:
        super().__init__()
        self.arrived = asyncio.Event()


async def _heard(c: Client, subject: str) -> Heard:
    got = Heard()

    async def cb(msg) -> None:
        got.append(msg.data)
        got.arrived.set()

    await c.nc.subscribe(subject, cb=cb)
    await c.nc.flush()
    return got


async def _settle(*clients: Client) -> None:
    for c in clients:
        await c.nc.flush()
    await asyncio.sleep(SETTLE_S)


async def _after_twin(sender: Client, twin: Heard) -> None:
    """Wait for the permitted twin, published after the forged message on the same
    connection. The server keeps one connection's order, so once the twin is in, a
    forged message the server let through has reached this client too; the settle
    only lets its subscription's callback run."""
    await sender.nc.flush()
    await asyncio.wait_for(twin.arrived.wait(), 5)
    await asyncio.sleep(SETTLE_S)


@pytest.fixture
async def login():
    opened: list[Client] = []

    async def _open(identity: str) -> Client:
        c = await _login(identity)
        opened.append(c)
        return c

    yield _open
    for c in opened:
        await c.nc.close()


@pytest.mark.parametrize("theirs, ours", [
    ("dida.state.shelly.shelly:probe", "dida.state.mqtt.mqtt:probe"),
    ("dida.entity.shelly.shelly:probe", "dida.entity.mqtt.mqtt:probe"),
    ("dida.reachability.shelly", "dida.reachability.mqtt"),
    ("dida.heartbeat.shelly", "dida.heartbeat.mqtt"),
    ("dida.journal.shelly", "dida.journal.mqtt"),
    ("dida.logs.shelly", "dida.logs.mqtt"),
    ("dida.state.core.virtual:probe", "dida.state.mqtt.virtual:probe"),
])
async def test_an_adapter_speaks_only_in_its_own_name(login, theirs, ours):
    core, mqtt = await login("core"), await login("mqtt")
    forged, honest = await _heard(core, theirs), await _heard(core, ours)
    await mqtt.nc.publish(theirs, b"forged")
    await mqtt.nc.publish(ours, b"fact")
    await _after_twin(mqtt, honest)
    assert honest == [b"fact"], "the permitted twin arrives, so silence below means refusal"
    assert forged == [], f"mqtt published {theirs}"
    assert mqtt.refused("publish")


@pytest.mark.parametrize("subject", [
    "dida.runner.ctl",
    "dida.runner.tunnel",
    "dida.events",
    "dida.automation.run",
    "dida.engine.stats",
    "dida.cfg.shelly",
    "dida.command.shelly.shelly:probe",
    "_INBOX.core.probe",
])
async def test_an_adapter_cannot_speak_on_the_control_plane(login, subject):
    core, mqtt = await login("core"), await login("mqtt")
    forbidden, allowed = await _heard(core, subject), await _heard(core, "dida.cfg.mqtt")
    await mqtt.nc.publish(subject, b"forged")
    await mqtt.nc.publish("dida.cfg.mqtt", b"sealed")
    await _after_twin(mqtt, allowed)
    assert allowed == [b"sealed"], "its own broker subject reaches the api"
    assert forbidden == [], f"{subject} reached the core side"
    assert mqtt.refused("publish")


async def test_only_the_adapters_named_may_command_another(login):
    core, announce = await login("core"), await login("announce")
    speaker = await _heard(core, "dida.command.cast.>")
    light = await _heard(core, "dida.command.shelly.>")
    await announce.nc.publish("dida.command.cast.cast:kitchen", b"say")
    await announce.nc.publish("dida.command.shelly.shelly:kitchen", b"switch")
    await _settle(announce)
    assert speaker == [b"say"], "announce drives speakers"
    assert light == [], "announce switched a light"
    assert announce.refused("publish")


async def test_the_matter_bridge_commands_any_entity(login):
    core, bridge = await login("core"), await login("matter-bridge")
    got = await _heard(core, "dida.command.>")
    await bridge.nc.publish("dida.command.shelly.shelly:kitchen", b"on")
    await bridge.nc.publish("dida.command.mqtt.mqtt:hall", b"on")
    await _settle(bridge)
    assert got == [b"on", b"on"]
    assert not bridge.refused("publish")


@pytest.mark.parametrize("subject", [
    "dida.state.>",
    "dida.command.shelly.>",
    "dida.shelly.ctl",
    "dida.discover.shelly",
    "dida.status.shelly",
    "dida.runner.>",
    "dida.cfg.>",
    "_INBOX.shelly.>",
])
async def test_an_adapter_hears_only_what_is_addressed_to_it(login, subject):
    core, mqtt = await login("core"), await login("mqtt")
    overheard = await _heard(mqtt, subject)
    own = await _heard(mqtt, "dida.command.mqtt.>")
    probe = subject.replace(">", "probe")
    await core.nc.publish(probe, b"not yours")
    await core.nc.publish("dida.command.mqtt.mqtt:hall", b"yours")
    await _settle(core)
    assert own == [b"yours"], "its own commands arrive"
    assert overheard == [], f"mqtt heard {probe}"
    assert mqtt.refused("subscription")


async def test_an_adapter_asks_the_broker_and_hears_only_its_own_answer(login):
    """The broker's answer comes back to the asker's inbox; with every client's
    inbox under its own name, another adapter cannot sit on it and read or replay
    what the api handed out."""
    core, mqtt, shelly = await login("core"), await login("mqtt"), await login("shelly")

    async def answer(msg) -> None:
        await msg.respond(b"sealed answer")

    await core.nc.subscribe("dida.cfg.mqtt", cb=answer)
    await core.nc.flush()
    snooped = await _heard(shelly, "_INBOX.mqtt.>")
    reply = await mqtt.nc.request("dida.cfg.mqtt", b"sealed ask", timeout=2)
    await _settle(shelly)
    assert reply.data == b"sealed answer"
    assert snooped == [], "shelly read mqtt's broker answer"
    assert shelly.refused("subscription")


async def test_an_adapter_answers_what_it_was_asked_and_nothing_else(login):
    """A reply goes back through allow_responses: an adapter may answer the one
    request it received, not write into an inbox it chose."""
    core, shelly = await login("core"), await login("shelly")

    async def discover(msg) -> None:
        await msg.respond(b"found")
        await shelly.nc.publish("_INBOX.core.chosen", b"unasked")

    await shelly.nc.subscribe("dida.discover.shelly", cb=discover)
    await shelly.nc.flush()
    unasked = await _heard(core, "_INBOX.core.chosen")
    reply = await core.nc.request("dida.discover.shelly", b"", timeout=2)
    await _settle(shelly)
    assert reply.data == b"found"
    assert unasked == [], "shelly wrote into an inbox it was not asked from"


async def test_an_adapter_cannot_touch_jetstream(login):
    core, mqtt = await login("core"), await login("mqtt")
    js = core.nc.jetstream()
    await js.add_stream(StreamConfig(name="ACL_PROBE", subjects=["acl.probe"]))
    try:
        with pytest.raises((nats.errors.NoRespondersError, nats.errors.TimeoutError)):
            await mqtt.nc.request("$JS.API.STREAM.DELETE.ACL_PROBE", b"", timeout=1)
        assert (await js.stream_info("ACL_PROBE")).config.name == "ACL_PROBE", \
            "the adapter deleted a stream"
    finally:
        await js.delete_stream("ACL_PROBE")


@pytest.mark.parametrize("identity, password_of", [("core", "mqtt"), ("shelly", "mqtt")])
async def test_a_password_opens_only_its_own_identity(identity, password_of):
    mine = await _login(password_of)
    await mine.nc.close()
    with pytest.raises(Exception, match=r"[Aa]uthorization"):
        await nats.connect(SERVER, user=identity, password=_password(password_of),
                           allow_reconnect=False, max_reconnect_attempts=0)
