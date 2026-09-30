"""Samsung TV over its own LAN API — what the adapter decides from the TV's answers.

Power is read from whether the TV answers its REST description, so the rules that
matter are the ones about silence: a Wi-Fi TV that is plainly on misses a single
request now and then, and reading that as "off" would flash the backlight and fire
every rule that follows the TV. Silence at boot is not yet an answer either.

A TV that goes quiet has been switched off as far as the LAN can tell, so the
adapter never calls it unreachable — that would ring the device alarm every night.

The wake packet has to name the TV's subnet: the house host routes by default over
the IoT VLAN, and the limited broadcast would leave on that leg. The fixture is the
house host's real route table.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from dida_adapter_samsungtv import adapter
from dida_adapter_samsungtv.adapter import _MISSES_BEFORE_OFF, SamsungTVAdapter
from dida_adapter_samsungtv.mapping import (
    broadcast_for,
    entity_id,
    identity,
    magic_packet,
    stated_power,
)
from dida_core import Command, ReachabilityEvent
from dida_core.health import StatusReporter

HOUSE_ROUTES = """Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
eth0	00000000	0114A8C0	0003	0	0	0	00000000	0	0	0
docker0	000011AC	00000000	0001	0	0	0	0000FFFF	0	0	0
br-23273cb8439b	000012AC	00000000	0001	0	0	0	0000FFFF	0	0	0
eth1	0001A8C0	00000000	0001	0	0	0	00FFFFFF	0	0	0
eth0	0014A8C0	00000000	0001	0	0	0	00FFFFFF	0	0	0
"""

TV_2018 = {"device": {
    "duid": "uuid:71c6a766-0747-4bb3-83a8-50963b964cfa", "modelName": "UE75NU7172",
    "name": "[TV] Living Room", "wifiMac": "64:1C:AE:00:53:44",
}}


# --- mapping ---------------------------------------------------------------------


def test_a_2018_tv_does_not_state_its_power():
    assert stated_power(TV_2018) is None
    assert stated_power(None) is None


def test_power_state_decides_on_models_that_answer_in_standby():
    assert stated_power({"device": {"PowerState": "standby"}}) is False
    assert stated_power({"device": {"PowerState": "on"}}) is True


def test_identity_keeps_the_device_id_and_a_valid_mac():
    assert identity(TV_2018) == {"duid": "71c6a766-0747-4bb3-83a8-50963b964cfa", "mac": "64:1c:ae:00:53:44"}
    assert identity({"device": {"wifiMac": "not-a-mac"}}) == {}


def test_entity_id_is_built_from_the_name():
    assert entity_id("Living Room TV") == "samsungtv:living_room_tv"


def test_magic_packet_is_six_ff_then_the_mac_sixteen_times():
    packet = magic_packet("64:1c:ae:00:53:44")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("641cae005344")
    with pytest.raises(ValueError):
        magic_packet("")


def test_the_wake_broadcast_names_the_tvs_subnet_not_the_default_route():
    assert broadcast_for("192.168.1.34", HOUSE_ROUTES) == "192.168.1.255"
    assert broadcast_for("192.168.20.40", HOUSE_ROUTES) == "192.168.20.255"


def test_a_tv_behind_a_router_cannot_be_woken():
    assert broadcast_for("10.0.0.5", HOUSE_ROUTES) is None
    assert broadcast_for("not-an-ip", HOUSE_ROUTES) is None


def test_the_library_cannot_log_the_remote_token():
    """samsungtvws logs each token it receives at INFO, and the services run with
    the root logger at INFO."""
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.DEBUG)
    try:
        assert not logging.getLogger("samsungtvws.connection").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(level)


# --- lifecycle -------------------------------------------------------------------


class _Cfg:
    def __init__(self, **values) -> None:
        self.values = {"host": "192.168.1.34", "name": "Living Room TV", **values}

    def get(self, key, default=""):
        return self.values.get(key, default)

    def int(self, key, default=0):
        return int(self.values.get(key, default))


class _Bus:
    def __init__(self) -> None:
        self.power: list[bool] = []
        self.entities: list = []
        self.reach: list[ReachabilityEvent] = []

    async def publish_state(self, update) -> None:
        self.power.append(update.value)

    async def publish_entity(self, info) -> None:
        self.entities.append(info)

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)


# The 2018 TV's answer during a standby wake: the same description, renderer shut.
WAKE = dict(TV_2018)


def _rig(answers: list[dict | None], token: str | None = "tok") -> SamsungTVAdapter:
    a = SamsungTVAdapter()
    a.status = StatusReporter("samsungtv")
    a._bus = _Bus()
    a._cfg = _Cfg()
    a._token = token
    script = iter(answers)
    last: list[dict | None] = [None]

    async def describe():
        last[0] = next(script)
        return last[0]

    async def renderer_up():
        return last[0] is not WAKE

    async def learn(info):
        a._device = {**a._device, **identity(info)}

    a._describe = describe
    a._renderer_up = renderer_up
    a._learn = learn
    return a


async def _polls(a: SamsungTVAdapter, n: int) -> None:
    for _ in range(n):
        await a._poll()


async def test_a_lone_missed_request_does_not_switch_the_tv_off():
    a = _rig([TV_2018, None, TV_2018, None, None, TV_2018])
    await _polls(a, 6)
    assert a._bus.power == [True]


async def test_a_run_of_silence_is_off():
    a = _rig([TV_2018] + [None] * _MISSES_BEFORE_OFF)
    await _polls(a, 1 + _MISSES_BEFORE_OFF)
    assert a._bus.power == [True, False]


async def test_silence_at_boot_publishes_nothing_until_it_is_conclusive():
    a = _rig([None] * _MISSES_BEFORE_OFF)
    await _polls(a, _MISSES_BEFORE_OFF - 1)
    assert a._bus.power == []
    await a._poll()
    assert a._bus.power == [False]


async def test_a_standby_wake_does_not_switch_the_tv_on():
    a = _rig([None] * _MISSES_BEFORE_OFF + [WAKE] * 6 + [None])
    await _polls(a, _MISSES_BEFORE_OFF + 7)
    assert a._bus.power == [False]
    assert a._device.get("mac"), "a wake still teaches the MAC the next turn_on needs"


async def test_the_renderer_blinking_shut_does_not_switch_a_tv_off():
    a = _rig([TV_2018, WAKE, TV_2018, WAKE, WAKE, TV_2018])
    await _polls(a, 6)
    assert a._bus.power == [True]


async def test_a_tv_whose_screen_stays_dark_switches_off():
    a = _rig([TV_2018] + [WAKE] * _MISSES_BEFORE_OFF)
    await _polls(a, 1 + _MISSES_BEFORE_OFF)
    assert a._bus.power == [True, False]


async def test_a_renderer_shut_for_longer_than_any_wake_is_reported(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    a = _rig([WAKE] * 3)
    await a._poll()
    clock[0] += 150
    await a._poll()
    assert a.status.snapshot()["state"] == "ok", "a two-minute wake is still just a wake"
    clock[0] += adapter._DARK_LIMIT
    await a._poll()
    status = a.status.snapshot()
    assert status["state"] == "error" and "9197" in status["detail"]


async def test_the_tv_is_announced_with_its_device_id_as_native_key():
    a = _rig([TV_2018])
    await a._poll()
    assert [(e.entity_id, e.native_key, e.capabilities) for e in a._bus.entities][-1] == (
        "samsungtv:living_room_tv", "71c6a766-0747-4bb3-83a8-50963b964cfa", ["on_off"])


async def test_switching_off_never_reads_as_unreachable():
    a = _rig([TV_2018] + [None] * 5 + [TV_2018])
    await _polls(a, 7)
    assert [e.reachable for e in a._bus.reach] == [True]


async def test_a_rename_republishes_power_under_the_new_entity():
    a = _rig([TV_2018, TV_2018])
    await a._poll()
    a._cfg.values["name"] = "Dnevni TV"
    await a._poll()
    assert a._bus.power == [True, True]
    assert [e.device_key for e in a._bus.reach] == ["samsungtv:living_room_tv", "samsungtv:dnevni_tv"]


async def test_the_first_time_it_is_on_without_a_token_the_tv_is_asked_once():
    a = _rig([TV_2018, TV_2018, None, None, None, TV_2018], token=None)
    asked = []

    async def pair():
        asked.append(True)

    a._pair = pair
    await _polls(a, 6)
    await asyncio.sleep(0)
    assert len(asked) == 2, "once per power-on, not once per poll"


def _command(cmd: str) -> Command:
    return Command(entity_id="samsungtv:living_room_tv", capability="on_off", command=cmd, ts_ns=1)


async def _commanded(on: bool, cmd: str) -> list[str]:
    a = _rig([])
    a._entity, a._on = "samsungtv:living_room_tv", on
    sent: list[str] = []

    async def power_key():
        sent.append("power_key")

    a._power_key = power_key
    a._send_wake = lambda: sent.append("wake")

    async def settle():
        pass

    a._settle = settle
    await a.handle_command(_command(cmd))
    return sent


async def test_turn_off_presses_power_only_when_the_tv_is_on():
    assert await _commanded(True, "turn_off") == ["power_key"]
    assert await _commanded(False, "turn_off") == []


async def test_turn_on_wakes_only_when_the_tv_is_off():
    assert await _commanded(False, "turn_on") == ["wake"]
    assert await _commanded(True, "turn_on") == []


async def test_toggle_resolves_from_the_last_known_power():
    assert await _commanded(True, "toggle") == ["power_key"]
    assert await _commanded(False, "toggle") == ["wake"]


async def test_a_wake_without_a_learned_mac_fails_loud():
    a = _rig([])
    a._host = "192.168.1.34"
    with pytest.raises(RuntimeError, match="MAC"):
        a._send_wake()


class _Remote:
    def __init__(self, log: list) -> None:
        self.log, self.token = log, "tok"

    async def start_listening(self, callback=None):
        self.log.append("listen")

    async def open(self):
        self.log.append("open")

    async def send_command(self, command, key_press_delay=None):
        self.log.append(("send", command.get_payload().count("KEY_POWER"), key_press_delay))

    async def close(self):
        self.log.append("close")


async def test_the_power_key_waits_for_the_channel_before_pressing(monkeypatch):
    """Sent straight after the connect event the TV ignores the key; two seconds into
    a listening connection it acts on it."""
    from dida_adapter_samsungtv import adapter as mod

    log: list = []
    a = _rig([])
    a._remote = lambda: _Remote(log)
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        log.append(("sleep", seconds))
        await real_sleep(0)

    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    await a._power_key()
    assert log == ["listen", ("sleep", mod._KEY_READY), ("send", 1, mod._KEY_HOLD_OPEN), "close"]
    assert mod._KEY_READY >= 2
