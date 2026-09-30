"""Regression tests for the Panasonic Comfort Cloud <-> canonical mapping.

Run inside the panasonic adapter image (dida_adapter_panasonic + the vendor
library installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-panasonic:latest \
      -c "python -m pytest tests/test_panasonic_mapping.py"

Covers the two translations that can silently corrupt a climate device: the
power/mode fold into `hvac_mode` (a unit that is OFF must never report the mode
it will resume in), and the fan ladder, whose canonical names differ from the
vendor's but whose ORDER must not — plus the mode list, which has to describe
the unit in front of us rather than the protocol's full vocabulary.
"""
import json

import pytest
from aio_panasonic_comfort_cloud import constants
from dida_adapter_panasonic.adapter import PanasonicAdapter
from dida_core.capabilities import CAPABILITIES, CapabilityKind


class _Params:
    def __init__(self, power, mode=constants.OperationMode.Cool,
                 fan_speed=constants.FanSpeed.Auto):
        self.power = power
        self.mode = mode
        self.fan_speed = fan_speed


class _Features:
    def __init__(self, cool=True, heat=True, dry=True, fan=True):
        self.cool_mode, self.heat_mode, self.dry_mode, self.fan_mode = cool, heat, dry, fan


class _Info:
    def __init__(self, auto_mode=True):
        self.auto_mode = auto_mode


@pytest.fixture
def adapter():
    a = PanasonicAdapter()
    a._build_maps()
    return a


# --- hvac_mode ------------------------------------------------------------------

def test_off_beats_the_mode_it_will_resume_in(adapter):
    p = _Params(constants.Power.Off, mode=constants.OperationMode.Heat)
    assert adapter._hvac_mode(p) == "off"


@pytest.mark.parametrize("mode,expected", [
    (constants.OperationMode.Auto, "auto"),
    (constants.OperationMode.Dry, "dry"),
    (constants.OperationMode.Cool, "cool"),
    (constants.OperationMode.Heat, "heat"),
    (constants.OperationMode.Fan, "fan_only"),
])
def test_every_running_mode_maps(adapter, mode, expected):
    assert adapter._hvac_mode(_Params(constants.Power.On, mode=mode)) == expected


def test_published_modes_are_all_canonical(adapter):
    allowed = set(CAPABILITIES[CapabilityKind.HVAC_MODE].choices)
    assert set(adapter._mode_to_str.values()) <= allowed
    assert "off" in allowed


# --- mode options ---------------------------------------------------------------

def test_options_describe_this_unit_not_the_protocol(adapter):
    opts = adapter._mode_options(_Info(auto_mode=False), _Features(heat=False, dry=False))
    assert opts == ["off", "cool", "fan_only"]


def test_options_are_a_subset_of_the_canonical_choices(adapter):
    opts = adapter._mode_options(_Info(), _Features())
    assert set(opts) <= set(CAPABILITIES[CapabilityKind.HVAC_MODE].choices)


# --- fan ladder ------------------------------------------------------------------

def test_fan_names_are_canonical(adapter):
    allowed = set(CAPABILITIES[CapabilityKind.FAN_MODE].choices)
    assert set(adapter._fan_to_str.values()) <= allowed


def test_fan_ladder_keeps_the_vendor_order(adapter):
    F = constants.FanSpeed
    speeds = [F.Low, F.LowMid, F.Mid, F.HighMid, F.High]
    ladder = ["silent", "low", "medium", "high", "max"]
    assert [adapter._fan_to_str[s] for s in speeds] == ladder


def test_fan_round_trips_both_ways(adapter):
    for speed, name in adapter._fan_to_str.items():
        assert adapter._str_to_fan[name] == speed


def test_no_two_speeds_collapse_onto_one_name(adapter):
    names = list(adapter._fan_to_str.values())
    assert len(names) == len(set(names))


# --- command mapping --------------------------------------------------------------

class _Builder:
    """Stands in for ChangeRequestBuilder — records what the adapter asked for."""

    def __init__(self):
        self.calls = {}

    def set_power_mode(self, v): self.calls["power"] = v
    def set_hvac_mode(self, v): self.calls["mode"] = v
    def set_target_temperature(self, v): self.calls["temp"] = v
    def set_fan_speed(self, v): self.calls["fan"] = v
    def set_eco_mode(self, v): self.calls["eco"] = v
    def set_nanoe_mode(self, v): self.calls["nanoe"] = v
    def set_vertical_swing(self, v): self.calls["swing_ud"] = v
    def set_horizontal_swing(self, v): self.calls["swing_lr"] = v
    def set_eco_navi_mode(self, v): self.calls["eco_navi"] = v
    def set_zone_mode(self, zid, v): self.calls["zone"] = (zid, v)
    def set_zone_damper(self, zid, v): self.calls["damper"] = (zid, v)


class _Cmd:
    def __init__(self, capability, args, command=""):
        self.entity_id = "panasonic:x"
        self.capability = capability
        self.command = command
        self.args = args


def test_off_switches_power_and_leaves_the_mode_alone(adapter):
    b = _Builder()
    adapter._apply_command(b, _Cmd("hvac_mode", {"value": "off"}), "", None)
    assert b.calls == {"power": constants.Power.Off}


def test_a_mode_also_powers_the_unit_on(adapter):
    b = _Builder()
    adapter._apply_command(b, _Cmd("hvac_mode", {"value": "heat"}), "", None)
    assert b.calls == {"power": constants.Power.On, "mode": constants.OperationMode.Heat}


def test_setpoint_and_fan_reach_the_builder(adapter):
    b = _Builder()
    adapter._apply_command(b, _Cmd("target_temperature", {"value": "22.5"}), "", None)
    adapter._apply_command(b, _Cmd("fan_mode", {"value": "max"}), "", None)
    assert b.calls == {"temp": 22.5, "fan": constants.FanSpeed.High}


def test_an_unknown_fan_name_is_rejected_not_guessed(adapter):
    with pytest.raises(KeyError):
        adapter._apply_command(_Builder(), _Cmd("fan_mode", {"value": "turbo"}), "", None)


# --- facets (eco, nanoe, swing, ECONAVI…) ------------------------------------------

class _FacetParams:
    """Parameters carrying every facet attribute, all available by default."""

    def __init__(self, **over):
        self.eco_mode = constants.EcoMode.Auto
        self.nanoe_mode = constants.NanoeMode.On
        self.vertical_swing_mode = constants.AirSwingUD.Auto
        self.horizontal_swing_mode = constants.AirSwingLR.Mid
        self.eco_navi_mode = constants.EcoNaviMode.Off
        self.eco_function_mode = constants.EcoFunctionMode.Off
        self.iautox_mode = constants.IAutoXMode.Off
        for k, v in over.items():
            setattr(self, k, v)


class _FacetFeatures:
    def __init__(self, **over):
        self.nanoe = True
        self.auto_swing_ud = True
        self.air_swing_lr = True
        self.eco_navi = True
        for k, v in over.items():
            setattr(self, k, v)


def test_a_unit_without_the_hardware_gets_no_facet(adapter):
    facet = adapter._facets["nanoe"]
    assert not facet.supported(_FacetFeatures(nanoe=False), _FacetParams())


def test_an_unavailable_reading_is_not_a_control(adapter):
    facet = adapter._facets["swing_lr"]
    params = _FacetParams(horizontal_swing_mode=constants.AirSwingLR.Unavailable)
    assert not facet.supported(_FacetFeatures(), params)


def test_a_present_facet_is_offered(adapter):
    assert adapter._facets["eco"].supported(_FacetFeatures(), _FacetParams())


def test_enum_facets_read_and_round_trip(adapter):
    for key in ("eco", "nanoe", "swing_ud", "swing_lr"):
        facet = adapter._facets[key]
        name = facet.read(_FacetParams())
        assert name in facet.options
        assert facet.to_vendor(name) == getattr(_FacetParams(), facet.attr)


def test_enum_facet_command_reaches_its_own_setter(adapter):
    b = _Builder()
    adapter._apply_command(b, _Cmd("enum", {"value": "powerful"}), "eco", _FacetParams())
    adapter._apply_command(b, _Cmd("enum", {"value": "swing"}), "swing_ud", _FacetParams())
    assert b.calls == {"eco": constants.EcoMode.Powerful, "swing_ud": constants.AirSwingUD.Swing}


def test_toggle_facet_maps_on_off_and_toggle(adapter):
    off = _FacetParams(eco_navi_mode=constants.EcoNaviMode.Off)
    b = _Builder()
    adapter._apply_command(b, _Cmd("boolean", {}, "turn_on"), "eco_navi", off)
    assert b.calls["eco_navi"] == constants.EcoNaviMode.On
    adapter._apply_command(b, _Cmd("boolean", {}, "toggle"), "eco_navi", off)
    assert b.calls["eco_navi"] == constants.EcoNaviMode.On  # it was off → toggle turns it on
    on = _FacetParams(eco_navi_mode=constants.EcoNaviMode.On)
    adapter._apply_command(b, _Cmd("boolean", {}, "toggle"), "eco_navi", on)
    assert b.calls["eco_navi"] == constants.EcoNaviMode.Off


# --- zones (ducted units) -----------------------------------------------------------

class _Zone:
    def __init__(self, zid, mode):
        self.id, self.mode = zid, mode


class _ZoneParams:
    def __init__(self, zone):
        self._zone = zone

    def get_zone(self, zid):
        assert zid == self._zone.id
        return self._zone


def test_zone_switch_addresses_its_own_zone(adapter):
    b = _Builder()
    params = _ZoneParams(_Zone(3, constants.ZoneMode.Off))
    adapter._apply_command(b, _Cmd("boolean", {}, "turn_on"), "zone:3", params)
    assert b.calls["zone"] == (3, constants.ZoneMode.On)


def test_zone_toggle_reads_the_zone_it_is_toggling(adapter):
    b = _Builder()
    params = _ZoneParams(_Zone(2, constants.ZoneMode.On))
    adapter._apply_command(b, _Cmd("boolean", {}, "toggle"), "zone:2", params)
    assert b.calls["zone"] == (2, constants.ZoneMode.Off)


def test_zone_damper_takes_a_percentage(adapter):
    b = _Builder()
    adapter._apply_command(b, _Cmd("number", {"value": "40.0"}, "set_value"), "zone_level:1", None)
    assert b.calls["damper"] == (1, 40)


# --- blocked-account reasons --------------------------------------------------------

def test_updated_terms_are_named_as_an_action(adapter):
    from aio_panasonic_comfort_cloud import exceptions as pexc
    reason = adapter._blocked_reason(pexc.AgreementNotAcceptedError([1, 2]))
    assert "Comfort Cloud app" in reason and "accept" in reason


def test_two_factor_and_bad_password_are_distinguished(adapter):
    from aio_panasonic_comfort_cloud import exceptions as pexc
    assert "2FA" in adapter._blocked_reason(pexc.MFARequiredError())
    assert "rejected" in adapter._blocked_reason(pexc.LoginError("nope"))


def test_a_flaky_cloud_is_not_reported_as_the_user_s_fault(adapter):
    assert adapter._blocked_reason(TimeoutError("gateway timeout")) is None


# --- published metadata -----------------------------------------------------------

def test_fan_options_are_json_and_unique(adapter):
    opts = json.loads(json.dumps(list(dict.fromkeys(adapter._fan_to_str.values()))))
    assert opts[0] == "auto" and len(opts) == len(set(opts))
