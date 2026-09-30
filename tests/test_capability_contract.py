"""Regression tests for the capability contract hardening (audit M3/M4).

Run inside any DIDA image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_capability_contract.py"

Asserts the string refinements (choices / pattern / JSON), write-only rejection,
and command-argument validation accept legitimate values and reject bad ones.
"""
import pytest
from dida_core.capabilities import (
    CapabilityError,
    setter_command,
    validate_command_args,
    validate_state,
)


def test_validate_state_accepts_legitimate_values():
    # legitimate values (union of what the midea + esphome adapters emit) must pass
    for cap, val in [
        ("hvac_mode", "cool"), ("hvac_mode", "heat_cool"),
        ("fan_mode", "silent"), ("fan_mode", "medium"),
        ("media_transport", "buffering"), ("mower", "starting"),
        ("color_rgb", "#a1b2c3"), ("enum_options", '["a","b"]'),
        ("media_queue", '{"items":[],"current":0}'),
        ("media_queue", ""),  # "" = cleared (wire convention for blanking metadata)
        ("temperature", 21.5), ("brightness", 100),
    ]:
        validate_state(cap, val)


def test_validate_state_rejects_bad_values():
    # bad values must be rejected (fail loud)
    for cap, val in [
        ("hvac_mode", "colling"), ("fan_mode", "turbo"),
        ("media_transport", "playing_ad"), ("color_rgb", "banana"),
        ("color_rgb", "#12"), ("enum_options", "not json{"),
        ("media_queue", "{bad"), ("notify", "x"), ("press", "x"),
    ]:
        with pytest.raises(CapabilityError):
            validate_state(cap, val)


def test_time_capability():
    # `time` = a SETTABLE minutes-since-midnight (the writable sibling of the
    # read-only time_of_day). The value type for a "time helper" a household edits.
    assert validate_state("time", 0) == 0, "00:00"
    assert validate_state("time", 1380) == 1380, "23:00"
    assert validate_state("time", 1439) == 1439, "23:59"
    for bad in (-1, 1440, 2000):
        with pytest.raises(CapabilityError):
            validate_state("time", bad)
    # its setter is set_time; a value in range passes, out of range / wrong cmd fail
    validate_command_args("time", "set_time", {"value": 420})
    with pytest.raises(CapabilityError):
        validate_command_args("time", "set_time", {"value": 1500})
    with pytest.raises(CapabilityError):
        validate_command_args("time", "set_value", {"value": 420})  # wrong command


def test_identity_presence_capability():
    # A recognised person's presence-with-confidence at a camera (BABA vision plane).
    # READ-only enum ladder: absent < body < face. An automation gates on one value.
    for v in ("absent", "body", "face"):
        assert validate_state("identity_presence", v) == v
    for bad in ("present", "FACE", "gait", ""):
        with pytest.raises(CapabilityError):
            validate_state("identity_presence", bad)
    # READ-only: a state update is fine, but there is no command to set it
    with pytest.raises(CapabilityError):
        validate_command_args("identity_presence", "set_identity_presence", {"value": "face"})


def test_identity_since_is_a_registered_capability():
    """The footnote under identity_presence — "present since 14:02".

    It was emitted by the baba adapter, declared on the entity, typed in the UI and
    rendered by three surfaces, but never registered HERE — so the engine rejected
    every publish at the boundary and the footnote silently showed nothing. The
    device journal is what surfaced it (23 rejections in one production boot); this
    test is what keeps it registered."""
    assert validate_state("identity_since", "2026-08-07T10:42:00Z") == "2026-08-07T10:42:00Z"
    assert validate_state("identity_since", "") == "", "empty means the person is absent"
    with pytest.raises(CapabilityError):
        validate_state("identity_since", 42)
    # READ-only, like its presence sibling: BABA observes it, nobody sets it.
    with pytest.raises(CapabilityError):
        validate_command_args("identity_since", "set_identity_since", {"value": "x"})


def test_validate_command_args():
    validate_command_args("brightness", "set_brightness", {"value": 50})
    validate_command_args("hvac_mode", "set_hvac_mode", {"value": "cool"})
    validate_command_args("media_transport", "play", {})
    validate_command_args("media_transport", "play_media", {"uri": "x"})
    with pytest.raises(CapabilityError):
        validate_command_args("brightness", "set_brightness", {"value": 100000})
    with pytest.raises(CapabilityError):
        validate_command_args("target_temperature", "set_temperature", {"value": "high"})
    with pytest.raises(CapabilityError):
        validate_command_args("hvac_mode", "set_hvac_mode", {"value": "colling"})
    with pytest.raises(CapabilityError):
        validate_command_args("color_rgb", "set_color", {"value": "notahex"})
    with pytest.raises(CapabilityError):
        validate_command_args("on_off", "explode", {})
    # A set_<x> with NO value (or a mis-keyed value) is garbage too — it must be
    # rejected here, not KeyError / silently no-op at the adapter.
    with pytest.raises(CapabilityError):
        validate_command_args("brightness", "set_brightness", {})
    with pytest.raises(CapabilityError):
        validate_command_args("target_temperature", "set_temperature", None)
    with pytest.raises(CapabilityError):
        validate_command_args("open_close", "set_position", {"position": 50})


def test_setter_command():
    # setter_command: how scene recall turns a captured state back into a command.
    def setter_is(cap, val, expected):
        got = setter_command(cap, val)
        assert got == expected, f"setter_command({cap!r},{val!r}) = {got}, expected {expected}"

    setter_is("on_off", True, ("turn_on", {}))
    setter_is("on_off", False, ("turn_off", {}))
    setter_is("brightness", 128, ("set_brightness", {"value": 128}))
    setter_is("color_rgb", "#aabbcc", ("set_color", {"value": "#aabbcc"}))
    setter_is("open_close", 50, ("set_position", {"value": 50}))
    # A cover captured as a bare open/closed bool must use the open/close verbs: the
    # old set_-first scan returned set_position {value: True}, which validate_command_args
    # rejects (position wants a number) — so recalling such a scene silently did nothing.
    setter_is("open_close", True, ("open", {}))
    setter_is("open_close", False, ("close", {}))
    setter_is("lock", True, ("lock", {}))
    setter_is("lock", False, ("unlock", {}))
    setter_is("hvac_mode", "cool", ("set_hvac_mode", {"value": "cool"}))
    setter_is("target_temperature", 21.5, ("set_temperature", {"value": 21.5}))
    # Sensors + momentary verbs have no settable state → excluded from scenes.
    setter_is("temperature", 20.0, None)   # the ambient READ sensor, not the setpoint
    setter_is("occupancy", True, None)
    setter_is("illuminance", 42, None)
    setter_is("press", "x", None)
    setter_is("scene_state", "idle", None)


def test_generic_number_and_enum_respect_the_device_own_limits():
    """`number` and `enum` are open at the SPEC level on purpose — the real range /
    option list is device metadata (number_options / enum_options). Without checking
    it, the boundary waved through a value the device never offered."""
    from dida_core.capabilities import validate_command_args, validate_runtime_options

    # No metadata published → nothing to check, the spec alone decides (unchanged).
    validate_command_args("number", "set_value", {"value": 1e6})

    opts = '{"min": 0, "max": 100, "step": 1}'
    validate_command_args("number", "set_value", {"value": 50}, opts)  # inside → fine
    for bad in (1e308, -1, 101):
        with pytest.raises(CapabilityError):
            validate_command_args("number", "set_value", {"value": bad}, opts)

    enum_opts = '["eco", "comfort", "boost"]'
    validate_command_args("enum", "set_option", {"value": "eco"}, enum_opts)
    with pytest.raises(CapabilityError):
        validate_command_args("enum", "set_option", {"value": "turbo"}, enum_opts)

    # Malformed or empty metadata must not block a legitimate command — that would
    # turn one adapter's bug into an unusable device.
    validate_runtime_options("number", 5, "not json")
    validate_runtime_options("enum", "eco", "")
    validate_runtime_options("enum", "eco", "[]")


# --- command arguments --------------------------------------------------------


@pytest.mark.parametrize("value", [True, 1, 1.5, "MUSIC"])
def test_a_scalar_argument_is_accepted(value):
    validate_command_args("media_transport", "play_media", {"value": value})


@pytest.mark.parametrize("value", [["a"], {"k": "v"}, None, (1, 2)])
def test_a_NON_scalar_argument_is_rejected_loudly(value):
    """msgspec encodes it happily; every adapter then drops the whole command as
    undecodable. Rejecting it at the sender is the only place it can be loud."""
    with pytest.raises(CapabilityError):
        validate_command_args("media_transport", "play_media", {"value": value})


def test_the_rejection_names_the_argument_and_its_type():
    """The rule author has to find this in a script; "invalid args" would not help."""
    with pytest.raises(CapabilityError) as e:
        validate_command_args("media_transport", "play_media", {"colour": ["r", "g", "b"]})
    assert "colour" in str(e.value) and "list" in str(e.value)


class _Options:
    def __init__(self, options):
        self.options, self.asked = options, []

    async def fetchval(self, sql, entity_id, capability):
        self.asked.append((entity_id, capability))
        return self.options


def test_every_sender_gets_the_devices_own_limits():
    """The api checked a `number` against the limits the device published; a rule,
    a scene and the assistant did not, so 1e308 reached the adapter from all three.
    They now build the command the same way."""
    import asyncio

    from dida_core import prepare_command

    pool = _Options('{"min": 0, "max": 40}')
    with pytest.raises(CapabilityError):
        asyncio.run(prepare_command(pool, "mqtt:boiler", "number", "set_value",
                                    {"value": 1e308}, source="automation:1:x"))
    assert pool.asked == [("mqtt:boiler", "number_options")]
    cmd = asyncio.run(prepare_command(pool, "mqtt:boiler", "number", "set_value",
                                      {"value": 21}, source="automation:1:x"))
    assert (cmd.entity_id, cmd.args, cmd.source) == ("mqtt:boiler", {"value": 21}, "automation:1:x")
