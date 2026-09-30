"""Regression tests for the pure geo helpers (dida_core.geo).

haversine_m() and resolve_zone() are deterministic circle-membership math with
no I/O — the zone resolution shared by every presence source (browser
geolocation, OwnTracks). test_core_semantics.py already covers resolve_zone's
basic membership/antipode-safety claim; this file covers haversine_m directly
(known great-circle distances) and the resolve_zone tie-break rules (nested
zones, equal-radius nearest-centre, empty zone list, inclusive boundary) that
aren't exercised elsewhere.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_geo.py"
"""
import pytest
from dida_core.geo import AWAY, haversine_m, resolve_zone


def test_haversine_m_same_point_is_zero():
    assert haversine_m(45.8, 16.0, 45.8, 16.0) == 0.0, "distance from a point to itself is 0"


def test_haversine_m_one_degree_latitude():
    # 1 degree of latitude is ~111.19 km on a sphere of radius 6371 km, regardless of longitude.
    assert haversine_m(0.0, 0.0, 1.0, 0.0) == pytest.approx(111194.926644, rel=1e-6), \
        "1 degree of latitude is ~111.19 km"


def test_haversine_m_short_distance():
    assert haversine_m(45.8, 16.0, 45.8005, 16.0) == pytest.approx(55.597463, rel=1e-6), \
        "~55 m over 0.0005 degrees of latitude"


def test_haversine_m_quarter_circumference():
    # 90 degrees of longitude at the equator is a quarter of Earth's circumference.
    assert haversine_m(0.0, 0.0, 0.0, 90.0) == pytest.approx(10007543.398010, rel=1e-6), \
        "90 deg of longitude at the equator is a quarter of the great circle"


def test_haversine_m_symmetric():
    a = haversine_m(45.8, 16.0, 46.5, 16.9)
    b = haversine_m(46.5, 16.9, 45.8, 16.0)
    assert a == pytest.approx(b, rel=1e-9), "haversine distance is symmetric in its two points"


def test_resolve_zone_nested_smaller_radius_wins():
    # A small zone inside a bigger one at the SAME centre: "most specific" (smallest
    # radius) wins, regardless of iteration order.
    zones = [("Big", 45.8, 16.0, 200.0), ("Small", 45.8, 16.0, 50.0)]
    assert resolve_zone(45.8, 16.0, zones) == "Small", "smaller-radius zone wins when nested"
    assert resolve_zone(45.8, 16.0, list(reversed(zones))) == "Small", \
        "winner is independent of list order"


def test_resolve_zone_equal_radius_nearest_centre_wins():
    # Same radius, different centres: the tie-break is the nearest centre.
    zones = [("A", 45.8, 16.0, 200.0), ("B", 45.8005, 16.0, 200.0)]
    assert resolve_zone(45.8, 16.0, zones) == "A", "equal radius -> nearest centre wins"


def test_resolve_zone_empty_zones_is_away():
    assert resolve_zone(45.8, 16.0, []) == AWAY, "no zones at all -> AWAY"


def test_resolve_zone_boundary_is_inclusive():
    # A point exactly on the radius boundary (dist == radius) still counts as inside
    # (resolve_zone uses `dist <= radius`).
    radius = haversine_m(45.8, 16.0, 46.8, 16.0)
    zones = [("Home", 46.8, 16.0, radius)]
    assert resolve_zone(45.8, 16.0, zones) == "Home", "dist == radius is inside (<=), not AWAY"


def test_resolve_zone_outside_every_zone_is_away():
    zones = [("Home", 45.8, 16.0, 150.0)]
    assert resolve_zone(0.0, 0.0, zones) == AWAY, "far outside every zone -> AWAY"


def test_resolve_zone_accuracy_credit_keeps_coarse_fix_in_zone():
    # A coarse WiFi/cell fix ~250 m off centre with a 300 m error circle still
    # overlaps the 100 m Home zone -> must NOT assert "away" (the 2026-07-13
    # "locira me daleko dok sam na kućnom wifiju" regression).
    zones = [("Home", 45.8131, 15.9772, 100.0)]
    assert resolve_zone(45.812615, 15.973961, zones) == AWAY, "precise fix outside -> away"
    assert resolve_zone(45.812615, 15.973961, zones, acc_m=300.0) == "Home", \
        "error circle overlapping the zone -> in zone"


def test_resolve_zone_accuracy_credit_is_capped():
    # The credit caps at ACC_CREDIT_MAX_M: a kilometre-grade fix can't glue a
    # person to a zone from across town.
    zones = [("Home", 45.8131, 15.9772, 100.0)]
    far = (45.8131, 15.9872)  # ~780 m east of centre
    assert resolve_zone(*far, zones, acc_m=5000.0) == AWAY, \
        "credit capped at 500 m -> 780 m stays away even with a 5 km-accuracy fix"
    near = (45.8131, 15.9812)  # ~310 m east of centre
    assert resolve_zone(*near, zones, acc_m=5000.0) == "Home", \
        "within radius+cap the (capped) credit still applies"
