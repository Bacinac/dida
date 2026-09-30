"""Govee's Home Assistant MQTT discovery → canonical capabilities.

518 statements across two modules, none of them tested. `discovery.py` is where
the real work is: the bridge publishes HA discovery payloads — abbreviated keys,
`~` base-topic shortcuts, brightness on an arbitrary scale, colour temperature in
mireds — and this turns them into capability bindings both ways.

Every conversion here is one a wrong answer makes SILENT. A brightness scale read
as 255 when the device uses 100 gives a lamp that only ever reaches 39 %. Mireds
inverted gives warm where the user asked for cold. Neither raises anything.
"""

from __future__ import annotations

from dida_adapter_govee.discovery import (
    Registry,
    StateBinding,
    decode,
    encode,
    expand_config,
)

# --- discovery payload shape --------------------------------------------------


def test_abbreviated_keys_are_expanded():
    """Tasmota and friends publish the short forms; the mapper only knows long ones,
    so an unexpanded payload yields an entity with no topics and no error."""
    out = expand_config({"stat_t": "x/state", "cmd_t": "x/set", "pl_on": "1"})
    assert out["state_topic"] == "x/state"
    assert out["command_topic"] == "x/set"
    assert out["payload_on"] == "1"


def test_a_full_payload_passes_through_unchanged():
    cfg = {"state_topic": "a/b", "command_topic": "a/c"}
    assert expand_config(cfg) == cfg


def test_the_tilde_base_topic_is_resolved():
    """`~` is the whole reason a discovery payload is short. Left unresolved, every
    topic is subscribed literally as `~/state` and the device never reports."""
    out = expand_config({"~": "govee/light1", "stat_t": "~/state", "cmd_t": "~/set"})
    assert out["state_topic"] == "govee/light1/state"
    assert out["command_topic"] == "govee/light1/set"


def test_nested_device_blocks_are_expanded_too():
    out = expand_config({"dev": {"ids": ["abc"], "name": "Lamp"}})
    assert out["device"]["identifiers"] == ["abc"]


# --- inbound: payload -> capability value -------------------------------------


def _b(transform, **kw):
    return StateBinding(capability="x", topic="t", json_key=kw.pop("json_key", None),
                        transform=transform, **kw)


def test_on_and_off_become_a_boolean():
    assert decode(_b("onoff"), b"ON") is True
    assert decode(_b("onoff"), b"OFF") is False


def test_a_payload_that_is_neither_is_dropped_not_guessed():
    """Returning False for an unknown payload would report a light OFF because the
    bridge sent something unexpected."""
    assert decode(_b("onoff"), b"MAYBE") is None


def test_custom_on_off_payloads_are_honoured():
    assert decode(_b("onoff", payload_on="1", payload_off="0"), b"1") is True


def test_brightness_is_rescaled_to_percent():
    """The scale is per-device. Reading a 0-100 device as 0-255 caps the lamp at
    39 % and nothing ever says so."""
    assert decode(_b("brightness", scale=255.0), b"255") == 100
    assert decode(_b("brightness", scale=255.0), b"128") == 50
    assert decode(_b("brightness", scale=100.0), b"100") == 100


def test_brightness_is_clamped_into_range():
    assert decode(_b("brightness", scale=255.0), b"300") == 100
    assert decode(_b("brightness", scale=255.0), b"-5") == 0


def test_mireds_are_converted_to_kelvin():
    """HA speaks mireds, the canonical model speaks kelvin — and the relationship is
    INVERSE, so getting it wrong gives warm light when cold was asked for."""
    assert decode(_b("mireds"), b"370") == 2703      # ~2700 K warm
    assert decode(_b("mireds"), b"153") == 6536 - 1  # ~6500 K cold, clamped to 6535


def test_zero_mireds_is_dropped_rather_than_dividing_by_zero():
    assert decode(_b("mireds"), b"0") is None


def test_a_json_payload_is_read_by_key():
    assert decode(_b("number", json_key="temperature"), b'{"temperature": 21.5}') == 21.5


def test_a_json_payload_missing_the_key_is_dropped():
    assert decode(_b("number", json_key="temperature"), b'{"humidity": 40}') is None


def test_malformed_json_is_dropped_not_raised():
    assert decode(_b("number", json_key="t"), b"{oops") is None


def test_undecodable_bytes_are_dropped():
    assert decode(_b("onoff"), b"\xff\xfe") is None


def test_a_cover_state_becomes_a_position():
    assert decode(_b("cover", payload_on="open", payload_off="closed"), b"open") == 100
    assert decode(_b("cover", payload_on="open", payload_off="closed"), b"closed") == 0


# --- outbound: command -> (topic, payload) ------------------------------------


def _entity_from(component, payload):
    reg = Registry()
    entity, _new = reg.add_config(component, "obj1", payload)
    return entity


def test_a_switch_config_yields_both_directions():
    entity = _entity_from("switch", {
        "~": "govee/sw1", "stat_t": "~/state", "cmd_t": "~/set",
        "name": "Plug", "dev": {"ids": ["sw1"], "name": "Plug"}})
    assert entity is not None
    assert any(s.capability == "on_off" for s in entity.states)
    assert "on_off" in entity.commands

    spec = entity.commands["on_off"]
    assert encode(spec, "turn_on", {}) == ("govee/sw1/set", "ON")
    assert encode(spec, "turn_off", {}) == ("govee/sw1/set", "OFF")


def test_brightness_is_rescaled_on_the_way_OUT_too():
    """A one-way conversion is the classic bug: the UI reads 50 % and sets 50 raw."""
    entity = _entity_from("light", {
        "~": "govee/l1", "stat_t": "~/state", "cmd_t": "~/set",
        "bri_stat_t": "~/bri", "bri_cmd_t": "~/bri/set", "bri_scl": 255,
        "name": "Lamp", "dev": {"ids": ["l1"], "name": "Lamp"}})
    assert entity is not None and "brightness" in entity.commands
    topic, payload = encode(entity.commands["brightness"], "set_brightness", {"value": 50})
    assert topic == "govee/l1/bri/set"
    assert payload == "128", f"50% of scale 255 should be 128, got {payload}"


def test_an_unexpressible_command_returns_None_rather_than_a_wrong_topic():
    entity = _entity_from("switch", {
        "~": "govee/sw2", "stat_t": "~/state", "cmd_t": "~/set",
        "name": "Plug", "dev": {"ids": ["sw2"], "name": "Plug"}})
    assert encode(entity.commands["on_off"], "set_brightness", {"value": 10}) is None


# --- the registry -------------------------------------------------------------


def test_the_registry_reports_a_config_as_new_only_once():
    """Re-announcing on every bridge restart must not re-announce every entity."""
    reg = Registry()
    cfg = {"~": "govee/s3", "stat_t": "~/state", "cmd_t": "~/set",
           "name": "Plug", "dev": {"ids": ["s3"], "name": "Plug"}}
    _e1, new1 = reg.add_config("switch", "obj", cfg)
    _e2, new2 = reg.add_config("switch", "obj", cfg)
    assert new1 is True and new2 is False


def test_state_topics_are_what_the_adapter_must_subscribe_to():
    reg = Registry()
    reg.add_config("switch", "obj", {
        "~": "govee/s4", "stat_t": "~/state", "cmd_t": "~/set",
        "name": "Plug", "dev": {"ids": ["s4"], "name": "Plug"}})
    assert "govee/s4/state" in reg.state_topics()


def test_a_snapshot_round_trips_through_load():
    """The registry is persisted so a restart does not lose every entity until the
    bridge happens to re-announce."""
    reg = Registry()
    reg.add_config("switch", "obj", {
        "~": "govee/s5", "stat_t": "~/state", "cmd_t": "~/set",
        "name": "Plug", "dev": {"ids": ["s5"], "name": "Plug"}})
    snap = reg.snapshot()

    restored = Registry()
    restored.load(snap)
    assert restored.state_topics() == reg.state_topics()
