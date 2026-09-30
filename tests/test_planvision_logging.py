"""planvision is the one service outside `dida_core` — it still answers to DIDA_LOG_LEVEL.

Its own base keeps it apart from the bus contract, and that isolation is deliberate. What it silently cost was the switch every other
service has: raising this one to DEBUG mid-incident meant editing code, rebuilding
and redeploying — which is precisely when you can least afford to. The switch is
therefore reimplemented locally, and these tests exist to keep the second copy from
drifting away from the first.
"""

from __future__ import annotations

import logging
import os

from dida_planvision.logsetup import setup_logging


def test_planvision_honours_DIDA_LOG_LEVEL_like_every_other_service(monkeypatch):
    monkeypatch.setenv("DIDA_LOG_LEVEL", "DEBUG")
    assert setup_logging() == logging.DEBUG


def test_an_unrecognised_level_falls_back_to_INFO_and_says_so(monkeypatch, caplog):
    """Silence would teach the operator that the switch does not work."""
    monkeypatch.setenv("DIDA_LOG_LEVEL", "verbose")
    with caplog.at_level(logging.ERROR):
        assert setup_logging() == logging.INFO
    assert "not one of" in caplog.text


def test_the_default_is_INFO_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("DIDA_LOG_LEVEL", raising=False)
    assert setup_logging() == logging.INFO


def test_uvicorn_is_not_allowed_to_reinstall_its_own_log_config():
    """`log_config=None` is the whole reason DIDA_LOG_LEVEL governs this service:
    uvicorn's default dictConfig replaces the root logger it just configured."""
    main = os.path.join(os.path.dirname(__file__), "..", "services", "planvision",
                        "src", "dida_planvision", "__main__.py")
    with open(main) as fh:
        source = fh.read()
    assert "log_config=None" in source
    assert "setup_logging()" in source
