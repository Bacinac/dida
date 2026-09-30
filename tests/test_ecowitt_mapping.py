"""Ecowitt: imperial wire units -> metric capabilities, and the push allowlist.

Two things worth pinning. The unit conversions feed automations and the heating
controller — a Fahrenheit value written into a `temperature` capability would be
accepted by the boundary (it is a plausible float) and quietly drive the house
from a wrong number, which is exactly the failure the capability model cannot
catch for us.

The allowlist is a security boundary: the push receiver listens on the IoT VLAN
with no authentication, so without it any host on that network could mint
arbitrary `ecowitt:<anything>:*` entities.
"""
from __future__ import annotations

import logging

import pytest
from dida_adapter_ecowitt.adapter import (
    _PUSH_MAP,
    EcowittAdapter,
    _f_to_c,
    _in_to_mm,
    _inhg_to_hpa,
    _mph_to_ms,
    _num,
)

# --- unit conversions ---------------------------------------------------------

def test_fahrenheit_becomes_celsius():
    assert _f_to_c(32) == 0.0
    assert _f_to_c(212) == 100.0
    assert _f_to_c(68) == 20.0


def test_inches_of_mercury_become_hectopascals():
    # 29.92 inHg is standard sea-level pressure ≈ 1013.2 hPa
    assert 1013.0 <= _inhg_to_hpa(29.92) <= 1013.5


def test_miles_per_hour_become_metres_per_second():
    assert _mph_to_ms(0) == 0.0
    assert 4.4 <= _mph_to_ms(10) <= 4.5


def test_inches_become_millimetres():
    assert _in_to_mm(1) == 25.4


def test_conversions_accept_the_STRINGS_the_gateway_actually_sends():
    # The push body is form-encoded — every value arrives as text.
    assert _f_to_c("68") == 20.0
    assert _in_to_mm("1") == 25.4


def test_every_mapped_field_names_a_real_capability():
    from dida_core.capabilities import CAPABILITIES
    for field, (suffix, cap, _conv) in _PUSH_MAP.items():
        assert cap in CAPABILITIES, f"{field} maps to unregistered capability {cap!r}"
        assert suffix in ("outdoor", "indoor")


def test_a_number_is_extracted_from_a_unit_suffixed_reading():
    assert _num("12.5 C") == 12.5
    assert _num("-3") == -3.0
    assert _num("no digits here") is None


# --- the push allowlist: an unauthenticated LAN receiver -----------------------

class StubStatus:
    """What `adapter_runner` injects as `adapter.status`. Every adapter reports its
    connection state through it, so a harness that omits it does not exercise the
    real code path — it crashes on the first status update."""

    def __init__(self):
        self.calls = []

    def ok(self, detail=""):
        self.calls.append(("ok", detail))

    def error(self, detail=""):
        self.calls.append(("error", detail))

    def idle(self, detail=""):
        self.calls.append(("idle", detail))

    def connecting(self, detail=""):
        self.calls.append(("connecting", detail))


def _adapter(stations: str | None = None):
    a = EcowittAdapter()
    a._cfg = {"stations": stations} if stations is not None else {}
    a._pinned_station = None
    a._rejected_warned = set()
    a.status = StubStatus()
    return a


def test_with_no_config_the_FIRST_station_is_pinned_and_others_rejected():
    """Bounded to one without configuration — otherwise any host on the IoT VLAN
    could create arbitrary entities just by POSTing."""
    a = _adapter()
    assert a._push_allowed("gw2000a") is True, "the first station seen is adopted"
    assert a._pinned_station == "gw2000a"
    assert a._push_allowed("gw2000a") is True, "…and keeps being accepted"
    assert a._push_allowed("someone_elses") is False, "a second station is refused"


def test_an_explicit_allowlist_wins_over_pinning():
    a = _adapter("gw2000a,wh2650")
    assert a._push_allowed("gw2000a") is True
    assert a._push_allowed("wh2650") is True
    assert a._push_allowed("intruder") is False
    assert a._pinned_station is None, "pinning is not used when an allowlist exists"


def test_the_allowlist_tolerates_spacing_and_empty_entries():
    a = _adapter(" gw2000a , , wh2650 ")
    assert a._push_allowed("gw2000a") is True
    assert a._push_allowed("wh2650") is True


# --- emitting ------------------------------------------------------------------

class StubBus:
    def __init__(self):
        self.states = []

    async def publish_state(self, u):
        self.states.append(u)


def _emitting(stations=None):
    a = _adapter(stations)
    a._bus = StubBus()
    return a


@pytest.mark.asyncio
async def test_a_push_becomes_metric_capabilities_on_one_named_device():
    import asyncio
    a = _emitting()
    a._emit_push({"model": "GW2000A", "tempf": "68", "humidity": "55",
                  "tempinf": "70", "baromrelin": "29.92"})
    for _ in range(6):
        await asyncio.sleep(0)
    by = {(u.entity_id, u.capability): u.value for u in a._bus.states}
    assert by[("ecowitt:gw2000a:outdoor", "temperature")] == 20.0, "converted, not raw °F"
    assert by[("ecowitt:gw2000a:indoor", "temperature")] == 21.11
    assert by[("ecowitt:gw2000a:outdoor", "humidity")] == 55.0
    # outdoor + indoor are facets of ONE station → one card
    assert {u.device for u in a._bus.states} == {"ecowitt:gw2000a"}


@pytest.mark.asyncio
async def test_a_firmware_suffix_does_not_rename_every_entity():
    """An OTA bump changes `model` from EasyWeatherPro_V5.2.1 to _V5.3.0. Left in
    the entity id, that would create a whole new device and split its history."""
    import asyncio
    for model in ("EasyWeatherPro_V5.2.1", "EasyWeatherPro_V5.3.0", "EasyWeatherPro"):
        a = _emitting()
        a._emit_push({"model": model, "tempf": "68"})
        for _ in range(4):
            await asyncio.sleep(0)
        assert a._bus.states[0].entity_id == "ecowitt:easyweatherpro:outdoor", model


@pytest.mark.asyncio
async def test_a_rejected_station_emits_NOTHING_and_warns_once(caplog):
    import asyncio
    a = _emitting("known")
    with caplog.at_level(logging.WARNING, logger="dida.adapter.ecowitt"):
        a._emit_push({"model": "intruder", "tempf": "68"})
        a._emit_push({"model": "intruder", "tempf": "69"})
        for _ in range(4):
            await asyncio.sleep(0)
    assert a._bus.states == [], "an unallowed station cannot mint entities"
    warned = [r for r in caplog.records if "intruder" in r.getMessage()]
    assert len(warned) == 1, "loud once, then quiet — a spamming host must not flood the log"


@pytest.mark.asyncio
async def test_missing_and_unparseable_fields_are_skipped_not_published_as_junk():
    import asyncio
    a = _emitting()
    a._emit_push({"model": "gw", "tempf": "", "humidity": None, "baromrelin": "n/a", "uv": "3"})
    for _ in range(4):
        await asyncio.sleep(0)
    caps = {u.capability for u in a._bus.states}
    assert caps == {"uv_index"}, "only the field that actually carried a value"


class _ReachBus:
    """Captures the reachability verdicts an adapter publishes."""

    def __init__(self):
        self.verdicts: list[tuple[str, bool, str]] = []

    async def publish_reachability(self, ev):
        self.verdicts.append((ev.device_key, ev.reachable, ev.detail))

    async def publish_state(self, _u):
        pass


@pytest.mark.asyncio
async def test_a_station_that_stops_uploading_is_reported_gone():
    """A pushing console has no connection to lose, so silence is the only signal it
    can give — and silence is what nothing notices. The WS2900 here spent seven weeks
    uploading to an address a host move had taken away: no error anywhere, entities
    simply frozen, and it surfaced only because somebody read a device list."""
    import time as _t

    from dida_adapter_ecowitt.adapter import PUSH_STALE_S, EcowittAdapter

    a = EcowittAdapter()
    a._bus = _ReachBus()
    a._last_push["ws2900"] = _t.monotonic() - (PUSH_STALE_S + 1)
    a._reach["ws2900"] = True

    now = _t.monotonic()
    for station, seen in list(a._last_push.items()):
        quiet = now - seen
        if quiet > PUSH_STALE_S:
            await a._reach_verdict(station, False, f"no upload for {int(quiet // 60)} min")

    assert a._bus.verdicts == [("ecowitt:ws2900", False, "no upload for 5 min")]


@pytest.mark.asyncio
async def test_a_station_nobody_has_ever_heard_from_is_not_late():
    """One that has never pushed is unconfigured, not overdue. Watching it would put
    an alert on every installation that simply has no console."""
    from dida_adapter_ecowitt.adapter import EcowittAdapter

    a = EcowittAdapter()
    a._bus = _ReachBus()
    assert a._last_push == {}, "nothing is watched until it has spoken once"


@pytest.mark.asyncio
async def test_the_verdict_is_published_on_the_change_only():
    from dida_adapter_ecowitt.adapter import EcowittAdapter

    a = EcowittAdapter()
    a._bus = _ReachBus()
    await a._reach_verdict("gw2000a", True)
    await a._reach_verdict("gw2000a", True)
    await a._reach_verdict("gw2000a", False, "no answer")
    assert a._bus.verdicts == [
        ("ecowitt:gw2000a", True, ""),
        ("ecowitt:gw2000a", False, "no answer"),
    ], "a repeat says nothing; a change says it once"


def test_the_gateway_fields_worth_taking_are_taken():
    """Measured off the live gateway: it serves twelve fields and this took five.
    Gust, direction and dew point are real measurements that were being dropped;
    feels-like and VPD are arithmetic on values already carried, the daily gust is a
    statistic, and the ten-minute direction duplicates the live one."""
    from dida_adapter_ecowitt.adapter import COMMON_CAPS

    assert COMMON_CAPS["0x0c"] == "wind_gust"
    assert COMMON_CAPS["0x0a"] == "wind_direction"
    assert COMMON_CAPS["0x03"] == "dew_point"
    for derived in ("3", "5", "0x19", "0x6d"):
        assert derived not in COMMON_CAPS, "a value that can be computed is not a measurement"
