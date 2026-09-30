"""A crashed CV pipeline must not be reported as an unreachable one.

The failure this prevents is a wrong investigation, not a wrong response: every
planvision fault used to read "planvision unreachable", which points at the network
and the container list while an image OpenCV choked on sits in a log nobody opened.
"""

from __future__ import annotations

import httpx
import pytest
from dida_api.floors import _planvision_error


def _status_error(code: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://planvision:8094/borders")
    response = httpx.Response(code, text=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_a_500_from_planvision_is_reported_as_failed_not_unreachable():
    err = _planvision_error(_status_error(500, "cv2.error: (-215:Assertion failed)"))
    assert err.status_code == 502
    assert "unreachable" not in err.detail
    assert "failed (500)" in err.detail


def test_the_pipelines_own_message_survives_into_the_api_response():
    """Without the body the operator learns only that something returned 500."""
    err = _planvision_error(_status_error(500, "cv2.error: (-215:Assertion failed) !empty()"))
    assert "cv2.error" in err.detail


def test_a_transport_failure_still_reads_unreachable():
    request = httpx.Request("POST", "http://planvision:8094/borders")
    err = _planvision_error(httpx.ConnectError("nodename nor servname", request=request))
    assert err.status_code == 502
    assert "unreachable" in err.detail


def test_a_long_pipeline_traceback_is_truncated():
    """A full traceback would otherwise land whole in an HTTP error detail."""
    err = _planvision_error(_status_error(500, "x" * 5000))
    assert len(err.detail) < 300


@pytest.mark.parametrize("code", [400, 413, 415, 500, 502])
def test_every_error_status_is_named_in_the_detail(code):
    err = _planvision_error(_status_error(code, "nope"))
    assert f"({code})" in err.detail


def test_the_planvision_log_switch_has_not_drifted_from_core():
    """planvision reimplements DIDA_LOG_LEVEL locally — it cannot import core (its own
    base carries no msgspec, so `import dida_core` fails there). Two copies of one
    switch that disagree are worse than one copy, so they are compared HERE, in the one
    image where both are importable."""
    from dida_core.logsetup import FORMAT as CORE_FORMAT
    from dida_core.logsetup import LEVELS as CORE_LEVELS
    from dida_planvision.logsetup import FORMAT, LEVELS

    assert LEVELS == CORE_LEVELS
    assert FORMAT == CORE_FORMAT
