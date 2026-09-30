"""BABA vision-plane mirror — the person-identity mapping (baba.state identities +
identity_sources -> baba:<cam>:person:<gid> = absent|body|face).

Run inside the baba adapter image (has nats + dida_adapter_baba):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-baba:latest \
      -c "python -m pytest tests/test_baba_mapping.py"

The thin-mirror rule means DIDA must not INTERPRET — but two things about the
person map are DIDA's own bookkeeping and are exactly what breaks quietly: a person
who LEFT is detected only by absence from the next snapshot (the map lists only the
present), and persons are inline in state (never in the roster) so the roster-driven
prune must never treat them as stale. These pin both.
"""
import asyncio
import json
import logging

import pytest
from conftest import FakeBroker
from dida_adapter_baba import adapter as baba
from dida_adapter_baba.adapter import SILENCE_S, BabaAdapter, _Site
from dida_core.baba_sites import BabaSite, site_key
from dida_core.health import StatusReporter


class Captured:
    def __init__(self):
        self.states = []   # (entity_id, capability, value, name)
        self.entities = []  # entity_id announced
        self.reach = []  # (device_key, reachable, detail)

    async def publish_state(self, u):
        self.states.append((u.entity_id, u.capability, u.value, u.name))

    async def publish_entity(self, info):
        self.entities.append(info.entity_id)

    async def publish_reachability(self, event):
        self.reach.append((event.device_key, event.reachable, event.detail))


def _adapter():
    a = BabaAdapter()
    a._bus = Captured()
    a.broker = None  # _on_state never asks the api (prune lives in _on_roster)
    a._camera_names["cf97"] = "Patio"
    a._known_cameras.add("cf97")
    site = _Site(BabaSite(**{"name": "Kuća", "nats_url": "nats://198.51.100.11:4222"}))
    site.roster = {"cf97"}
    a._sites[site.key] = site
    return a


class _Msg:
    def __init__(self, text):
        self.data = text.encode()
        self.subject = "baba.place.cf97"


async def _feed(a, snapshot):
    class Msg:
        data = json.dumps(snapshot).encode()
    await a._on_state(a._sites[site_key("Kuća")], Msg())
    for _ in range(4):  # let the spawned publish tasks run
        await asyncio.sleep(0)


def _states(a):
    return a._bus.states


@pytest.mark.asyncio
async def test_parked_vehicle_is_its_own_capability_not_the_label():
    """The vehicle standing in a place rides as `parked_vehicle` STATE on the
    scene entity — never in the display name, which loads once and left every
    already-open wall blind to it. The scene's own state stays present/empty,
    so automations keyed on it never see a car come and go; an emptied place
    publishes "" so the chip clears by state flow, not by page reload."""
    a = _adapter()
    a._scene_names["r1"] = "P1"
    a._scene_places["r1"] = "P1"
    await _feed(a, {**SNAP, "scenes": {"r1": "present"},
                    "parked": {"P1": {"name": "Marko's car", "since": "2026-07-28T14:46:44+00:00"}}})
    labels = [n for e, c, v, n in _states(a) if c == "scene_state"]
    assert labels[-1] == "P1", "the label stays bare — the vehicle is state, not name"
    vehicles = [v for e, c, v, n in _states(a) if c == "parked_vehicle"]
    assert json.loads(vehicles[-1]) == {"name": "Marko's car", "since": "2026-07-28T14:46:44+00:00"}
    # Occupied but not yet identified (the registry holds `unknown` until the
    # plate is read): an empty NAME with a since, never "" — "" made the UI
    # show its memory of the PREVIOUS car over a spot something else took.
    await _feed(a, {**SNAP, "scenes": {"r1": "present"},
                    "parked": {"P1": {"name": "", "since": "2026-07-29T14:41:44+00:00"}}})
    vehicles = [v for e, c, v, n in _states(a) if c == "parked_vehicle"]
    assert json.loads(vehicles[-1]) == {"name": "", "since": "2026-07-29T14:41:44+00:00"}
    # Same scene state, car gone: the chip must clear through state flow.
    await _feed(a, {**SNAP, "scenes": {"r1": "present"}, "parked": {}})
    vehicles = [v for e, c, v, n in _states(a) if c == "parked_vehicle"]
    assert vehicles[-1] == ""


ALEX = "88071ca7-21a0-4e0e-aeeb-e72d623313e0"
BEA = "1111aaaa-0000-0000-0000-000000000000"
SNAP = {"camera_id": "cf97", "camera_name": "Patio"}


@pytest.mark.asyncio
async def test_present_person_mirrors_source_and_name():
    a = _adapter()
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "body"}})
    pid = f"baba:cf97:person:{ALEX}"
    assert (pid, "identity_presence", "body", "Alex") in _states(a), "present by body → 'body', name inline"
    assert pid in a._bus.entities, "the person entity was announced"

    # face confirms → same entity flips to 'face' (the confidence ladder climbs)
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "face"}})
    assert (pid, "identity_presence", "face", "Alex") in _states(a), "face confirm → 'face', same entity"


@pytest.mark.asyncio
async def test_identity_since_rides_alongside_and_clears_on_absence():
    """`identity_since` mirrors the open episode's start next to the presence
    value — and "" both while the sustain gate is still holding the episode
    back AND once the person is gone, so the UI never ages a stale start."""
    a = _adapter()
    pid = f"baba:cf97:person:{ALEX}"
    # present, but no since yet (verdict inside the sustain gate)
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "face"}})
    assert (pid, "identity_since", "", "Alex") in _states(a)
    # the episode earned its row → the start arrives
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "face"},
                    "identity_since": {ALEX: "2026-07-30T08:19:59+00:00"}})
    assert (pid, "identity_since", "2026-07-30T08:19:59+00:00", "Alex") in _states(a)
    # gone → presence absent and the since cleared with it
    await _feed(a, {**SNAP, "identities": {}})
    assert (pid, "identity_presence", "absent", "Alex") in _states(a)
    assert (pid, "identity_since", "", "Alex") in _states(a)


@pytest.mark.asyncio
async def test_a_person_who_leaves_goes_absent():
    a = _adapter()
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "face"}})
    a._bus.states.clear()
    # next snapshot no longer lists Marko — the map lists ONLY the present, so his
    # absence from it is the only signal, and we must mirror it as 'absent'
    await _feed(a, {**SNAP, "identities": {}, "identity_sources": {}})
    pid = f"baba:cf97:person:{ALEX}"
    assert (pid, "identity_presence", "absent", "Alex") in _states(a), "gone from the map → 'absent' (cached name)"


@pytest.mark.asyncio
async def test_absent_is_per_camera_and_leaves_others_present():
    a = _adapter()
    a._camera_names["gate"] = "Gate"
    a._known_cameras.add("gate")
    # Alex at Patio, Bea at Gate
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {ALEX: "face"}})
    await _feed(a, {"camera_id": "gate", "identities": {BEA: "Bea"}, "identity_sources": {BEA: "body"}})
    a._bus.states.clear()
    # Patio empties — must NOT touch Bea's entity on the Gate camera
    await _feed(a, {**SNAP, "identities": {}})
    absents = [s for s in _states(a) if s[2] == "absent"]
    assert absents == [(f"baba:cf97:person:{ALEX}", "identity_presence", "absent", "Alex")], \
        "only the Patio person went absent; the Gate person is untouched"


@pytest.mark.asyncio
async def test_missing_source_falls_back_to_body_not_face():
    # A present person with no valid source must mirror at the WEAKER rung — never
    # invent 'face' (which would let a hard action fire on an unconfirmed sighting).
    a = _adapter()
    await _feed(a, {**SNAP, "identities": {ALEX: "Alex"}, "identity_sources": {}})
    pid = f"baba:cf97:person:{ALEX}"
    assert (pid, "identity_presence", "body", "Alex") in _states(a), "present, no source → 'body', never 'face'"


def _catalog(device_keys, subs=(), placed=()):
    """What the api answers the prune from: this adapter's entities, and which of
    them somebody put on the floor plan or in a room — the api keeps those rows when
    asked to (`keep_placed`), and says how many it kept."""
    rows = [{"entity_id": dk, "device_key": dk} for dk in device_keys]
    rows += [{"entity_id": e, "device_key": d} for e, d in subs]

    def forget(entity_ids=(), device_keys=(), keep_placed=False):
        hit = [r["entity_id"] for r in rows if r["entity_id"] in entity_ids or r["device_key"] in device_keys]
        return {"kept": sum(1 for e in hit if keep_placed and e in placed)}

    return FakeBroker(entities=lambda **_: list(rows), forget=forget)


def _forgotten(broker):
    """(cameras, sub-entities) the prune let go of — and every ask spares placements."""
    asks = broker.asked("forget")
    assert all(a.get("keep_placed") for a in asks), "the prune never spends a placement"
    return ([dk for a in asks for dk in a.get("device_keys", ())],
            [e for a in asks for e in a.get("entity_ids", ())])


def _site(a, name, roster):
    from dida_adapter_baba.adapter import _Site

    s = _Site(BabaSite(**{"name": name, "nats_url": f"nats://{name}:4222", "go2rtc": "",
               "go2rtc_user": "", "go2rtc_password": "", "api_url": "", "peer_key": ""}))
    s.roster = set(roster)
    a._sites[s.key] = s
    return s


@pytest.mark.asyncio
async def test_prune_waits_until_every_location_has_reported():
    """The disaster case, in one test. Two installs; only the house has answered.
    Its roster does not list the holiday home's cameras — pruning on it would
    delete them together with the room and floor-plan placement DIDA holds and
    BABA cannot give back (that is what cost a restore once). So: no roster from
    a location, no prune at all."""
    a = BabaAdapter()
    a.broker = _catalog(["baba:home-cam", "baba:far-cam"])
    _site(a, "home", ["home-cam"])
    _site(a, "far", [])            # not reported yet
    await a._prune_stale()
    assert a.broker.asked("forget") == [], "an unreported location blocks pruning entirely"


class _Status:
    """Captures what the adapter would put on its card."""

    def __init__(self):
        self.state = ""
        self.detail = ""

    def ok(self, detail=""):
        self.state, self.detail = "ok", detail

    def error(self, detail=""):
        self.state, self.detail = "error", detail

    def idle(self, detail=""):
        self.state, self.detail = "idle", detail


@pytest.mark.asyncio
async def test_a_location_without_a_nats_address_is_named_not_ignored():
    """An entry added from a scan that could not find the peer port has no NATS
    address, so it can never connect. It used to be dropped in silence, which reads
    as "it did not save" — and the same location gets typed in again."""
    a = BabaAdapter()
    a.status = _Status()
    a._cfg = _Cfg(["far"])
    a._cfg._raw = json.dumps([{"name": "Cabin", "api_url": "http://198.51.100.9:8080"}])
    a._ok_status()
    assert a.status.state == "error"
    assert "Cabin" in a.status.detail and "NATS" in a.status.detail


@pytest.mark.asyncio
async def test_a_location_whose_media_plane_is_down_says_so_on_the_card():
    """The state plane and the media plane fail independently. With go2rtc rejecting
    every request the cameras get no descriptor and the wall stays empty — but the
    roster still arrives, so the badge used to read "ok · Cabin: 2 cams" while nothing
    could be watched. A count is not health."""
    a = BabaAdapter()
    a.status = _Status()
    site = _site(a, "Cabin", ["cam-1"])
    a._known_cameras.add("cam-1")
    a._ok_status()
    assert a.status.state == "ok", "a healthy location reports its camera count"

    site.media_error = "go2rtc rejected our credentials (401)"
    a._ok_status()
    assert a.status.state == "error"
    assert "Cabin" in a.status.detail and "401" in a.status.detail


class _Cfg:
    """Stands in for AdapterConfig: only `sites` matters to the prune guard."""

    def __init__(self, names):
        self._raw = json.dumps([{"name": n, "nats_url": f"nats://{n}:4222"} for n in names])

    def get(self, key, default=""):
        return self._raw if key == "sites" else default


@pytest.mark.asyncio
async def test_prune_waits_for_a_location_that_has_not_connected_yet():
    """The one the earlier guard missed, and it cost a real wipe: it asked whether
    every CONNECTED location had a roster, which the first one to connect satisfies
    on its own. On each restart the house connected a beat before the holiday home,
    its roster read as the whole inventory, and the other install's cameras were
    deleted — then re-announced as new entities with no room, which is why the
    "assign an area" nudge kept coming back after every deploy."""
    a = BabaAdapter()
    a.broker = _catalog(["baba:home-cam", "baba:far-cam"])
    a._cfg = _Cfg(["home", "far"])
    _site(a, "home", ["home-cam"])   # connected and reported; `far` has not connected
    await a._prune_stale()
    assert a.broker.asked("forget") == [], "a configured location that has not connected blocks pruning"


@pytest.mark.asyncio
async def test_prune_spans_every_location_once_all_reported():
    """With both inventories in hand the live set is their UNION: each keeps its
    own cameras, and only what NEITHER lists is dropped."""
    a = BabaAdapter()
    a.broker = _catalog(["baba:home-cam", "baba:far-cam", "baba:deleted-cam"])
    _site(a, "home", ["home-cam"])
    _site(a, "far", ["far-cam"])
    await a._prune_stale()
    assert _forgotten(a.broker)[0] == ["baba:deleted-cam"], \
        "neither location claims it → stale; each other camera is claimed by one of them"


def test_media_credentials_follow_who_answers():
    """A tunnelled install serves go2rtc THROUGH BABA's web server on one
    hostname, and that proxy authenticates callers with the peer key — offering
    it go2rtc's basic auth gets a 401 and an empty camera wall. Separate origins
    mean go2rtc itself is answering, and the peer key must not go to that port."""
    from dida_adapter_baba.adapter import _Site

    tunnel = _Site(BabaSite(**{"name": "Cabin", "nats_url": "wss://cabin.example",
                    "go2rtc": "https://cabin.example/go2rtc", "go2rtc_user": "",
                    "go2rtc_password": "", "api_url": "https://cabin.example/api",
                    "peer_key": "FARPEER"}))
    assert tunnel.cfg.media_headers() == {"X-Peer-Key": "FARPEER"}

    lan = _Site(BabaSite(**{"name": "Kuća", "nats_url": "nats://198.51.100.11:4222",
                 "go2rtc": "http://198.51.100.11:11984", "go2rtc_user": "baba",
                 "go2rtc_password": "G2PW", "api_url": "http://198.51.100.11:8080",
                 "peer_key": "PEER"}))
    from base64 import b64decode
    lan_headers = lan.cfg.media_headers()
    assert "X-Peer-Key" not in lan_headers, "the peer key has no business on a bare go2rtc port"
    assert b64decode(lan_headers["Authorization"].split(" ", 1)[1]).decode() == "baba:G2PW"


def test_camera_slug_maps_are_per_location():
    """go2rtc slugs are unique only within one box: two installs both have a
    'patio'. The slug→UUID map is therefore per location, or one install's media
    plane would resolve to the other's camera."""
    a = BabaAdapter()
    home = _site(a, "home", [])
    far = _site(a, "far", [])
    home.camera_ids["patio"] = "uuid-home"
    far.camera_ids["patio"] = "uuid-far"
    assert home.camera_ids["patio"] != far.camera_ids["patio"]


class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Stands in for the aiohttp session: every GET gets one status; keeps what was asked."""

    def __init__(self, status):
        self.status, self.asked = status, []

    def get(self, url, timeout=None, headers=None):
        self.asked.append((url, headers))
        return _Resp(self.status)


ROSTER = {"cameras": [
    {"id": "u-gate", "slug": "gate", "name": "Gate"},
    {"id": "u-yard", "slug": "yard", "name": "Yard"},
    {"id": "u-old", "slug": "old", "name": "Old", "enabled": False},
]}


def _cabin(a, status):
    a.status = _Status()
    a._session = _Session(status)
    site = _Site(BabaSite(name="Cabin", nats_url="nats://cabin:4222", peer_key="pk",
                          go2rtc="https://cabin.example/go2rtc", api_url="https://cabin.example/api"))
    a._sites[site.key] = site
    a._handle_roster(site, ROSTER)
    return site


@pytest.mark.asyncio
async def test_the_wall_comes_from_the_roster_not_from_go2rtcs_stream_list():
    """go2rtc's /api/streams carries every camera's RTSP credentials, so BABA refuses
    it to a peer, and Cabin's wall went dark the evening that shipped. The roster
    already names every camera; go2rtc is only asked whether it takes our key, on a
    media path that opens no camera. A switched-off camera is not in go2rtc at all."""
    a = _adapter()
    site = _cabin(a, 404)
    await a._announce_cameras(site)
    for _ in range(4):
        await asyncio.sleep(0)
    assert a._session.asked == [("https://cabin.example/go2rtc/api/stream.mp4", {"X-Peer-Key": "pk"})]
    tiles = {e: json.loads(v) for e, c, v, n in _states(a) if c == "camera"}
    assert set(tiles) == {"baba:u-gate", "baba:u-yard"}
    assert tiles["baba:u-gate"]["stream"] == "gate" and tiles["baba:u-gate"]["site"] == "Cabin"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "said"), [(401, "peer key for Cabin"), (403, "peer key for Cabin"),
                                              (502, "answered 502")])
async def test_a_media_plane_that_will_not_take_us_is_an_error_not_a_dark_wall(status, said):
    a = _adapter()
    site = _cabin(a, status)
    with pytest.raises(RuntimeError, match=said):
        await a._announce_cameras(site)
    assert not [e for e, c, v, n in _states(a) if c == "camera"]


@pytest.mark.asyncio
async def test_a_camera_switched_off_in_baba_leaves_the_media_plane():
    a = _adapter()
    site = _cabin(a, 404)
    assert site.camera_ids == {"gate": "u-gate", "yard": "u-yard"}
    off = {"cameras": [{**c, "enabled": c["id"] != "u-yard"} for c in ROSTER["cameras"]]}
    a._handle_roster(site, off)
    assert site.camera_ids == {"gate": "u-gate", "old": "u-old"}


def test_inline_sub_entities_are_excluded_from_the_roster_prune():
    """People and pets/vehicles are inline in state, never in the roster's
    live_subs — so the roster-driven level-2 prune (which deletes any sub-entity
    not in live_subs) must SKIP them, or every roster refresh deletes every one.

    `person` was skipped by name and `object` was added later WITHOUT being added
    to the skip, so pets and vehicles were dropped on each refresh and silently
    re-announced on the next sighting. That is why the kinds are a list now, and
    why this test asserts on the list rather than on one hardcoded marker."""
    from dida_adapter_baba.adapter import _INLINE_KINDS

    def skipped(eid):
        return any(f":{k}:" in eid for k in _INLINE_KINDS)

    assert skipped(f"baba:cf97:person:{ALEX}"), "a person is inline → never roster-stale"
    assert skipped("baba:cf97:object:24e24874"), "a pet/vehicle is inline too"
    assert not skipped("baba:cf97:zone:462988b2"), "a zone IS roster catalog — stays prunable"
    assert not skipped("baba:cf97:scene:462988b2"), "so is a scene region"


@pytest.mark.asyncio
async def test_the_vehicle_is_found_by_place_not_by_what_the_region_is_called():
    """`scenes` is keyed by region uuid, `parked` by place, and the roster's
    `place` is what joins them. Matching the DISPLAY NAME held only while every
    parking region happened to be called P1..P4 — rename one to something a
    human reads and the car silently stops appearing. Two regions (one camera
    each) watching the one place must both show the same vehicle."""
    a = _adapter()
    a._scene_names["r1"] = "Nadstrešnica lijevo"
    a._scene_places["r1"] = "P4"
    a._scene_names["r2"] = "Nadstrešnica lijevo (shed)"
    a._scene_places["r2"] = "P4"
    await _feed(a, {**SNAP, "scenes": {"r1": "present", "r2": "present"},
                    "parked": {"P4": {"name": "Goran", "since": "2026-08-28T08:37:20+00:00"}}})
    vehicles = [v for e, c, v, n in _states(a) if c == "parked_vehicle"]
    assert len(vehicles) == 2 and all(
        json.loads(v)["name"] == "Goran" for v in vehicles
    ), "both views of one place carry the same car"


@pytest.mark.asyncio
async def test_a_region_that_watches_no_place_never_carries_a_vehicle():
    """A gate has a state and no place. Before the roster said so, `parked`
    was probed with the gate's own name — harmless only because nothing is
    ever parked in a key called "Gate"."""
    a = _adapter()
    a._scene_names["g1"] = "Gate"
    a._scene_places.pop("g1", None)
    await _feed(a, {**SNAP, "scenes": {"g1": "open"},
                    "parked": {"Gate": {"name": "nonsense", "since": "2026-08-28T08:00:00+00:00"}}})
    vehicles = [v for e, c, v, n in _states(a) if c == "parked_vehicle"]
    assert vehicles[-1] == ""


@pytest.mark.asyncio
async def test_an_arrival_lands_on_the_place_it_happened_at(monkeypatch):
    """The scene entity already shows WHO stands there; this is the record that
    they came and went, which a live state cannot be. It is filed against the
    region that watches the place, so the row sits on the view whose footage
    covers the minute it carries."""
    a = _adapter()
    a._scene_names["r1"] = "Nadstrešnica lijevo"
    a._scene_places["r1"] = "P4"
    a._scene_cams["r1"] = "cf97"
    sent = []

    async def _emit(_bus, kind, **kw):
        sent.append((kind, kw))

    monkeypatch.setattr("dida_adapter_baba.adapter.emit_journal", _emit)
    await a._on_place(None, _Msg(json.dumps({
        "camera_id": "cf97", "kind": "vehicle_left", "place": "P4",
        "name": "Goran", "stood_s": 53400,
        "started_at": "2026-08-28T06:44:04+00:00",
        "ended_at": "2026-08-28T06:45:04+00:00"})))
    assert len(sent) == 1
    kind, kw = sent[0]
    assert kind == "vehicle_left"
    assert kw["entity_id"].endswith(":scene:r1"), "filed on the region that watches P4"
    assert "Goran" in kw["message"] and "14 h 50 min" in kw["message"]
    assert kw["data"]["started_at"].endswith("06:44:04+00:00"), "the clip window rides along"


@pytest.mark.asyncio
async def test_an_arrival_for_a_place_nothing_here_watches_is_dropped(monkeypatch):
    """A second BABA installation's places arrive on the same wildcard."""
    a = _adapter()
    sent = []

    async def _emit(_bus, kind, **kw):
        sent.append(kind)

    monkeypatch.setattr("dida_adapter_baba.adapter.emit_journal", _emit)
    await a._on_place(None, _Msg(json.dumps({
        "camera_id": "cf97", "kind": "vehicle_arrived", "place": "P9", "name": ""})))
    assert sent == []


@pytest.mark.asyncio
async def test_a_place_name_is_resolved_on_the_camera_that_reported_it(monkeypatch):
    """Place names are the operator's labels and repeat: the house and the
    holiday home both have a P1. Matched on the name alone, an arrival at one
    was filed on whichever P1 region happened to come first — the other house's
    timeline."""
    a = _adapter()
    for rid, cid in (("home-p1", "cf97"), ("cabin-p1", "b0b0")):
        a._scene_names[rid] = "P1"
        a._scene_places[rid] = "P1"
        a._scene_cams[rid] = cid
    sent = []

    async def _emit(_bus, kind, **kw):
        sent.append(kw["entity_id"])

    monkeypatch.setattr("dida_adapter_baba.adapter.emit_journal", _emit)
    await a._on_place(None, _Msg(json.dumps({
        "camera_id": "b0b0", "kind": "vehicle_arrived", "place": "P1", "name": "Goran"})))
    assert len(sent) == 1 and sent[0].endswith(":scene:cabin-p1")
    await a._on_place(None, _Msg(json.dumps({
        "camera_id": "f00d", "kind": "vehicle_arrived", "place": "P1", "name": "Goran"})))
    assert len(sent) == 1, "a camera with no P1 region of its own files nothing"


@pytest.mark.asyncio
async def test_a_disabled_zone_reads_empty():
    """BABA stops evaluating a zone the operator switched off, so the last value it
    sent — often "occupied" — would stand for good, and every rule on it with it.
    The roster says which zones are off; those read empty whatever a snapshot
    still carries."""
    a = _adapter()
    zone = "baba:cf97:zone:z1"
    roster = {"cameras": [{"id": "cf97", "name": "Patio",
                           "zones": [{"id": "z1", "name": "Terasa", "enabled": False}]}]}
    a._handle_roster(a._sites[site_key("Kuća")], roster)
    await _feed(a, {**SNAP, "zones": {"z1": True}})
    occupancy = [v for e, c, v, n in _states(a) if e == zone and c == "occupancy"]
    assert occupancy and not any(occupancy)

    roster["cameras"][0]["zones"][0]["enabled"] = True
    a._handle_roster(a._sites[site_key("Kuća")], roster)
    await _feed(a, {**SNAP, "zones": {"z1": True}})
    assert [v for e, c, v, n in _states(a) if e == zone and c == "occupancy"][-1] is True


@pytest.mark.asyncio
async def test_a_placed_sub_entity_survives_the_prune():
    """West P3 was switched off in BABA pending a decision about its geometry. The
    roster stopped listing it, the prune deleted the entity, and the floor-plan spot
    went with it — so when the region came back a week later it had no place on the
    plan and read as something that had never existed. A placement is the operator's
    work and BABA cannot give it back, so the prune lets go of the VALUE and keeps
    the row; deleting for real stays the explicit Remove in the UI."""
    a = BabaAdapter()
    a.broker = _catalog(
        ["baba:west"],
        subs=[("baba:west:scene:p3", "baba:west"), ("baba:west:scene:p9", "baba:west")],
        placed=["baba:west:scene:p3"],
    )
    a._known_scenes |= {"baba:west:scene:p3", "baba:west:scene:p9"}
    _site(a, "home", ["west"])       # camera still live; neither scene in live_subs
    await a._prune_stale()
    cameras, subs = _forgotten(a.broker)
    assert cameras == [], "the camera itself is still in the roster"
    assert subs == ["baba:west:scene:p3", "baba:west:scene:p9"], \
        "both lose their value; the api keeps the placed row (keep_placed)"
    assert not a._known_scenes & {"baba:west:scene:p3", "baba:west:scene:p9"}, \
        "both are forgotten, so a region switched back on re-announces onto its kept row"


@pytest.mark.asyncio
async def test_a_placed_entity_survives_its_camera_being_pruned():
    """Same rule one level up: a camera can vanish from the roster while the room
    and floor-plan spots its entities were given are still worth more than the rows
    are worth deleting."""
    a = BabaAdapter()
    a.broker = _catalog(["baba:gone-cam", "baba:live-cam"],
                        subs=[("baba:gone-cam:scene:p1", "baba:gone-cam")],
                        placed=["baba:gone-cam:scene:p1"])
    _site(a, "home", ["live-cam"])   # a real roster that simply no longer lists gone-cam
    await a._prune_stale()
    assert _forgotten(a.broker) == (["baba:gone-cam"], []), \
        "the stale camera is swept, sparing placements; its scene goes with it, not twice"


@pytest.mark.asyncio
async def test_an_unplaced_sub_entity_is_still_pruned():
    """The guard is placement, not immortality: what nobody put anywhere is still
    cleaned up, or a renamed region would leave its old key behind forever."""
    a = BabaAdapter()
    a.broker = _catalog(["baba:west"], subs=[("baba:west:scene:old", "baba:west")], placed=[])
    _site(a, "home", ["west"])
    await a._prune_stale()
    assert _forgotten(a.broker)[1] == ["baba:west:scene:old"]


def test_the_descriptor_offers_the_cameras_own_stream_as_mp4():
    """go2rtc has no encoder behind its MJPEG route, so the full-resolution live
    view is the camera's own H.264/H.265 as fragmented MP4, on the media plane."""
    from dida_adapter_baba.adapter import _descriptor

    desc = json.loads(_descriptor("http://g:1984", "gate", "http://b:8080", "cam-uuid", "Kuća"))
    assert desc["mp4"] == "http://g:1984/api/stream.mp4?src=gate"
    assert desc["mp4"].startswith(desc["snapshot"].partition("/api/frame.jpeg")[0])


def test_the_descriptor_offers_the_detector_ring_as_a_still():
    from dida_adapter_baba.adapter import _descriptor

    desc = json.loads(_descriptor("http://g:1984", "gate", "http://b:8080", "cam-uuid", "Kuća"))
    assert desc["still"] == "http://b:8080/cameras/cam-uuid/live.jpg?boxes=false"
    assert "still" not in json.loads(_descriptor("http://g:1984", "gate"))


# --- a location that went quiet --------------------------------------------------

@pytest.mark.asyncio
async def test_the_first_snapshot_says_the_camera_is_there_once():
    a = _adapter()
    await _feed(a, {"camera_id": "cf97", "motion": False})
    await _feed(a, {"camera_id": "cf97", "motion": True})
    assert a._bus.reach == [("baba:cf97", True, "")]


@pytest.mark.asyncio
async def test_a_silent_location_takes_its_cameras_down_until_it_speaks(monkeypatch):
    """BABA re-publishes every snapshot each minute. Past SILENCE_S of nothing, the
    last snapshot is nobody's truth: an occupied zone must not stay occupied on it."""
    import dida_adapter_baba.adapter as mod

    a = _adapter()
    await _feed(a, {"camera_id": "cf97", "motion": False})
    a._sites[site_key("Kuća")].heard -= SILENCE_S + 1
    ticks = iter([None, None])

    async def two_ticks(_s):
        if next(ticks, "stop") == "stop":
            raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", two_ticks)
    with pytest.raises(asyncio.CancelledError):
        await a._silence_loop()
    monkeypatch.undo()
    assert [r[:2] for r in a._bus.reach] == [("baba:cf97", True), ("baba:cf97", False)], \
        "down once, not said again on every tick"
    assert "no state from BABA" in a._bus.reach[-1][2]

    await _feed(a, {"camera_id": "cf97", "motion": False})
    assert a._bus.reach[-1] == ("baba:cf97", True, "")


@pytest.mark.asyncio
async def test_a_reconnect_asks_for_the_roster_again(monkeypatch):
    import dida_adapter_baba.adapter as mod

    asked: list[str] = []
    callbacks: dict = {}

    class NC:
        is_closed = False

        async def subscribe(self, *_a, **_k):
            pass

        async def request(self, subject, *_a, **_k):
            asked.append(subject)
            raise mod.nats.errors.NoRespondersError

    async def connect(_url, **kw):
        callbacks.update(kw)
        return NC()

    monkeypatch.setattr(mod.nats, "connect", connect)
    a = BabaAdapter()
    a._bus = Captured()
    a.status = StatusReporter("baba")
    await a._connect_site(_Site(BabaSite(**{"name": "Kuća", "nats_url": "nats://198.51.100.11:4222"})))
    await callbacks["reconnected_cb"]()
    assert asked == [mod.ROSTER_REQUEST, mod.ROSTER_REQUEST]


@pytest.mark.asyncio
async def test_replies_come_back_under_the_mirrors_own_inbox(monkeypatch):
    """BABA lets the mirror account subscribe to `_INBOX.dida.>` and no wider, so a
    reply addressed to the shared `_INBOX` would never reach the roster request."""
    import dida_adapter_baba.adapter as mod

    seen: dict = {}

    class NC:
        is_closed = False

        async def subscribe(self, *_a, **_k):
            pass

        async def request(self, *_a, **_k):
            raise mod.nats.errors.NoRespondersError

    async def connect(_url, **kw):
        seen.update(kw)
        return NC()

    monkeypatch.setattr(mod.nats, "connect", connect)
    a = BabaAdapter()
    a._bus = Captured()
    a.status = StatusReporter("baba")
    await a._connect_site(_Site(BabaSite(**{"name": "Kuća", "nats_url": "nats://198.51.100.11:4222"})))
    assert seen["inbox_prefix"] == "_INBOX.dida"


@pytest.mark.asyncio
async def test_a_dropped_message_is_a_warning_once_a_minute(caplog, monkeypatch):
    """At debug level a BABA that sent nothing usable looked exactly like a quiet one;
    one line per snapshot would bury everything else."""
    a = _adapter()
    site = a._sites[site_key("Kuća")]

    class Garbled:
        data = b"{not json"
        subject = "baba.state.cf97"

    class Anonymous:
        data = json.dumps({"motion": True}).encode()
        subject = "baba.state.cf97"

    clock = [1000.0]
    monkeypatch.setattr(baba.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="dida.adapter.baba"):
        for _ in range(5):
            await a._on_state(site, Garbled())
        await a._on_state(site, Anonymous())
        clock[0] += baba._DROP_COMPLAIN_S
        await a._on_state(site, Anonymous())
    assert [r.getMessage() for r in caplog.records] == [
        "baba: undecodable state on baba.state.cf97",
        "baba: state on baba.state.cf97 names no camera",
    ]


class _Answer:
    def __init__(self, body):
        self.status, self._body = 200, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _Web:
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, timeout=None):
        return _Answer(self.answers[url.split("/")[2].split(":")[0]])


@pytest.mark.asyncio
async def test_a_baba_is_found_by_what_it_says_it_is(monkeypatch):
    """A web page's title and an open port were guesses: DIDA's own api answers
    /version too. Only an install that names itself BABA is one."""
    a = BabaAdapter()
    a._session = _Web({
        "198.51.100.11": {"product": "baba", "version": "0.1.560"},
        "198.51.100.12": {"product": "dida", "version": "0.1.900"},
        "198.51.100.13": {"version": "0.1.557"},
    })

    async def port_open(ip, port, timeout_s=0.6):
        return port == BabaAdapter.API_PORT

    monkeypatch.setattr(a, "_port_open", port_open)
    assert await a._identify("198.51.100.11") == {"ip": "198.51.100.11", "version": "0.1.560", "nats_url": ""}
    assert await a._identify("198.51.100.12") is None
    assert await a._identify("198.51.100.13") is None
