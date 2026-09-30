"""Unit test — the IAMMETER payload → facets mapping, single- and three-phase.

The meter answers in one of two shapes: `Data` (a flat row, WEM3080) or `Datas`
(one row per phase, WEM3080T). The three-phase meter at the second location was
invisible for exactly this reason, so the properties pinned here are the ones that
make the two locations interchangeable: the mains entity ids are identical whatever
the meter is, the totals are the whole service (extensive summed, intensive
averaged), and the per-phase facets never add a second energy counter — the energy
dashboard reads every `*_energy` leaf on a grid meter as import/export, so a
per-phase counter would silently double the house total.

Pure — no bus, no aiohttp. Runs in the api image via the working-tree path.
"""
from dida_adapter_iammeter.adapter import CATALOG, _facets, _mains, _rows
from dida_api.energy import _suggest_roles

SINGLE = {"SN": "1E979533", "Data": [231.65, 9.12, -2034.0, 15845.55, 4817.85, 49.96, 0.96]}
THREE = {"SN": "35D32E5E", "Datas": [
    [234.1, 0.120, 21.0, 1769.745, 0.112, 49.97, 0.70],
    [234.3, 0.360, 79.0, 2543.820, 0.000, 49.97, 0.93],
    [234.4, 0.690, 124.0, 3853.698, 0.495, 49.97, 0.76],
]}


def _values(payload):
    return {suffix: value for suffix, _c, _n, _d, _u, value in _facets(_rows(payload))}


def test_both_meter_shapes_are_recognised():
    assert _rows(SINGLE) == [SINGLE["Data"]]
    assert _rows(THREE) == THREE["Datas"]


def test_unrecognised_payload_yields_nothing():
    """No rows is what makes the adapter fail LOUD instead of publishing zeros."""
    for payload in ({}, {"Data": [1.0, 2.0]}, {"Datas": []}, {"Datas": [[1.0, 2.0]]},
                    {"Datas": "nope"}, [1, 2, 3], None):
        assert _rows(payload) == [], payload


def test_single_phase_passes_readings_through():
    v = _values(SINGLE)
    assert [s for s, *_ in _facets(_rows(SINGLE))] == [s for s, *_ in CATALOG], "no extra facets"
    assert v["voltage"] == 231.65
    assert v["power"] == -2034.0, "export keeps its negative sign"
    assert v["import_energy"] == 15845.55
    assert v["power_factor"] == 0.96


def test_three_phase_totals_are_the_whole_service():
    v = _values(THREE)
    assert v["power"] == 224.0, "power adds up across the phases"
    assert round(v["current"], 3) == 1.17
    assert round(v["import_energy"], 3) == 8167.263
    assert round(v["export_energy"], 3) == 0.607
    assert round(v["voltage"], 3) == 234.267, "voltage is the mean, not the sum"
    assert v["frequency"] == 49.97
    assert round(v["power_factor"], 4) == 0.7967


def test_mains_entity_ids_do_not_depend_on_the_meter():
    """An automation written against `iammeter:meter:power` has to work at either
    location — the phase facets are additions, never replacements."""
    mains = {s for s, *_ in _mains()}
    assert mains <= _values(SINGLE).keys()
    assert mains <= _values(THREE).keys()


def test_per_phase_facets_are_diagnostics_and_carry_no_energy():
    phase = [f for f in _facets(_rows(THREE)) if f[0].startswith(("l1_", "l2_", "l3_"))]
    assert len(phase) == 12, "4 facets × 3 phases"
    assert all(diag for _s, _c, _n, diag, _u, _v in phase), "per-phase readings are diagnostic"
    assert not any(cap == "energy" for _s, cap, *_ in phase), "no per-phase kWh counter"
    assert [n for _s, _c, n, *_ in phase][:2] == ["L1 voltage", "L1 current"]
    assert _values(THREE)["l3_power"] == 124.0


def test_three_phase_meter_still_counts_once_on_the_energy_dashboard():
    """The guard that matters: role suggestion groups by device, so any second
    `*_energy` leaf on the meter would be counted as grid import/export again."""
    ids = [f"iammeter:meter:{s}" for s in _values(THREE)]
    roles = _suggest_roles(ids)
    assert sum(r == "grid_import" for r in roles.values()) == 1
    assert sum(r == "grid_export" for r in roles.values()) == 1
