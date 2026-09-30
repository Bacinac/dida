"""Regression tests for the retention TTL DDL generator (dida_core.retention).

`_ttl_expr` turns the Postgres retention policy (class rows + per-capability
groups + per-entity overrides) into a ClickHouse `MODIFY TTL` expression; `_q`
escapes string literals into that DDL. Both are pure and security-relevant (the
generated string is executed as ClickHouse DDL), and the audit flagged them as
prime infra-free test targets. No DB — feed dicts, assert the emitted SQL.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_retention.py"
"""
from dida_core.retention import _q, _ttl_expr


def test_q_escaping():
    # _q: ClickHouse string-literal escaping (quote doubling + backslash)
    assert _q("abc") == "'abc'", "_q wraps a plain string"
    assert _q(90) == "'90'", "_q stringifies non-strings"
    assert _q("it's") == "'it''s'", "_q doubles a single quote"
    assert _q("a\\b") == "'a\\\\b'", "_q doubles a backslash"
    # the smuggling case the code comment guards: a TRAILING backslash must be doubled
    # so it can't escape the closing quote and break out of the literal.
    assert _q("x\\") == "'x\\\\'", "_q doubles a trailing backslash (no quote break-out)"


def test_ttl_expr_no_branches():
    # _ttl_expr: no capability groups, no overrides → the simple else-only form
    classes = {"default": {"days_raw": 90}}
    assert (
        _ttl_expr("ts", "days_raw", classes, [], [], 90) == "ts + toIntervalDay(90)"
    ), "no branches → plain tscol + toIntervalDay(default)"


def test_ttl_expr_class_group():
    # one capability class group → multiIf(IN-list, class_days, default_days)
    classes = {"default": {"days_raw": 90}, "climate": {"days_raw": 30}}
    caps = [{"capability": "temperature", "class_name": "climate"}, {"capability": "humidity", "class_name": "climate"}]
    assert (
        _ttl_expr("ts", "days_raw", classes, caps, [], 90)
        == "ts + toIntervalDay(multiIf(capability IN ('humidity', 'temperature'), 30, 90))"
    ), "class group → sorted IN-list + class days, default as else"


def test_ttl_expr_override_precedes_class():
    # a per-entity override must precede the class rule (multiIf short-circuits)
    classes = {"default": {"days_raw": 90}, "climate": {"days_raw": 30}, "volatile": {"days_raw": 7}}
    caps = [{"capability": "temperature", "class_name": "climate"}]
    overrides = [{"entity_id": "sensor:x", "capability": "temperature", "class_name": "volatile"}]
    assert (
        _ttl_expr("ts", "days_raw", classes, caps, overrides, 90)
        == "ts + toIntervalDay(multiIf("
        "(entity_id = 'sensor:x' AND capability = 'temperature'), 7, "
        "capability IN ('temperature'), 30, 90))"
    ), "override branch comes first → its 7 days wins over the class 30"


def test_ttl_expr_default_class_is_else():
    # the 'default' class is the else branch, never its own IN-list group
    assert (
        _ttl_expr("ts", "days_raw", {"default": {"days_raw": 90}}, [{"capability": "foo", "class_name": "default"}], [], 90)
        == "ts + toIntervalDay(90)"
    ), "capabilities in the default class collapse into the else branch"


def test_ttl_expr_unknown_class_falls_back_to_default():
    # days_of fallback: an unknown class name falls back to 'default'
    assert (
        _ttl_expr("ts", "days_raw", {"default": {"days_raw": 90}}, [{"capability": "foo", "class_name": "ghost"}], [], 90)
        == "ts + toIntervalDay(multiIf(capability IN ('foo'), 90, 90))"
    ), "unknown class name resolves to the default class's days"


def test_ttl_expr_no_default_row_uses_fallback():
    # days_of fallback: no 'default' row at all → the caller's fallback_days
    assert (
        _ttl_expr("bucket", "days_1d", {}, [], [], 730) == "bucket + toIntervalDay(730)"
    ), "no default class row → fallback_days is used"
