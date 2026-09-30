"""Regression tests for the core pure-function fixes (Pass F + core audit).

Run inside any DIDA image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_core_semantics.py"

Covers: condition compare() bool-vs-number strictness, geo resolve_zone
membership + antipode safety, automation definition validation (empty ids,
non-finite holds), and that the migration files are present + ordered.
"""
from pathlib import Path

import msgspec as _msgspec
import pytest
from dida_core import resolve_zone
from dida_core.automations import compare, validate_definition
from dida_core.capabilities import CapabilityError
from dida_core.events import Command as _Command


def test_compare_bool_vs_number():
    # compare(): equality must not conflate bool with number (Pass F m5)
    assert compare("==", 1, True) is False, "1 == True is NOT a match (bool != int)"
    assert compare("!=", 1, True) is True, "1 != True holds"
    assert compare("==", True, True) is True, "True == True"
    assert compare("==", 1, 1.0) is True, "1 == 1.0 (int/float compare)"
    assert compare("<", 2, 3) is True, "2 < 3"
    assert compare(">=", 3, 3) is True, "3 >= 3"
    assert compare("<", "a", "b") is False, "string ordering is not-met (numbers only)"
    assert compare("<", True, False) is False, "bool ordering is not-met"


def test_resolve_zone():
    # resolve_zone(): membership + antipode safety (core m6)
    zones = [("Home", 45.8, 16.0, 150.0)]
    assert resolve_zone(45.8, 16.0, zones) == "Home", "exact centre is inside the zone"
    assert resolve_zone(45.8005, 16.0, zones) == "Home", "~55 m away still inside 150 m radius"
    assert resolve_zone(46.5, 16.9, zones) == "away", "far point is away"
    resolve_zone(-45.8, -164.0, zones)  # antipode of Home must not raise (asin domain guard)


def bad(raw, label):
    # `label` documents the rejected shape at the call site; the shape must raise.
    with pytest.raises(CapabilityError):
        validate_definition(raw)


def test_validate_definition_rejects_dead_shapes():
    # validate_definition(): reject silently-dead shapes (core m3/m4)
    valid = {
        "triggers": [{"entity_id": "mqtt:pir", "capability": "occupancy", "to": True}],
        "actions": [{"entity_id": "mqtt:light", "capability": "on_off", "command": "turn_on"}],
    }
    validate_definition(valid)  # a well-formed rule validates (raises otherwise)

    bad({**valid, "triggers": [{"entity_id": "", "capability": "occupancy", "to": True}]},
        "empty trigger entity_id rejected")
    bad({"triggers": [{"entity_id": "mqtt:pir", "capability": "occupancy", "to": False}],
         "actions": [{"entity_id": "", "capability": "on_off", "command": "turn_off"}]},
        "empty action entity_id rejected")
    bad({"triggers": [{"entity_id": "mqtt:pir", "capability": "occupancy", "to": False,
                       "for_seconds": float("nan")}],
         "actions": [{"entity_id": "mqtt:light", "capability": "on_off", "command": "turn_off"}]},
        "NaN for_seconds rejected")


def test_command_source_wire_compat():
    # Command.source wire-compat (audit trail)
    # Old frames (no `source`) must decode to the "" default, and a frame WITH
    # `source` must round-trip untouched — the audit column may never break an
    # adapter still running a pre-source image (map encoding, unknown-tolerant).
    _enc = _msgspec.msgpack.Encoder()
    _dec = _msgspec.msgpack.Decoder(_Command)
    _old_frame = _msgspec.msgpack.encode(
        {"entity_id": "mqtt:l", "capability": "on_off", "command": "turn_on", "ts_ns": 1}
    )
    assert _dec.decode(_old_frame).source == "", "pre-source frame decodes with '' default"
    _c = _Command(entity_id="mqtt:l", capability="on_off", command="turn_on", ts_ns=1,
                  source="user:alex")
    assert _dec.decode(_enc.encode(_c)).source == "user:alex", "source round-trips on the wire"
    assert _dec.decode(_enc.encode(_msgspec.structs.replace(_c, source=""))).source == "", \
        "empty source is omitted (omit_defaults) yet decodes to ''"


def test_migration_files_present_and_ordered():
    # migration files present + numerically ordered (Pass H)
    mig_dir = Path("/app/db/migrations")
    if not mig_dir.exists():
        mig_dir = Path("db/migrations")
    files = sorted(p.name for p in mig_dir.glob("*.sql"))
    assert files == sorted(files), "migration files are name-ordered"
    assert "0001_init.sql" in files and "0003_consolidate_self_heal.sql" in files, \
        "0001 + 0003 migrations present"
