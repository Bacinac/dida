"""Regression tests for the pure device-type classifier (dida_core.capabilities).

classify_device_type() derives DIDA's canonical entities.device_type from an
entity's capability set (mirrors the frontend deviceType() — see the code
comment on keeping them in sync); resolve_device_type() layers the adapter's
native type hint on top, falling back to classify_device_type() when the hint
is absent or unmapped. Both are pure — no I/O — seed-once values consumed by
the engine.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_device_type.py"
"""
from dida_core.capabilities import DEVICE_TYPES, classify_device_type, resolve_device_type


def test_classify_device_type_actuators_before_sensor_catchall():
    # order matters: a light also reports `power` (a sensor capability), but
    # on_off must win over the sensor catch-all.
    assert classify_device_type({"on_off", "power"}) == "switch", \
        "on_off (no brightness/color_temp) -> switch, even though it also reports power"
    assert classify_device_type({"on_off", "brightness", "power"}) == "light", \
        "on_off + brightness -> light"
    assert classify_device_type({"on_off", "color_temp"}) == "light", \
        "on_off + color_temp (no brightness) -> light"


def test_classify_device_type_media_transport_wins_first():
    assert classify_device_type({"media_transport", "on_off"}) == "media", \
        "media_transport is checked before on_off"


def test_classify_device_type_remote_needs_source_and_press():
    assert classify_device_type({"source", "press"}) == "remote", \
        "source + press together -> remote"
    assert classify_device_type({"source"}) == "other", \
        "source alone (no press) is not enough for remote"
    assert classify_device_type({"press"}) == "other", \
        "press alone (no source) is not enough for remote either"


def test_classify_device_type_button_capability():
    assert classify_device_type({"button"}) == "button", \
        "the BUTTON capability (momentary event source) classifies as button"


def test_classify_device_type_presence_lock_cover():
    assert classify_device_type({"location"}) == "presence"
    assert classify_device_type({"lock"}) == "lock"
    assert classify_device_type({"open_close"}) == "cover"


def test_classify_device_type_sensor_catchall():
    assert classify_device_type({"temperature"}) == "sensor"
    assert classify_device_type({"battery", "signal"}) == "sensor"


def test_classify_device_type_other_is_the_last_resort():
    assert classify_device_type(set()) == "other", "no capabilities at all -> other"
    assert classify_device_type(None) == "other", "None capabilities -> other (falsy -> empty set)"
    assert classify_device_type({"unmapped_cap"}) == "other", \
        "a capability outside every check -> other"


def test_classify_device_type_result_is_always_a_known_device_type():
    for caps in (set(), {"on_off"}, {"lock"}, {"open_close"}, {"temperature"},
                 {"button"}, {"location"}, {"media_transport"}, {"source", "press"}):
        assert classify_device_type(caps) in DEVICE_TYPES, \
            f"classify_device_type({caps!r}) must be one of DEVICE_TYPES"


def test_resolve_device_type_adapter_hint_wins_when_mapped():
    assert resolve_device_type("light", {"on_off"}) == "light", \
        "a mapped adapter hint is used as-is, even if capabilities alone would say switch"
    assert resolve_device_type("binary_sensor", set()) == "sensor", \
        "binary_sensor hint maps to sensor"
    assert resolve_device_type("media_player", set()) == "media", "media_player hint maps to media"


def test_resolve_device_type_unmapped_hint_falls_back_to_classification():
    assert resolve_device_type("number", {"temperature"}) == "sensor", \
        "an adapter hint with no entry in the hint map falls back to classify_device_type()"


def test_resolve_device_type_no_hint_falls_back_to_classification():
    assert resolve_device_type(None, {"on_off", "brightness"}) == "light", \
        "no hint -> classify_device_type() decides"
    assert resolve_device_type("", {"lock"}) == "lock", "empty-string hint is falsy -> classify"
