"""Verbosity is one env var away, and a typo in it is loud.

Thirty-nine services used to hardcode `basicConfig(level=INFO)`. Raising ONE
adapter to DEBUG mid-incident meant editing code, rebuilding its image and
redeploying — which is exactly when you can least afford it. Every adapter is
already its own container, so a plain env var gives per-adapter granularity.

The silent-fallback case is the one worth pinning: someone who sets
DIDA_LOG_LEVEL=verbose and gets INFO with no complaint concludes the switch is
broken and stops reaching for it.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src python -m pytest tests/test_logsetup.py"
"""
from __future__ import annotations

import logging

import pytest
from dida_core.logsetup import LEVELS, setup_logging


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


@pytest.mark.parametrize("name", LEVELS)
def test_every_documented_level_is_applied(monkeypatch, name):
    monkeypatch.setenv("DIDA_LOG_LEVEL", name)
    assert setup_logging() == getattr(logging, name)


def test_the_default_is_info_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("DIDA_LOG_LEVEL", raising=False)
    assert setup_logging() == logging.INFO


def test_lowercase_and_whitespace_are_accepted(monkeypatch):
    # A human types `debug`, not `DEBUG`. Refusing that would be pedantry, and the
    # loud-failure path below is for genuine typos, not for casing.
    monkeypatch.setenv("DIDA_LOG_LEVEL", "  debug ")
    assert setup_logging() == logging.DEBUG


def test_an_unrecognised_level_COMPLAINS_and_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("DIDA_LOG_LEVEL", "verbose")
    with caplog.at_level(logging.ERROR, logger="dida.logsetup"):
        assert setup_logging() == logging.INFO
    assert any("VERBOSE" in r.getMessage() for r in caplog.records), \
        "the bad value is named in the complaint, not swallowed"


def test_an_empty_value_is_treated_as_unset(monkeypatch, caplog):
    # `DIDA_LOG_LEVEL=` in a .env is a common way to "clear" a setting; it must
    # mean the default, not an error.
    monkeypatch.setenv("DIDA_LOG_LEVEL", "")
    with caplog.at_level(logging.ERROR, logger="dida.logsetup"):
        assert setup_logging() == logging.INFO
    assert not caplog.records, "an empty value is not a typo"


def test_a_custom_env_name_is_honoured(monkeypatch):
    monkeypatch.setenv("OTHER_LEVEL", "WARNING")
    assert setup_logging(env="OTHER_LEVEL") == logging.WARNING


def test_no_service_hardcodes_the_level_any_more():
    """The regression this exists to prevent: one service quietly reintroducing
    `basicConfig(level=logging.INFO)` and becoming undebuggable again."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for d in ("core", "services", "adapters"):
        for f in (root / d).rglob("*.py"):
            # `logsetup.py` IS the switch, so it is allowed to call basicConfig. There
            # are two: core's, and planvision's — that service builds on its own
            # base and cannot import core. test_planvision_errors keeps them identical.
            if "__pycache__" in str(f) or f.name == "logsetup.py":
                continue
            if "logging.basicConfig(level=logging.INFO" in f.read_text(errors="ignore"):
                offenders.append(str(f.relative_to(root)))
    assert not offenders, f"hardcoded log level in: {offenders}"
