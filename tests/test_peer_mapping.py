"""Unit test — the peer (second DIDA installation) mirror's id mapping and guards.

Two houses run the same adapters, so the remote's ids collide with ours byte for
byte: `iammeter:meter:power` exists at both. The properties pinned here are the
ones that keep a mirrored house from corrupting this one — ids namespaced per
location, the facet left as the LAST segment (device grouping and the energy
dashboard's per-device dedup both read it), an allowlist that mirrors nothing
until it is set, and mirrored counters that never enter this house's energy total.

Pure — no bus, no HTTP. Runs in the api image via the working-tree path.
"""
import pytest
from dida_adapter_peer.adapter import (
    PeerAdapter,
    _Site,
    _ts_ns,
    device_id,
    local_id,
    parse_sites,
    selected,
    site_key,
)
from dida_api.energy import _suggest_roles
from dida_core import Command, CommandRejected

CABIN = ('[{"name": "Cabin", "api_url": "https://example.org/api/", "username": "peer", '
        '"password": "s3cret", "entities": ["iammeter:*"]}]')


def test_parse_sites_keeps_usable_locations():
    [site] = parse_sites(CABIN)
    assert site["api_url"] == "https://example.org/api", "trailing slash trimmed"
    assert site["entities"] == ["iammeter:*"]


def test_parse_sites_drops_what_cannot_connect():
    """A location without an address or a user can only fail; it is dropped here and
    named on the badge, never carried as a half-live connection."""
    for raw in ('[{"name": "X"}]', '[{"api_url": "https://e/api", "username": "u"}]',
                '[{"name": "X", "api_url": "https://e/api"}]', "not json", '{"name": "X"}', ""):
        assert parse_sites(raw) == [], raw


def test_ids_are_namespaced_per_location():
    """The whole point: the same remote id at two houses must never be one entity."""
    assert local_id("cabin", "iammeter:meter:power", "iammeter:meter") == \
        "peer:cabin_iammeter_meter:power"
    assert local_id("seaside", "iammeter:meter:power", "iammeter:meter") == \
        "peer:seaside_iammeter_meter:power"
    assert local_id("cabin", "iammeter:meter:power") == "peer:cabin_iammeter_meter:power", \
        "device_key is optional — it falls back to the id minus its facet"


def test_facet_stays_the_last_segment():
    """`_dev()` in the energy roles (and the UI's device grouping) is a rsplit on the
    last colon: fold the remote device into the MIDDLE segment or every mirrored
    entity of a house collapses into one 'device'."""
    eid = local_id("cabin", "iammeter:meter:import_energy", "iammeter:meter")
    assert eid.rsplit(":", 1)[-1] == "import_energy"
    assert device_id("cabin", "iammeter:meter:import_energy", "iammeter:meter") == \
        "peer:cabin_iammeter_meter"
    assert device_id("cabin", "iammeter:meter:power", "iammeter:meter") == \
        device_id("cabin", "iammeter:meter:voltage", "iammeter:meter"), "one card per device"


def test_device_level_entity_keeps_a_facet():
    """Not every remote id is `adapter:device:facet` — a BABA camera IS its device."""
    assert local_id("cabin", "baba:8d1f", "baba:8d1f") == "peer:cabin_baba_8d1f:state"


def test_empty_allowlist_mirrors_nothing():
    assert selected("iammeter:meter:power", []) is False
    assert selected("iammeter:meter:power", ["iammeter:*"]) is True
    assert selected("panasonic:ac:power", ["iammeter:*"]) is False
    assert selected("iammeter:meter:power", ["*"]) is True


def test_site_key_survives_a_display_name():
    """The key becomes part of an entity id and a NATS subject, so it is slugged —
    diacritics collapse to `_` rather than transliterating (same rule as BABA's
    locations). What matters is that it is stable and never empty."""
    assert site_key("Cabin") == "cabin"
    assert site_key("Kuća na moru") == "ku_a_na_moru"
    assert site_key("") == "site"


def test_mirrored_value_keeps_the_time_the_peer_measured_it():
    assert _ts_ns("2026-08-03T11:00:00+00:00") == 1785754800_000000000
    assert _ts_ns("nonsense") > 0, "an unparseable stamp falls back to now, never to 0"


class _Status:
    def __init__(self):
        self.state = None
        self.detail = ""

    def ok(self, detail=""):
        self.state, self.detail = "ok", detail

    def idle(self, detail=""):
        self.state, self.detail = "idle", detail

    def error(self, detail=""):
        self.state, self.detail = "error", detail


def _adapter_with_cabin(patterns=("iammeter:*",)):
    a = PeerAdapter()
    a.status = _Status()
    site = _Site({"name": "Cabin", "api_url": "https://example.org/api",
                  "username": "peer", "password": "x", "entities": list(patterns)})
    site.remote_of = {"peer:cabin_iammeter_meter:power": "iammeter:meter:power"}
    site.mirrored, site.available = 1, 55
    a._sites = {site.key: site}
    return a, site


def _cmd(entity_id):
    return Command(entity_id=entity_id, capability="on_off", command="turn_on", ts_ns=1)


async def test_a_command_reaches_the_location_that_owns_the_entity():
    a, site = _adapter_with_cabin()
    sent = []

    async def record(s, rid, c):
        sent.append((s.key, rid, c.command))

    a._post_command = record  # type: ignore[assignment]
    await a.handle_command(_cmd("peer:cabin_iammeter_meter:power"))
    assert sent == [("cabin", "iammeter:meter:power", "turn_on")], "mapped back to the remote id"
    assert site.cmd_error == ""


async def test_a_command_for_an_unmirrored_entity_is_refused_not_forwarded():
    """Only peer commands reach this adapter; one for an entity no site mirrors
    (gone at the far end, or never there) is refused where the journal shows it,
    and nothing leaves this house."""
    a, _ = _adapter_with_cabin()
    sent = []

    async def record(_s, rid, _c):
        sent.append(rid)

    a._post_command = record  # type: ignore[assignment]
    for eid in ("peer:cabin_panasonic_ac:power", "peer:other_x:y"):
        with pytest.raises(CommandRejected, match="not mirrored"):
            await a.handle_command(_cmd(eid))
    assert sent == []


async def test_a_rejected_command_goes_red_instead_of_doing_nothing_quietly():
    """The far end refusing control is a configuration fault. Without a badge the
    only symptom is a control that silently does nothing."""
    a, site = _adapter_with_cabin()

    async def boom(_s, _rid, _c):
        raise RuntimeError("the peer user may not control this device")

    a._post_command = boom  # type: ignore[assignment]
    with pytest.raises(CommandRejected, match="may not control"):
        await a.handle_command(_cmd("peer:cabin_iammeter_meter:power"))
    assert "may not control" in site.cmd_error
    assert a.status.state == "error", "a rejected command must reach the badge"


def test_a_mirrored_meter_is_not_this_house_energy():
    """The guard that matters: the role heuristic reads any `*_energy` leaf on a grid
    meter as import/export, so without this a second building's grid draw would be
    summed into this one's consumption."""
    ids = ["iammeter:meter:import_energy", "iammeter:meter:export_energy",
           "peer:cabin_iammeter_meter:import_energy", "peer:cabin_iammeter_meter:export_energy"]
    roles = _suggest_roles(ids)
    assert roles["iammeter:meter:import_energy"] == "grid_import", "our own meter is unaffected"
    assert roles["iammeter:meter:export_energy"] == "grid_export"
    assert roles["peer:cabin_iammeter_meter:import_energy"] == "ignore"
    assert roles["peer:cabin_iammeter_meter:export_energy"] == "ignore"


async def test_a_mirrored_device_is_announced_as_standing_at_its_location():
    a, site = _adapter_with_cabin()
    announced = []

    class Bus:
        async def publish_entity(self, info):
            announced.append(info)

    a._bus = Bus()
    await a._announce(site, "iammeter:meter:power", {"entity_id": "iammeter:meter:power",
                                                     "device_key": "iammeter:meter",
                                                     "capabilities": ["power"]})
    assert [i.site for i in announced] == ["Cabin"]
