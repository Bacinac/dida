"""Broadlink: learned IR codes → entities, and the four ways a bad file degrades.

240 statements at 0%. The blaster itself is one device fronting several — Screen,
Laser, Projector — and what turns a JSON file of base64 blobs into DIDA entities is
all pure, so it tests without hardware.

The degradation paths are the reason this suite matters. `_load_codes` runs inside
`start()`, BEFORE the supervise loop's try — so an exception there does not make one
adapter unhealthy, it wedges the container in a restart loop. Every bad-input branch
therefore has to return "no commands yet" and say so in the log, and each of those
branches was written from a real failure shape: an absent file (codes are off-repo),
JSON that parses but is not an object, and unpadded base64 (which is how Home
Assistant and every community dump store these).
"""

from __future__ import annotations

import base64
import json

import pytest
from dida_adapter_broadlink import adapter as mod
from dida_adapter_broadlink.adapter import BroadlinkAdapter, _mac_hex, _slug

RAW = b"\x26\x00\x4c\x01"          # a plausible IR frame header
B64 = base64.b64encode(RAW).decode()


@pytest.fixture
def codes_file(tmp_path, monkeypatch):
    """Point the adapter at a throwaway codes.json."""
    path = tmp_path / "codes.json"
    monkeypatch.setattr(mod, "CODES_PATH", path)
    return path


def _adapter(codes_file, data) -> BroadlinkAdapter:
    if data is not None:
        codes_file.write_text(data if isinstance(data, str) else json.dumps(data))
    a = BroadlinkAdapter()
    a._codes = a._load_codes()
    a._reindex()
    return a


# --- the codes file -----------------------------------------------------------


def test_a_learned_code_becomes_a_command(codes_file):
    a = _adapter(codes_file, {"Screen": {"Down": B64}})
    assert a._codes["Screen"]["Down"] == RAW


def test_unpadded_base64_is_accepted(codes_file):
    """Home Assistant and every community dump store these unpadded; strict
    b64decode refuses them, and the command would silently not exist."""
    a = _adapter(codes_file, {"Laser": {"On": B64.rstrip("=")}})
    assert a._codes["Laser"]["On"] == RAW


def test_ONE_bad_code_does_not_lose_the_others(codes_file):
    """A single mistyped blob must cost one command, not the whole remote."""
    a = _adapter(codes_file, {"Screen": {"Down": B64, "Up": "!!! not base64 !!!"}})
    assert "Down" in a._codes["Screen"]


def test_an_ABSENT_file_is_no_commands_not_a_crash(codes_file):
    """The codes live off-repo; a fresh install simply has none yet."""
    a = _adapter(codes_file, None)
    assert a._codes == {}


def test_CORRUPT_json_is_no_commands_not_a_crash(codes_file, caplog):
    """`_load_codes` runs inside start(), before the supervise loop's try — an
    exception here does not make one adapter unhealthy, it wedges the container in a
    restart loop."""
    import logging

    with caplog.at_level(logging.WARNING):
        a = _adapter(codes_file, "{ this is not json")
    assert a._codes == {}
    assert "cannot read" in caplog.text, "a bad file must be loud, not just empty"


def test_json_that_is_not_an_OBJECT_is_no_commands_either(codes_file, caplog):
    """Valid JSON, wrong shape — a bare list would AttributeError on .items()."""
    import logging

    with caplog.at_level(logging.WARNING):
        a = _adapter(codes_file, ["Screen", "Laser"])
    assert a._codes == {}
    assert "not a JSON object" in caplog.text


def test_a_device_with_a_null_command_map_is_skipped_quietly(codes_file):
    a = _adapter(codes_file, {"Screen": {"Down": B64}, "Broken": None})
    assert list(a._codes) == ["Screen"]


# --- entity ids ---------------------------------------------------------------


def test_each_device_command_pair_is_its_own_entity(codes_file):
    a = _adapter(codes_file, {"Screen": {"Down": B64, "Up": B64}})
    assert set(a._entity_cmd) == {"broadlink:screen_down", "broadlink:screen_up"}


def test_the_entity_id_maps_back_to_the_device_and_command(codes_file):
    """The reverse lookup is what a command arriving on the bus is resolved through;
    a mismatch here means the button presses nothing."""
    a = _adapter(codes_file, {"Party Laser": {"On": B64}})
    assert a._entity_cmd["broadlink:party_laser_on"] == ("Party Laser", "On")


@pytest.mark.parametrize("name,want", [
    ("Screen", "screen"),
    ("Party Laser", "party_laser"),
    ("Projector-Main", "projector_main"),
    ("  Spaced  Out  ", "spaced_out"),
    ("!!!", "device"),
])
def test_names_slug_into_subject_safe_ids(name, want):
    assert _slug(name) == want


# --- the on/off command -------------------------------------------------------


def test_a_learned_ON_becomes_the_devices_power_toggle(codes_file):
    """A party laser's On is power; its other keys are modes."""
    a = _adapter(codes_file, {"Laser": {"On": B64, "Strobe": B64}})
    assert a._power_cmd("Laser") == "On"


def test_the_match_ignores_case_and_padding(codes_file):
    a = _adapter(codes_file, {"Laser": {" on ": B64}})
    assert a._power_cmd("Laser") == " on "


def test_a_device_with_no_ON_is_press_only(codes_file):
    """No discrete Off is invented: the same IR frame toggles both ways, so an
    assumed Off would send the wrong thing half the time."""
    a = _adapter(codes_file, {"Screen": {"Down": B64, "Up": B64}})
    assert a._power_cmd("Screen") is None


def test_an_unknown_device_has_no_power_command(codes_file):
    a = _adapter(codes_file, {"Screen": {"Down": B64}})
    assert a._power_cmd("Nope") is None


# --- the MAC ------------------------------------------------------------------


@pytest.mark.parametrize("mac", [
    "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff", "AABBCCDDEEFF",
])
def test_any_mac_separator_is_accepted(mac):
    """The UI stores it the standard way like every other adapter; python-broadlink
    wants raw hex. Rejecting a form the user reasonably typed would read as "the
    device is unreachable"."""
    assert _mac_hex(mac).lower() == "aabbccddeeff"


def test_the_mac_keeps_only_hex():
    assert _mac_hex("aa:bb:zz:cc") == "aabbcc"
