"""Regression tests for the Dreame MIoT <-> canonical mapping and the cloud client's
request shaping.

Run inside the dreame adapter image (dida_adapter_dreame installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-dreame:latest \
      -c "python -m pytest tests/test_dreame_mapping.py"

Covers status_caps (MIoT result list → capability dict: state vocabulary, partial
polls, fault extraction), the suction vocabulary round-trip, and the parts of the
cloud client that decide WHERE a command lands — the per-device endpoint shard and
the label re-attachment a property read depends on.
"""
import pytest
from dida_adapter_dreame.cloud import DreameCloud, DreameCloudError
from dida_adapter_dreame.mapping import (
    PROPS,
    SUCTION_OPTIONS,
    status_caps,
    suction_id,
)


def _r(did, value, code=0):
    return {"did": did, "siid": 1, "piid": 1, "code": code, "value": value}


# --- status_caps ---------------------------------------------------------------

def test_status_caps_full_poll():
    caps, fault = status_caps([
        _r("state", 1), _r("error_code", 0), _r("battery_level", 82),
        _r("charging_status", 2), _r("suction_level", 1),
    ])
    assert caps == {"vacuum": "cleaning", "battery": 82.0, "enum": "basic"}
    assert fault is None, "no fault while the state is not 'error'"


def test_status_caps_state_vocabulary():
    for raw, canonical in [(1, "cleaning"), (2, "idle"), (3, "paused"), (4, "error"),
                           (5, "returning"), (6, "charging"), (7, "cleaning"),
                           (8, "docked"), (9, "docked"), (10, "returning"),
                           (12, "cleaning"), (13, "docked"), (17, "returning")]:
        caps, _ = status_caps([_r("state", raw)])
        assert caps["vacuum"] == canonical, f"state {raw} → {canonical}"
    caps, _ = status_caps([_r("state", 999)])
    assert caps["vacuum"] == "idle", "an unknown state id degrades to idle, not a crash"


def test_station_chores_read_as_docked():
    # Washing and drying happen ON the dock. Reading them as anything else would
    # put a robot that is standing in its base on the floorplan as if it were out.
    for raw in (8, 9, 16, 19):
        caps, _ = status_caps([_r("state", raw)])
        assert caps["vacuum"] == "docked"


def test_status_caps_fault_only_in_error_state():
    caps, fault = status_caps([_r("state", 4), _r("error_code", 128)])
    assert caps["vacuum"] == "error" and fault == "dock error"
    _, fault = status_caps([_r("state", 4), _r("error_code", 9999)])
    assert "9999" in fault, "an uncatalogued code still names itself"
    _, fault = status_caps([_r("state", 6), _r("error_code", 128)])
    assert fault is None, "a lingering code without the error state is not a fault"


def test_status_caps_partial_poll():
    caps, _ = status_caps([_r("state", 6), _r("battery_level", None, code=-4004)])
    assert caps == {"vacuum": "charging"}, "an errored property is skipped, the rest still map"
    assert status_caps([])[0] == {}, "an empty result maps to nothing (nothing is published)"


def test_battery_rejects_bools():
    # A bool is an int in Python; publishing True as 1 % battery would be a lie.
    caps, _ = status_caps([_r("battery_level", True)])
    assert "battery" not in caps


def test_suction_vocabulary_round_trip():
    assert SUCTION_OPTIONS == ["silent", "basic", "strong", "max"]
    for option in SUCTION_OPTIONS:
        level = suction_id(option)
        assert level is not None
        caps, _ = status_caps([_r("suction_level", level)])
        assert caps["enum"] == option
    assert suction_id("turbo") is None, "an unknown option is refused, not guessed"


# --- cloud client --------------------------------------------------------------

def test_bind_shards_the_command_endpoint():
    cloud = DreameCloud("a@b.c", "x", "eu")
    cloud.bind({"did": "-119609766", "bindDomain": "10000.mt.eu.iot.dreame.tech:19973"})
    assert cloud.device_id == "-119609766"
    assert cloud._suffix == "-10000", "commands must go to the device's own shard"


def test_bind_without_domain_uses_the_plain_endpoint():
    cloud = DreameCloud("a@b.c", "x", "eu")
    cloud.bind({"did": "42"})
    assert cloud._suffix == ""


def test_bind_refuses_a_record_without_a_did():
    with pytest.raises(DreameCloudError):
        DreameCloud("a@b.c", "x", "eu").bind({"bindDomain": "10000.mt.eu.iot.dreame.tech"})


def test_unknown_region_falls_back_to_eu():
    assert "eu.iot.dreame.tech" in DreameCloud("a@b.c", "x", "hr")._url("/x")


def test_get_properties_reattaches_labels(monkeypatch):
    # The reply carries only siid/piid; the mapping keys off our labels, so a
    # client that dropped them would silently map nothing.
    cloud = DreameCloud("a@b.c", "x", "eu")
    cloud.bind({"did": "7"})
    sent = {}

    def fake_send(method, params):
        sent["method"], sent["params"] = method, params
        return [{"siid": p["siid"], "piid": p["piid"], "code": 0, "value": 1} for p in params]

    monkeypatch.setattr(cloud, "_send", fake_send)
    out = cloud.get_properties(PROPS)
    assert sent["method"] == "get_properties"
    assert [p["did"] for p in sent["params"]] == ["7"] * len(PROPS), "the wire wants the device id"
    assert [r["did"] for r in out] == [p["did"] for p in PROPS], "our labels come back"


def test_get_properties_fails_loud_without_a_result(monkeypatch):
    cloud = DreameCloud("a@b.c", "x", "eu")
    cloud.bind({"did": "7"})
    monkeypatch.setattr(cloud, "_send", lambda *a: None)
    with pytest.raises(DreameCloudError):
        cloud.get_properties(PROPS)


def test_commands_refuse_to_go_out_unbound():
    cloud = DreameCloud("a@b.c", "x", "eu")
    with pytest.raises(DreameCloudError):
        cloud.action(2, 1)


# --- the extras: consumables, station, job ------------------------------------

def test_every_extra_is_polled_and_uniquely_named():
    from dida_adapter_dreame.mapping import EXTRA_PROPS, EXTRAS
    assert len(EXTRA_PROPS) == len(EXTRAS)
    assert len({e.entity for e in EXTRAS}) == len(EXTRAS), "two extras would share an entity id"
    assert len({(e.siid, e.piid) for e in EXTRAS}) == len(EXTRAS), "the same property read twice"
    for extra in EXTRAS:
        assert bool(extra.values) == (extra.capability in ("enum", "text")), \
            f"{extra.entity}: a worded reading needs a vocabulary, a number must not have one"
        assert extra.capability != "enum" or extra.writable or extra.entity in ("dust_bag",
            "clean_water", "dirty_water", "detergent"), \
            f"{extra.entity}: a read-only reading must not be published as a settable enum"


def test_extra_caps_maps_wear_parts_and_station():
    from dida_adapter_dreame.mapping import extra_caps
    out = extra_caps([
        {"did": "main_brush_left", "code": 0, "value": 100},
        {"did": "filter_left", "code": 0, "value": 63},
        {"did": "dust_bag_status", "code": 0, "value": 0},
        {"did": "clean_water_tank_status", "code": 0, "value": 2},
        {"did": "self_wash_base_status", "code": 0, "value": 1},
    ])
    assert out == {"main_brush": 100.0, "filter": 63.0, "dust_bag": "ok",
                   "clean_water": "low", "station": "washing"}


def test_extra_caps_skips_what_the_robot_did_not_answer():
    from dida_adapter_dreame.mapping import extra_caps
    assert extra_caps([{"did": "filter_left", "code": -4004, "value": None}]) == {}
    assert extra_caps([{"did": "dust_bag_status", "code": 0, "value": 77}]) == {}, \
        "a raw value outside the vocabulary is dropped, not published as a number"
    assert extra_caps([{"did": "not_ours", "code": 0, "value": 1}]) == {}


def test_cleaning_time_is_published_in_seconds():
    # The robot counts minutes; the `duration` capability is seconds, and a
    # 40-minute run shown as 40 seconds would make every history chart wrong.
    from dida_adapter_dreame.mapping import extra_caps
    assert extra_caps([{"did": "cleaning_time", "code": 0, "value": 40}]) == {"cleaning_time": 2400.0}


def test_station_vocabulary_has_no_duplicates():
    from dida_adapter_dreame.mapping import EXTRAS, extra_options
    station = next(e for e in EXTRAS if e.entity == "station")
    options = extra_options(station)
    assert options == list(dict.fromkeys(options)), "the picker would list the same word twice"
    assert "washing" in options and "drying" in options


def test_water_level_round_trip():
    from dida_adapter_dreame.mapping import extra_caps, water_volume_id
    for option in ("low", "medium", "high"):
        raw = water_volume_id(option)
        assert raw is not None
        assert extra_caps([{"did": "water_volume", "code": 0, "value": raw}]) == {"water_volume": option}
    assert water_volume_id("drenched") is None


# --- the packed cleaning mode -------------------------------------------------

def test_the_reported_zero_means_vacuum_and_mop_together():
    # The live robot reports 5120. Read naively that is "sweeping"; the low bits are
    # swapped on a machine whose mop pad lifts, so it is in fact both jobs at once.
    from dida_adapter_dreame.mapping import cleaning_mode_name
    assert cleaning_mode_name(5120) == "sweeping and mopping"
    assert cleaning_mode_name(5122) == "sweeping"
    assert cleaning_mode_name(5121) == "mopping"
    assert cleaning_mode_name(5123) == "mopping after sweeping"
    assert cleaning_mode_name(None) is None and cleaning_mode_name(True) is None


def test_writing_a_mode_keeps_every_other_packed_setting():
    # The same integer carries how often the mop goes back to be washed (byte 1) and
    # the mopping route (byte 2). Changing "also mop" must not reset either.
    from dida_adapter_dreame.mapping import CLEANING_MODES, cleaning_mode_packed
    packed = (3 << 16) | (20 << 8) | 0     # route 3, self-clean 20, both jobs
    for option in CLEANING_MODES:
        out = cleaning_mode_packed(packed, option)
        assert out is not None
        assert (out >> 8) & 0xFF == 20, f"{option} lost the self-clean interval"
        assert out >> 16 == 3, f"{option} lost the mopping route"
    assert cleaning_mode_packed(packed, "polishing") is None


def test_cleaning_mode_round_trips_through_the_packing():
    from dida_adapter_dreame.mapping import CLEANING_MODES, cleaning_mode_name, cleaning_mode_packed
    for option in CLEANING_MODES:
        assert cleaning_mode_name(cleaning_mode_packed(5120, option)) == option


def test_the_station_is_read_only_text_not_a_picker():
    """It reports what the base is doing; it takes no orders. As an `enum` the UI
    would render a picker whose choices the adapter has no way to honour."""
    from dida_adapter_dreame.mapping import EXTRAS, extra_caps
    station = next(e for e in EXTRAS if e.entity == "station")
    assert station.capability == "text"
    assert extra_caps([{"did": "self_wash_base_status", "code": 0, "value": 2}]) == {"station": "drying"}


def test_the_mode_rides_on_the_robot_itself_not_a_side_entity():
    """It is a setting in the same rank as the suction level, so it belongs to the
    same entity — a separate entity would put it off the robot's card."""
    from dida_adapter_dreame.adapter import _CAPS
    assert "vacuum_mode" in _CAPS and "vacuum_mode_options" in _CAPS
    assert "enum" in _CAPS, "the suction level keeps its slot"


def test_every_mode_we_publish_is_in_the_canonical_vocabulary():
    from dida_adapter_dreame.mapping import CLEANING_MODES
    from dida_core.capabilities import CAPABILITIES, CapabilityKind
    assert set(CLEANING_MODES) <= set(CAPABILITIES[CapabilityKind.VACUUM_MODE].choices), \
        "a mode the core would reject at the boundary"



def test_the_adapter_says_out_loud_whether_the_plan_is_linked():
    """A send button that accepts a tap and quietly does nothing is the worst of both
    worlds, so whether a tap can mean anything is published as state, not logged."""
    from dida_adapter_dreame.adapter import _LINK_ENTITY, DreameAdapter
    from dida_adapter_dreame.area import Placement

    assert _LINK_ENTITY == "plan_link"
    assert Placement.load("") is None, "a map never laid down reads as not linked"
    assert hasattr(DreameAdapter, "_publish_link")




# --- the map laid over the plan -----------------------------------------------

def _map_bytes(w=4, h=4, cells=None, extra=None):
    import base64
    import json as _json
    import struct as _struct
    import zlib

    header = bytearray(27)
    _struct.pack_into("<h", header, 17, 50)     # 50 mm per cell
    _struct.pack_into("<h", header, 19, w)
    _struct.pack_into("<h", header, 21, h)
    _struct.pack_into("<h", header, 23, 0)      # origin x
    _struct.pack_into("<h", header, 25, 0)      # origin y
    # Default: every cell driven, so the crop is the whole canvas and a geometry
    # test measures the transform rather than the cropping.
    grid = bytearray(cells if cells is not None else bytes([1]) * (w * h))
    body = bytes(header) + bytes(grid) + _json.dumps(extra or {}).encode()
    packed = base64.urlsafe_b64encode(zlib.compress(body)).decode()
    return _json.dumps({"mapstr": [{"id": 0, "map": packed}]}).encode()


def test_rooms_come_out_of_the_grid_with_their_middles():
    from dida_adapter_dreame.area import decode_map
    cells = bytearray(16)
    cells[0] = cells[1] = 1
    cells[15] = 2 | 0x80                        # a wall cell belongs to no room
    frame = decode_map(_map_bytes(cells=cells, extra={"seg_inf": {"1": {"type": 4}}}))
    assert [r.id for r in frame.rooms] == [1]
    assert frame.room(1).name == "Kitchen" and frame.room(1).centre == (25.0, 0.0)
    assert frame.room(9) is None


def test_a_plan_point_lands_where_the_picture_was_dragged():
    from dida_adapter_dreame.area import Placement, decode_map
    frame = decode_map(_map_bytes())             # 4x4 cells of 50 mm = 200 x 200 mm
    laid = Placement(x=10, y=10, w=20)           # a 20 %-wide box at 10,10
    assert laid.to_map((20.0, 20.0), frame) == (100.0, 100.0), "its middle is the map's middle"
    assert laid.to_map((10.0, 10.0), frame) == (0.0, 0.0), "its corner is the map's corner"


def test_mirroring_flips_only_the_one_axis():
    from dida_adapter_dreame.area import Placement, decode_map
    frame = decode_map(_map_bytes())
    plain = Placement(x=0, y=0, w=20)
    flipped = Placement(x=0, y=0, w=20, mirrored=True)
    assert plain.to_map((5.0, 5.0), frame) == (50.0, 50.0)
    assert flipped.to_map((5.0, 5.0), frame) == (150.0, 50.0), "x mirrors, y does not"


def test_turning_the_picture_turns_the_translation():
    from dida_adapter_dreame.area import Placement, decode_map
    frame = decode_map(_map_bytes())
    turned = Placement(x=0, y=0, w=20, rotation=180)
    x, y = turned.to_map((5.0, 5.0), frame)
    assert round(x) == 150 and round(y) == 150, "half a turn about the middle"


def test_a_drawn_area_becomes_the_rectangle_the_robot_expects():
    from dida_adapter_dreame.area import Placement, decode_map, zone_for
    frame = decode_map(_map_bytes(w=40, h=40))  # 40 x 40 cells of 50 mm = 2 x 2 m
    laid = Placement(x=0, y=0, w=20)            # the whole map drawn 20 % wide
    drawn = zone_for(laid, frame, (2.0, 2.0), (18.0, 14.0))
    assert drawn == [200, 200, 1800, 1400], "both corners translated, then normalised"


def test_the_corners_are_normalised_after_the_map_is_turned_or_mirrored():
    """Drawing right-to-left, or over a mirrored map, must still hand the robot a
    rectangle written left-top to right-bottom in ITS frame."""
    from dida_adapter_dreame.area import Placement, decode_map, zone_for
    frame = decode_map(_map_bytes(w=40, h=40))
    for laid in (Placement(x=0, y=0, w=20),
                 Placement(x=0, y=0, w=20, mirrored=True),
                 Placement(x=0, y=0, w=20, rotation=180)):
        zone = zone_for(laid, frame, (18.0, 14.0), (2.0, 2.0))
        assert zone[0] < zone[2] and zone[1] < zone[3], f"{laid} produced an inside-out box"


def test_a_patch_is_never_smaller_than_the_robot_accepts():
    from dida_adapter_dreame.area import MIN_ZONE_MM, Placement, decode_map, zone_for
    frame = decode_map(_map_bytes())
    zone = zone_for(Placement(x=0, y=0, w=20), frame, (10.0, 10.0), (10.05, 10.05))
    assert zone[2] - zone[0] >= MIN_ZONE_MM and zone[3] - zone[1] >= MIN_ZONE_MM


def test_an_area_bigger_than_the_robot_takes_is_refused():
    from dida_adapter_dreame.area import MapError, Placement, decode_map, zone_for
    frame = decode_map(_map_bytes())
    laid = Placement(x=0, y=0, w=0.2)          # the map drawn tiny: a small drag covers metres
    with pytest.raises(MapError, match="larger"):
        zone_for(laid, frame, (0.0, 0.0), (20.0, 20.0))


def test_an_unreadable_placement_is_loud_and_an_empty_one_is_simply_absent():
    from dida_adapter_dreame.area import MapError, Placement
    assert Placement.load("") is None
    with pytest.raises(MapError):
        Placement.load("{not json")
    laid = Placement.load('{"x": 1, "y": 2, "w": 30, "rotation": 90, "mirrored": true}')
    assert (laid.x, laid.y, laid.w, laid.rotation, laid.mirrored) == (1, 2, 30, 90, True)


def test_the_map_renders_as_a_png_with_rooms_and_walls():
    from dida_adapter_dreame.area import render
    cells = bytearray(16)
    cells[0] = 1
    cells[5] = 2
    cells[10] = 0x80                             # wall
    png, frame = render(_map_bytes(cells=cells))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 60
    # cells at (0,0), (1,1) and (2,2) → the picture is their bounding box, not the canvas
    assert (frame.width, frame.height) == (3, 3) and frame.grid_mm == 50


def test_the_picture_is_cropped_to_what_the_robot_has_seen():
    """The robot's canvas is bigger than the house. An overlay padded with blank
    edges cannot be lined up by eye — you would be sizing the padding."""
    from dida_adapter_dreame.area import decode_map

    cells = bytearray(16)      # a 4x4 canvas with one cell driven, at (2, 1)
    cells[1 * 4 + 2] = 1
    frame = decode_map(_map_bytes(cells=cells, extra={"seg_inf": {"1": {"type": 1}}}))
    assert (frame.width, frame.height) == (1, 1), "cropped to the driven cell"
    assert frame.origin == (100, 50), "and the origin moved with the crop"
    assert frame.room(1).centre == (100.0, 50.0), "so a room's middle still lands in millimetres"


def test_a_map_with_nothing_driven_says_so():
    from dida_adapter_dreame.area import MapError, decode_map
    with pytest.raises(MapError, match="empty"):
        decode_map(_map_bytes(cells=bytearray(16)))


# --- the robot while it works -------------------------------------------------

def _frame_message(robot=(1000, -2000), heading=90, track="l1000,-2000", areas=None):
    import base64
    import json as _json
    import struct as _struct
    import zlib

    header = bytearray(27)
    _struct.pack_into("<h", header, 5, robot[0])
    _struct.pack_into("<h", header, 7, robot[1])
    _struct.pack_into("<h", header, 9, heading)
    _struct.pack_into("<h", header, 11, -6)      # charger x
    _struct.pack_into("<h", header, 13, 459)     # charger y
    _struct.pack_into("<h", header, 17, 50)
    _struct.pack_into("<h", header, 19, 0)       # a live frame carries no grid of its own
    _struct.pack_into("<h", header, 21, 0)
    tail = {"timestamp_ms": 1700, "tr": track}
    if areas is not None:
        tail["da2"] = {"areas": areas}
    body = bytes(header) + _json.dumps(tail).encode()
    packed = base64.urlsafe_b64encode(zlib.compress(body)).decode()
    return _json.dumps({"id": 1, "did": -1,
                        "data": {"params": [{"did": "-1", "siid": 6, "piid": 1, "value": packed}]}}).encode()


def test_a_live_frame_gives_position_heading_and_the_area_it_was_sent_to():
    from dida_adapter_dreame.live import Live, apply_frame

    live = Live()
    assert apply_frame(live, _frame_message(areas=[[4429, -3866, 6402, -593, 1, 1, 3]]))
    assert live.robot == (1000, -2000) and live.heading == 90
    assert live.charger == (-6, 459)
    assert live.areas == [[4429, -3866, 6402, -593]], "the extras after the corners are not the area"


def test_the_track_accumulates_and_does_not_repeat_itself():
    from dida_adapter_dreame.live import Live, apply_frame

    live = Live()
    apply_frame(live, _frame_message(robot=(10, 10), track="l10,10"))
    apply_frame(live, _frame_message(robot=(20, 20), track="l10,10"))   # same point again
    apply_frame(live, _frame_message(robot=(30, 30), track="l30,30"))
    assert live.track == [(10, 10), (30, 30)]
    live.clear_track()
    assert live.track == [], "a new job starts a new line"


def test_an_ordinary_property_push_is_not_mistaken_for_a_frame():
    import json as _json

    from dida_adapter_dreame.live import Live, apply_frame

    live = Live()
    battery = _json.dumps({"data": {"params": [{"siid": 3, "piid": 1, "value": 93}]}}).encode()
    assert not apply_frame(live, battery)
    assert live.robot is None
    assert not apply_frame(live, b"not json")


def test_the_broker_wants_a_client_id_of_its_own_shape():
    """Credentials alone are refused as unauthorized — the NAME is what it checks."""
    from dida_adapter_dreame.live import client_id
    assert client_id("VN771122", "10000.mt.eu.iot.dreame.tech", "ABCDEF") \
        == "p_VN771122_ABCDEF_10000.mt.eu.iot.dreame.tech"


def test_an_acknowledgement_on_the_same_topic_does_not_end_the_subscription():
    """Their broker mixes plain acknowledgements in with the frames, and a parameter
    that is a bare string used to escape and cost a reconnect every few seconds."""
    import json as _json

    from dida_adapter_dreame.live import Live, apply_frame

    live = Live()
    for payload in (_json.dumps({"data": "ok"}).encode(),
                    _json.dumps({"data": {"params": "ok"}}).encode(),
                    _json.dumps({"data": {"params": ["ok"]}}).encode(),
                    _json.dumps(["not", "an", "object"]).encode()):
        assert not apply_frame(live, payload)
    assert live.robot is None


# --- a house with more than one storey -----------------------------------------

def _two_maps(current=8):
    import base64
    import json as _json
    import struct as _struct
    import zlib

    def one(fill, charger):
        header = bytearray(27)
        _struct.pack_into("<h", header, 11, charger[0])
        _struct.pack_into("<h", header, 13, charger[1])
        _struct.pack_into("<h", header, 17, 50)
        _struct.pack_into("<h", header, 19, 4)
        _struct.pack_into("<h", header, 21, 4)
        _struct.pack_into("<h", header, 23, 0)
        _struct.pack_into("<h", header, 25, 0)
        body = bytes(header) + bytes([fill]) * 16 + b"{}"
        return base64.urlsafe_b64encode(zlib.compress(body)).decode()

    return _json.dumps({"curr_id": current, "mapstr": [
        {"id": 1, "name": "Downstairs", "map": one(1, (-9, 468))},
        {"id": 0, "name": "Upstairs", "map": one(2, (452, -9))}]}).encode()


def test_the_map_read_is_the_one_whose_dock_the_robot_reports():
    """The file's curr_id is not the entries' id — with two storeys it reads 8 while
    they are numbered 1 and 0. The dock is what tells them apart, and the live stream
    reports the dock from wherever the robot actually is."""
    from dida_adapter_dreame.area import decode_map, pick_map

    _entry, key = pick_map(_two_maps(), charger=(452, -9))
    assert key == "Upstairs"
    assert decode_map(_two_maps(), charger=(452, -9)).room(2) is not None
    _entry, key = pick_map(_two_maps(), charger=(-9, 468))
    assert key == "Downstairs"
    # No live position yet: the first map is used and corrects itself on the next frame.
    assert pick_map(_two_maps())[1] == "Downstairs"
    # A dock nowhere near either map is not evidence of anything.
    assert pick_map(_two_maps(), charger=(99000, 99000))[1] == "Downstairs"


def test_each_map_keeps_its_own_arrangement():
    from dida_adapter_dreame.area import Placement

    store = '{"Downstairs": {"x": 1, "y": 2, "w": 30, "floor": "ground"}, ' \
            '"Upstairs": {"x": 5, "y": 6, "w": 40, "floor": "upstairs"}}'
    assert Placement.load(store, "Downstairs").floor == "ground"
    assert Placement.load(store, "Upstairs").w == 40
    assert Placement.load(store, "Attic") is None, "a map never laid down has no arrangement"


def test_the_single_map_shape_still_reads():
    """What was stored before the house had two floors belongs to whichever map was
    current then — it must not read as nothing."""
    from dida_adapter_dreame.area import Placement

    assert Placement.load('{"x": 1, "y": 2, "w": 30}', "Downstairs").w == 30
    assert Placement.load('{"x": 1, "y": 2, "w": 30}', "Upstairs").w == 30


def test_every_saved_map_is_offered_not_only_the_one_under_the_robot():
    """A storey the robot is not standing on is still a storey whose picture belongs
    over its own plan — waiting to carry the robot upstairs to arrange it would be a
    silly thing to ask."""
    from dida_adapter_dreame.area import all_maps, render_entry

    maps = all_maps(_two_maps())
    assert [key for key, _ in maps] == ["Downstairs", "Upstairs"]
    for _key, entry in maps:
        png, frame = render_entry(entry)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and frame.grid_mm == 50
