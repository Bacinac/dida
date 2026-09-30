"""The adapter-config surface: which secret is used, and which subnets get scanned.

`adapters.py` is 463 statements at 51%. Three of the uncovered helpers are pure and
each one fails in a way the operator would misread.

THE SECRET is the sharpest. Adapter credentials are Fernet-encrypted with the
CROSS-SERVICE `DIDA_SECRET_KEY` — the same value api and every adapter
holding a secret decrypt with. Falling back to `app.state.secret_key` (the
per-instance JWT key) would look like it worked: the save succeeds, the read
succeeds in this process, and every OTHER container fails to decrypt. On a default
install that breaks every adapter secret at once, and nothing says why.

THE SUBNETS carry a fix from a real report. An installation with no VLAN foot and no
manual subnet scanned NOTHING and reported "found none" — which reads as "you have
no devices" rather than "I looked nowhere".
"""

from __future__ import annotations

import json

import pytest
from dida_api import adapters as mod
from dida_api.adapters import _adapter_category, _adapter_secret, _scan_subnets
from dida_core.adapter_config import DISCOVERABLE
from fastapi import HTTPException

# --- the cross-service secret -------------------------------------------------


def test_the_shared_secret_is_used(monkeypatch):
    monkeypatch.setenv("DIDA_SECRET_KEY", "shared-value")
    assert _adapter_secret() == "shared-value"


def test_a_MISSING_secret_fails_loudly_instead_of_falling_back(monkeypatch):
    """The failure mode this prevents is invisible: a per-instance fallback saves and
    reads fine HERE while every other container cannot decrypt a thing."""
    monkeypatch.delenv("DIDA_SECRET_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        _adapter_secret()
    assert e.value.status_code == 500
    assert "DIDA_SECRET_KEY" in str(e.value.detail), "the message must name the variable to set"


def test_a_BLANK_secret_counts_as_missing(monkeypatch):
    """An empty env var is the shape a half-written .env produces; treating it as a
    key would encrypt everything with the empty string."""
    monkeypatch.setenv("DIDA_SECRET_KEY", "   ")
    with pytest.raises(HTTPException):
        _adapter_secret()


# --- how an adapter is offered in the UI --------------------------------------


def test_a_scannable_adapter_is_offered_as_discover():
    adapter = next(iter(DISCOVERABLE))
    assert _adapter_category(adapter) == "discover"


@pytest.mark.parametrize("adapter", ["denon", "heos", "dlna"])
def test_an_ssdp_adapter_appears_by_itself(adapter):
    """These need no configuration at all — offering a "Scan" button for them would
    invite the user to fix something that is not broken."""
    assert _adapter_category(adapter) == "auto"


def test_everything_else_needs_credentials():
    """The default has to be `account`: an undiscoverable secret is the case where
    guessing wrong leaves the user waiting for a scan that can never find anything."""
    assert _adapter_category("spotify") == "account"
    assert _adapter_category("some-adapter-that-does-not-exist") == "account"


# --- which subnets a scan covers ----------------------------------------------


class _Pool:
    def __init__(self, settings: dict) -> None:
        self._settings = settings


class _Request:
    def __init__(self, pool) -> None:
        self.app = type("A", (), {"state": type("S", (), {"pool": pool})()})()


@pytest.fixture
def scan(monkeypatch):
    """Drive `_scan_subnets` with scripted VLAN feet and settings."""
    async def run(*, vlans=(), subnets="", lan_status=None, lan_raw=None):
        async def _vlan_networks(_pool):
            return [(f"vlan{i}", n) for i, n in enumerate(vlans)]

        async def _get_setting(_pool, key):
            if key == "discovery_subnets":
                return subnets
            if key == "lan_status":
                # `lan_raw` passes a string through UNSERIALISED, which is the only
                # way to exercise the undecodable case — a first draft "tested" it by
                # passing None, which is simply the absent case wearing another name.
                if lan_raw is not None:
                    return lan_raw
                return json.dumps(lan_status) if lan_status is not None else None
            return None

        monkeypatch.setattr(mod, "vlan_networks", _vlan_networks)
        monkeypatch.setattr(mod, "get_setting", _get_setting)
        return await _scan_subnets(_Request(_Pool({})))
    return run


async def test_a_vlan_foot_scans_its_own_segment(scan):
    assert await scan(vlans=["192.168.20.0/24"]) == ["192.168.20.0/24"]


async def test_manual_subnets_are_ADDED_not_substituted(scan):
    """The two compose: a VLAN foot scans on-link, manual entries add the routed
    segments DIDA can only reach over L3."""
    got = await scan(vlans=["192.168.20.0/24"], subnets="10.0.5.0/24")
    assert got == ["10.0.5.0/24", "192.168.20.0/24"]


async def test_a_range_too_wide_for_a_house_is_refused_not_cut(scan):
    """A /16 was cut to its first 1024 addresses and the rest silently not scanned."""
    with pytest.raises(HTTPException) as e:
        await scan(vlans=["192.168.20.0/24"], subnets="10.0.0.0/16")
    assert e.value.status_code == 400 and "65792 addresses" in e.value.detail


async def test_DIDAs_own_segment_is_scanned_even_with_no_config(scan):
    """The reported failure: no VLAN foot, no manual subnet — the scan covered
    nothing and said "found none", which reads as "you have no devices" rather than
    "I looked nowhere"."""
    got = await scan(lan_status={"address": "192.168.1.100"})
    assert got == ["192.168.1.0/24"]


async def test_the_same_segment_twice_is_scanned_once(scan):
    """A VLAN foot on the same net as the host must not double the scan."""
    got = await scan(vlans=["192.168.1.0/24"], lan_status={"address": "192.168.1.100"})
    assert got == ["192.168.1.0/24"]


async def test_a_malformed_lan_address_is_skipped_not_raised(scan):
    """lanprobe writes this; a bad value must cost the host's own segment, not the
    whole scan."""
    got = await scan(vlans=["192.168.20.0/24"], lan_status={"address": "not-an-ip"})
    assert got == ["192.168.20.0/24"]


async def test_unparseable_lan_status_is_skipped_too(scan):
    """lanprobe writes this setting; a truncated write must cost the host's own
    segment, never the whole scan."""
    got = await scan(vlans=["192.168.20.0/24"], lan_raw="{broken")
    assert got == ["192.168.20.0/24"]


async def test_blank_entries_in_the_manual_list_are_ignored(scan):
    """A trailing comma in the settings field must not become an empty subnet."""
    got = await scan(subnets="10.0.5.0/24, ,")
    assert got == ["10.0.5.0/24"]


async def test_with_nothing_configured_at_all_the_list_is_empty(scan):
    assert await scan() == []


# --- Zigbee bridge actions: a refusal must not read as success ------------------


from dida_api.adapters import _mqtt_ctl, _raise_on_z2m_error  # noqa: E402


def test_a_successful_bridge_action_passes_through():
    ok = {"status": "ok", "data": {"time": 254}}
    assert _raise_on_z2m_error(ok) is ok


def test_a_z2m_refusal_is_a_400_not_a_silent_ok():
    """z2m answers a refused action with status=error; returning it as-is would let
    the UI show 'done' for a device that never paired/renamed."""
    with pytest.raises(HTTPException) as e:
        _raise_on_z2m_error({"status": "error", "error": "device not found"})
    assert e.value.status_code == 400 and "device not found" in str(e.value.detail)


def test_a_transport_error_field_is_also_a_400():
    with pytest.raises(HTTPException) as e:
        _raise_on_z2m_error({"error": "broker not connected"})
    assert e.value.status_code == 400


class _BoomNC:
    async def request(self, *a, **k):
        raise TimeoutError("no responders")


class _BusReq:
    def __init__(self, nc) -> None:
        self.nc = nc


class _ReqWithBus:
    def __init__(self, nc) -> None:
        self.app = type("A", (), {"state": type("S", (), {"bus": _BusReq(nc)})()})()


async def test_a_silent_mqtt_adapter_is_a_503_not_a_hang():
    """If the MQTT adapter isn't answering its control channel, the endpoint must
    fail loud (503), not surface a confusing 500 or block the request."""
    with pytest.raises(HTTPException) as e:
        await _mqtt_ctl(_ReqWithBus(_BoomNC()), {"action": "permit_join"}, 1)
    assert e.value.status_code == 503
