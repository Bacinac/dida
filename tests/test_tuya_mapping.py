"""Regression tests for the Tuya DPS <-> canonical mapping.

Run inside the tuya adapter image (dida_adapter_tuya installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-tuya:latest \
      -c "python -m pytest tests/test_tuya_mapping.py"

Covers decode (DPS value -> canonical: the 0..1000 scale, the 2700..6500 K color
temp, the HSV colour hex), encode (canonical command -> DPS: the brightness floor
of 10, the reverse scales), and a color_temp + colour round-trip.
"""
from dida_adapter_tuya.mapping import decode, encode


# --- decode: Tuya DPS -> canonical ------------------------------------------
def test_decode():
    assert decode("on_off", True) is True, "on_off bool True"
    assert decode("on_off", "on") is True and decode("on_off", "false") is False, "on_off strings"
    assert decode("brightness", 1000) == 100, "brightness 1000 -> 100 %"
    assert decode("brightness", 500) == 50, "brightness 500 -> 50 %"
    assert decode("color_temp", 0) == 2700, "color_temp 0 -> 2700 K (warm end)"
    assert decode("color_temp", 1000) == 6500, "color_temp 1000 -> 6500 K (cool end)"
    assert decode("color_temp", 500) == 4600, "color_temp 500 -> 4600 K (midpoint)"
    assert decode("open_close", 50) == 50, "cover position 50 -> 50"
    assert decode("temperature", 21.5) == 21.5, "temperature passthrough"
    assert decode("unmappable_cap", 1) is None, "unknown capability -> None"

    # HSV colour hex (HHHHSSSSVVVV) -> #RRGGBB
    assert decode("color_rgb", "000003e803e8") == "#ff0000", "HSV hue0/sat1000/val1000 -> red"
    assert decode("color_rgb", "short") is None, "malformed colour -> None"


# --- encode: canonical command -> Tuya DPS ----------------------------------
def test_encode():
    assert encode("on_off", "turn_on", {}) is True, "turn_on -> True"
    assert encode("on_off", "turn_off", {}) is False, "turn_off -> False"
    assert encode("brightness", "set_brightness", {"value": 100}) == 1000, "brightness 100% -> 1000"
    assert encode("brightness", "set_brightness", {"value": 0}) == 10, "brightness 0% floors at 10 (Tuya min)"
    assert encode("color_temp", "set_color_temp", {"value": 2700}) == 0, "2700 K -> 0"
    assert encode("color_temp", "set_color_temp", {"value": 6500}) == 1000, "6500 K -> 1000"
    assert encode("open_close", "set_position", {"value": 30}) == 30, "set_position 30 -> 30"
    assert encode("open_close", "open", {}) == "open", "open -> 'open'"


# --- round-trips ------------------------------------------------------------
def test_round_trips():
    assert decode("color_temp", encode("color_temp", "set_color_temp", {"value": 4600})) == 4600, \
        "color_temp 4600 K round-trips"
    rgb = encode("color_rgb", "set_color", {"value": "#00ff00"})
    assert decode("color_rgb", rgb) == "#00ff00", "green round-trips through Tuya HSV hex"
