"""Tuya's local decode → publish, and the guard that stops a poll undoing a command.

`mapping.py` is at 86.8%; `adapter.py` sits at 9.8% and holds the part that decides
WHAT reaches the bus. Two rules in it are the kind that fail silently:

  * a composite enum spans a power dp and a level dp, so one selector reads
    Off/L1/L2/L3 (the patio heater). Read the level alone and a heater that is OFF
    reports "Level 2" — the UI shows it running, and an automation conditioned on it
    fires against a cold device;
  * Tuya's `status()` lags a cycle behind `set_value()`, so the poll that lands right
    after a command reads the OLD value. Without a guard the UI snaps back the moment
    you press a button, the user presses again, and the device ends up toggled twice.

Both are pure functions of the device dict and the dps payload, so they test without
a socket — which is the whole reason this file exists rather than a mock of tinytuya.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from dida_adapter_tuya.adapter import TuyaAdapter
from dida_core.health import StatusReporter


@pytest.mark.parametrize("scale, raw, expected", [(0, 23, 23), (1, 230, 23), (2, -1250, -12.5)])
@pytest.mark.parametrize("as_json", [False, True])
def test_auto_mapping_preserves_numeric_scale_and_unit_through_canonical_validation(scale, raw, expected, as_json):
    from dida_adapter_tuya.adapter import _readings
    from dida_adapter_tuya.cloud import auto_dps
    from dida_core import validate_state

    values = {"scale": scale, "unit": "°C"}
    mapping = {"1": {"code": "temp_current", "type": "Integer", "values": json.dumps(values) if as_json else values}}
    dps, unmapped = auto_dps(mapping)
    assert not unmapped
    assert dps["1"] == {"cap": "temperature", "scale": scale, "unit": "°C"}
    readings = _readings({"dps": dps}, {"1": raw})
    assert readings == [("temperature", expected)]
    assert validate_state("temperature", readings[0][1]) == expected


def test_manual_numeric_mapping_stays_in_canonical_units():
    from dida_adapter_tuya.adapter import _readings
    assert _readings({"dps": {"1": "temperature"}}, {"1": 23}) == [("temperature", 23)]


def test_explicit_scaled_units_convert_to_canonical_units():
    from dida_adapter_tuya.mapping import decode
    assert decode("temperature", 770, scale=1, unit="°F") == 25
    assert decode("current", 1000, unit="mA") == 1
    assert decode("energy", 500, unit="Wh") == 0.5


@pytest.mark.parametrize("scale", [-1, 13, True, "1"])
def test_invalid_scale_fails_loudly(scale):
    from dida_adapter_tuya.mapping import decode
    with pytest.raises(ValueError, match="scale"):
        decode("temperature", 230, scale=scale)


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, object]] = []

    async def publish_state(self, update) -> None:
        self.published.append((update.entity_id, update.capability, update.value))

    async def publish_entity(self, info) -> None:
        pass


@pytest.fixture
def rig():
    a = TuyaAdapter()
    a.status = StatusReporter("tuya")
    a._bus = _Bus()
    return a


async def _emit(a, dev, dps):
    a._emit(dev, dev["slug"], dps)
    for _ in range(6):
        await asyncio.sleep(0)      # `_emit` spawns the publishes
    return a._bus.published


def _dev(dps, name="Patio Heater"):
    return {"id": "bf1234", "name": name, "slug": "patio_heater", "dps": dps}


async def test_a_plain_capability_is_published(rig):
    out = await _emit(rig, _dev({"1": "on_off"}), {"1": True})
    assert out == [("tuya:patio_heater", "on_off", True)]


async def test_a_dp_absent_from_the_payload_is_skipped(rig):
    """Tuya sends partial dps updates; treating a missing dp as a value would
    publish a default the device never reported."""
    out = await _emit(rig, _dev({"1": "on_off", "2": "temperature"}), {"1": True})
    assert [c for _, c, _ in out] == ["on_off"]


async def test_a_composite_enum_reads_OFF_when_the_power_dp_is_off(rig):
    """The heater is off. Reading the level dp alone would report Level 2 and show a
    cold device as running."""
    dev = _dev({"3": {"cap": "enum", "options": ["Off", "Level 1", "Level 2"],
                      "raw": [None, "level_1", "level_2"], "power_dp": "1"}})
    out = await _emit(rig, dev, {"1": False, "3": "level_2"})
    assert ("tuya:patio_heater", "enum", "Off") in out


async def test_a_composite_enum_reads_the_LEVEL_when_the_power_dp_is_on(rig):
    dev = _dev({"3": {"cap": "enum", "options": ["Off", "Level 1", "Level 2"],
                      "raw": [None, "level_1", "level_2"], "power_dp": "1"}})
    out = await _emit(rig, dev, {"1": True, "3": "level_2"})
    assert ("tuya:patio_heater", "enum", "Level 2") in out


async def test_the_enum_options_travel_with_the_value(rig):
    """Without them the UI has a value and no list to choose from."""
    dev = _dev({"3": {"cap": "enum", "options": ["Off", "Level 1"],
                      "raw": [None, "level_1"], "power_dp": "1"}})
    out = await _emit(rig, dev, {"1": True, "3": "level_1"})
    assert any(c == "enum_options" for _, c, _ in out)


async def test_an_unmapped_raw_code_passes_through_rather_than_vanishing(rig):
    """A firmware update adding a level must not make the selector go blank."""
    dev = _dev({"3": {"cap": "enum", "options": ["Off", "Level 1"],
                      "raw": [None, "level_1"], "power_dp": "1"}})
    out = await _emit(rig, dev, {"1": True, "3": "level_9"})
    assert ("tuya:patio_heater", "enum", "level_9") in out


async def test_an_unchanged_value_is_not_republished(rig):
    """The poll runs continuously; without the dedup every device would write to the
    state firehose several times a minute forever."""
    dev = _dev({"1": "on_off"})
    await _emit(rig, dev, {"1": True})
    rig._bus.published.clear()
    assert await _emit(rig, dev, {"1": True}) == []


async def test_a_LAGGING_poll_does_not_undo_a_command_just_sent(rig):
    """Tuya's status() lags a cycle behind set_value(). Without this guard the UI
    snaps back the instant you press a button — so the user presses again, and the
    device is toggled twice."""
    dev = _dev({"1": "on_off"})
    await _emit(rig, dev, {"1": False})          # known state: off
    rig._bus.published.clear()

    # A command sets it on and blocks contradicting polls for a moment.
    rig._last[("tuya:patio_heater", "on_off")] = True
    rig._cmd_until[("tuya:patio_heater", "on_off")] = asyncio.get_running_loop().time() + 1e6

    assert await _emit(rig, dev, {"1": False}) == [], \
        "a stale poll overwrote the value we had just commanded"


async def test_the_guard_expires_so_a_genuine_change_still_lands(rig):
    """A permanent guard would mean a device switched at the wall never updates."""
    dev = _dev({"1": "on_off"})
    await _emit(rig, dev, {"1": True})
    rig._bus.published.clear()
    rig._cmd_until[("tuya:patio_heater", "on_off")] = 0.0   # already expired

    assert await _emit(rig, dev, {"1": False}) == [("tuya:patio_heater", "on_off", False)]


async def test_a_device_reporting_only_diagnostics_stays_diagnostic(rig):
    """A bare battery probe should not become a tile in the house view.

    `battery`, not `rssi`: the first draft of this used `rssi`, which is not a
    capability at all — `decode` returned None, `_emit` bailed on an empty reading
    list, and BOTH this test and the sticky one below passed without touching the
    curation logic. Sabotaging that logic left them green, which is how it was
    caught. A diagnostic capability that actually decodes is the whole test."""
    dev = _dev({"9": "battery"})
    out = await _emit(rig, dev, {"9": 80})
    assert out, "the fixture must produce a real reading, or this proves nothing"
    assert "tuya:patio_heater" not in rig._curated


async def test_one_primary_capability_makes_the_whole_entity_curated(rig):
    """A plug that also reports battery must NOT be hidden as diagnostic because
    of it."""
    dev = _dev({"1": "on_off", "9": "battery"})
    await _emit(rig, dev, {"1": True, "9": 80})
    assert "tuya:patio_heater" in rig._curated


async def test_curation_is_STICKY_across_partial_updates(rig):
    """Tuya sends partial dps: a later update carrying only the diagnostic dp must
    not flip an already-curated device back to hidden."""
    dev = _dev({"1": "on_off", "9": "battery"})
    await _emit(rig, dev, {"1": True, "9": 80})
    out = await _emit(rig, dev, {"9": 70})
    assert out, "the second update must carry a reading, or stickiness is untested"
    assert "tuya:patio_heater" in rig._curated


async def test_nothing_decodable_publishes_nothing(rig):
    out = await _emit(rig, _dev({"1": "on_off"}), {"7": "unknown-dp"})
    assert out == []


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


async def test_only_the_device_past_the_poll_horizon_flips(rig):
    import time as _time

    now = _time.monotonic()
    rig._seen = {"dead1": now - 9999, "live1": now}
    rig._bus = _ReachBus()
    desired = {
        "dead1": {"id": "dead1", "name": "Patio Heater"},
        "live1": {"id": "live1", "name": "Desk Plug"},
    }
    await rig._publish_reach_verdicts(desired, now - 60)
    assert ("patio_heater", False) in _rkeys(rig._bus)
    assert ("desk_plug", True) in _rkeys(rig._bus)
    assert len(rig._bus.reach) == 2
    await rig._publish_reach_verdicts(desired, now - 60)
    assert len(rig._bus.reach) == 2, "an unchanged verdict is not re-published"
