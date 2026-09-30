"""Landroid control path: a command must not evaporate when Worx' MQTT drops.

Control (start/pause/dock) exists only over Worx' MQTT, and pyworxcloud retries
that link after a failed *initial* connect or a token rotation — not after a
mid-session drop. A dropped link therefore stayed down for hours, and every
command sent into it (the schedule's morning start included) was logged and
discarded. These tests pin the recovery: the watchdog rebuilds the session, a
command rebuilds before giving up, and giving up is loud.
"""
import time

import pytest
from dida_adapter_landroid.adapter import TORQUE_ENTITY, LandroidAdapter
from dida_core import Command, CommandRejected
from dida_core.health import StatusReporter


class StubCloud:
    def __init__(self, connected: bool) -> None:
        self.mqtt_connected = connected
        self.sent: list[tuple[str, str]] = []

    async def start(self, serial):
        self.sent.append(("start", serial))

    async def pause(self, serial):
        self.sent.append(("pause", serial))

    async def home(self, serial):
        self.sent.append(("dock", serial))

    async def set_torque(self, serial, value):
        self.sent.append(("torque", f"{serial}:{value}"))

    async def update(self, serial):
        pass


class StubCfg:
    def int(self, key, default=0):
        return default


def _adapter(connected: bool) -> LandroidAdapter:
    a = LandroidAdapter()
    a.status = StatusReporter("landroid")
    a._cfg = StubCfg()
    a._cloud = StubCloud(connected)
    a._serial = "SERIAL"
    return a


def _command(name: str) -> Command:
    return Command(entity_id="landroid:mower", capability="mower", command=name, ts_ns=0)


def _torque(value) -> Command:
    return Command(entity_id=TORQUE_ENTITY, capability="number", command="set_value",
                   ts_ns=0, args={"value": value})


async def test_watchdog_rebuilds_only_after_the_grace_window():
    a = _adapter(connected=False)
    rebuilds = []

    async def fake_rebuild():
        rebuilds.append(1)
        a._cloud.mqtt_connected = True

    a._rebuild = fake_rebuild

    assert await a._watch_mqtt() is False, "first tick down only starts the clock"
    assert rebuilds == [], "a momentary drop is not worth tearing the session down"

    a._mqtt_down_since = time.monotonic() - 121  # past the 120 s default grace
    assert await a._watch_mqtt() is True, "rebuild restored control"
    assert rebuilds == [1], "down past the grace window rebuilds the session"
    assert a._mqtt_down_since is None, "a live link clears the down clock"


async def test_command_on_a_dead_link_rebuilds_and_still_sends():
    a = _adapter(connected=False)

    async def fake_rebuild():
        a._cloud = StubCloud(connected=True)

    a._rebuild = fake_rebuild

    await a.handle_command(_command("start"))

    assert a._cloud.sent == [("start", "SERIAL")], "the start survived the dead link"
    assert a.status.snapshot()["state"] != "error", "recovered control is not an error"


async def test_command_gives_up_loud_when_the_rebuild_fails():
    a = _adapter(connected=False)

    async def fake_rebuild():
        pass  # Worx still refusing — control genuinely unavailable

    a._rebuild = fake_rebuild

    with pytest.raises(CommandRejected, match="Worx MQTT down"):
        await a.handle_command(_command("start"))

    assert a._cloud.sent == [], "nothing can be sent over a link that never came back"
    assert a.status.snapshot()["state"] == "error", "control being dead shows as error, not a green badge"


async def test_torque_is_written_to_the_mower():
    a = _adapter(connected=True)

    await a.handle_command(_torque(25))

    assert a._cloud.sent == [("torque", "SERIAL:25")]


async def test_torque_outside_what_the_mower_accepts_is_refused_here():
    a = _adapter(connected=True)

    with pytest.raises(CommandRejected, match="outside"):
        await a.handle_command(_torque(80))
    with pytest.raises(CommandRejected, match="no number"):
        await a.handle_command(_torque("hard"))

    assert a._cloud.sent == [], "an out-of-range write would be rejected by Worx anyway"


async def test_torque_on_a_dead_link_rebuilds_like_any_other_command():
    a = _adapter(connected=False)

    async def fake_rebuild():
        a._cloud = StubCloud(connected=True)

    a._rebuild = fake_rebuild

    await a.handle_command(_torque(-10))

    assert a._cloud.sent == [("torque", "SERIAL:-10")]
