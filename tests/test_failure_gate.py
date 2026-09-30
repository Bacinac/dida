"""Regression tests for FailureGate (dida_core.tasks) — the consecutive-failure
counter every adapter uses to drive its fail-loud status badge.

The robustness claim (audit flagged it as untested): a single blip must NOT flap
the badge red, but a persistently dead backend must NOT hide behind a stale green.
Pure logic over a stub StatusReporter — no infra.

    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_failure_gate.py"
"""
from dida_core import FailureGate


class StubStatus:
    def __init__(self):
        self.calls = []

    def ok(self, detail=""):
        self.calls.append(("ok", detail))

    def error(self, detail=""):
        self.calls.append(("error", detail))


def test_threshold_suppresses_single_blips():
    s = StubStatus()
    g = FailureGate(s, threshold=3)
    g.fail("blip")
    assert not g.failing and s.calls == [], "one failure below threshold: no error badge, not failing"
    g.fail("blip")
    assert not g.failing and s.calls == [], "two failures still below threshold 3"
    g.fail("dead")
    assert g.failing and s.calls == [("error", "dead")], "third consecutive failure flips the badge to error"


def test_ok_resets_the_streak():
    s = StubStatus()
    g = FailureGate(s, threshold=3)
    g.fail()
    g.fail()
    g.ok("recovered")
    assert not g.failing, "ok() clears the failure streak"
    assert s.calls[-1] == ("ok", "recovered"), "ok() reports the recovery on the badge"
    g.fail()
    assert not g.failing, "after ok(), a single fresh failure is not failing again"


def test_flapping_backend_never_shows_hard_error():
    # fail,fail,ok,fail,fail — never 3 CONSECUTIVE failures, so the badge must
    # never go error (the whole point of the gate: suppress flaps).
    s = StubStatus()
    g = FailureGate(s, threshold=3)
    g.fail()
    g.fail()
    g.ok()
    g.fail()
    g.fail()
    assert not g.failing, "interleaved oks keep the consecutive streak below threshold"
    assert not any(c[0] == "error" for c in s.calls), "a flapping backend never trips a hard error badge"


def test_reset_clears_streak_without_touching_badge():
    # reset() is for a caller that sets its own non-error status (e.g. 'expected
    # offline') — it clears the streak but must NOT emit ok/error itself.
    s = StubStatus()
    g = FailureGate(s, threshold=3)
    g.fail()
    g.fail()
    g.reset()
    assert not g.failing, "reset clears the streak"
    assert s.calls == [], "reset does NOT touch the badge (no ok/error emitted)"
    g.fail()
    assert not g.failing, "after reset, a single failure is not failing"
