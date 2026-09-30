"""Regression tests for the Roidmi MiOT <-> canonical mapping and the in-house
miio wire codec.

Run inside the roidmi adapter image (dida_adapter_roidmi installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-roidmi:latest \
      -c "python -m pytest tests/test_roidmi_mapping.py"

Covers status_caps (MiOT result list → capability dict: state vocabulary,
partial polls, fault extraction only in the error state), the fan-speed
vocabulary round-trip, and the miio packet codec (encrypt/decrypt round-trip,
checksum enforcement, hello parsing) — the codec is ours, not a library's, so
its invariants are gated here.
"""
import hashlib
import json
import struct

import pytest
from dida_adapter_roidmi.mapping import FAN_OPTIONS, fan_id, status_caps
from dida_adapter_roidmi.miio import MiioClient, MiioError


def _r(did, value, code=0):
    return {"did": did, "siid": 1, "piid": 1, "code": code, "value": value}


# --- status_caps ---------------------------------------------------------------

def test_status_caps_full_poll():
    caps, fault = status_caps([
        _r("state", 4), _r("error_code", 0), _r("fanspeed_mode", 2), _r("battery_level", 82),
    ])
    assert caps == {"vacuum": "cleaning", "battery": 82.0, "enum": "basic"}
    assert fault is None, "no fault while the state is not 'error'"


def test_status_caps_state_vocabulary():
    for raw, canonical in [(1, "idle"), (2, "idle"), (3, "paused"), (4, "cleaning"),
                           (5, "returning"), (6, "charging"), (7, "error"),
                           (9, "docked"), (11, "paused")]:
        caps, _ = status_caps([_r("state", raw)])
        assert caps["vacuum"] == canonical, f"state {raw} → {canonical}"
    caps, _ = status_caps([_r("state", 999)])
    assert caps["vacuum"] == "idle", "an unknown state id degrades to idle, not a crash"


def test_status_caps_fault_only_in_error_state():
    caps, fault = status_caps([_r("state", 7), _r("error_code", 13)])
    assert caps["vacuum"] == "error" and fault == "dustbin full"
    _, fault = status_caps([_r("state", 7), _r("error_code", 999)])
    assert "999" in fault, "an uncatalogued code still names itself"
    _, fault = status_caps([_r("state", 6), _r("error_code", 13)])
    assert fault is None, "a stale error code without the error state is not a fault"


def test_status_caps_partial_poll():
    caps, _ = status_caps([_r("state", 6), _r("battery_level", None, code=-4004)])
    assert caps == {"vacuum": "charging"}, "an errored property is skipped, the rest still map"
    caps, _ = status_caps([])
    assert caps == {}, "an empty result maps to nothing (nothing is published)"


def test_fan_vocabulary_round_trip():
    for opt in FAN_OPTIONS:
        level = fan_id(opt)
        caps, _ = status_caps([_r("fanspeed_mode", level)])
        assert caps["enum"] == opt, f"{opt} survives the id round-trip"
    assert fan_id("warp") is None, "an unknown option is refused, not guessed"
    caps, _ = status_caps([_r("fanspeed_mode", 0)])
    assert "enum" not in caps, "the mop-mode oddity (0) is not a suction level"


# --- miio codec ----------------------------------------------------------------

TOKEN = "00112233445566778899aabbccddeeff"


def _client() -> MiioClient:
    c = MiioClient("192.0.2.23", TOKEN)
    c._device_id = b"\x01\x02\x03\x04"
    c._stamp = 1000
    c._session_at = 0.0
    return c


def test_codec_round_trip():
    c = _client()
    body = json.dumps({"id": 1, "method": "get_properties", "params": []}).encode()
    packet = c._build(c._encrypt(body))
    magic, length = struct.unpack(">HH", packet[:4])
    assert magic == 0x2131 and length == len(packet), "header declares the real length"
    assert c._parse(packet) == {"id": 1, "method": "get_properties", "params": []}, \
        "encrypt → build → parse is the identity"


def test_codec_rejects_wrong_token():
    c = _client()
    packet = c._build(c._encrypt(b'{"id": 1}'))
    other = MiioClient("192.0.2.23", "ff" * 16)
    other._device_id, other._stamp, other._session_at = c._device_id, c._stamp, 0.0
    with pytest.raises(MiioError, match="checksum"):
        other._parse(packet)


def test_codec_rejects_empty_reply():
    c = _client()
    with pytest.raises(MiioError, match="empty"):
        c._parse(c._build(c._encrypt(b"{}"))[:32])


def test_token_validation():
    with pytest.raises(MiioError, match="hex"):
        MiioClient("192.0.2.23", "zz" * 16)
    with pytest.raises(MiioError, match="32 hex"):
        MiioClient("192.0.2.23", "aabb")


def test_key_derivation_matches_protocol():
    # key = md5(token), iv = md5(key + token) — the documented miio derivation.
    c = _client()
    token = bytes.fromhex(TOKEN)
    key = hashlib.md5(token).digest()
    assert c._key == key
    assert c._iv == hashlib.md5(key + token).digest()


# --- reachability: the verdict from the source, edge-triggered --------------------


class _ReachBus:
    def __init__(self) -> None:
        self.reach: list = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)

    async def publish_state(self, update) -> None:
        pass

    async def publish_entity(self, info) -> None:
        pass


def _rkeys(bus):
    return [(e.device_key, e.reachable) for e in bus.reach]


async def test_reachability_follows_the_poll_and_is_edge_triggered():
    from dida_adapter_roidmi.adapter import RoidmiAdapter

    a = RoidmiAdapter()
    a._bus = _ReachBus()
    a._key = ("192.168.20.23", "token")
    await a._publish_reach(False, "no reply")
    await a._publish_reach(False, "still")
    await a._publish_reach(True)
    assert _rkeys(a._bus) == [("192.168.20.23", False), ("192.168.20.23", True)]


async def test_reachability_without_a_host_publishes_nothing():
    from dida_adapter_roidmi.adapter import RoidmiAdapter

    a = RoidmiAdapter()
    a._bus = _ReachBus()
    a._key = None
    await a._publish_reach(False, "x")
    assert a._bus.reach == []
