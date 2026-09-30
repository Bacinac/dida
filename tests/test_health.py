"""Regression tests for the status reporter (dida_core.health): the per-adapter
connection badge answered over `dida.status.<name>`. The liveness file is
home_core.health and tested there.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_health.py"
"""
import json

from dida_core.health import STATUS_SUBJECT_PREFIX, StatusReporter, status_subject

# --- status_subject ---------------------------------------------------------

def test_status_subject_composes_prefix():
    assert status_subject("mqtt") == "dida.status.mqtt", "adapter name appended to the prefix"
    assert status_subject("mqtt").startswith(STATUS_SUBJECT_PREFIX), "uses the shared prefix"


# --- StatusReporter ---------------------------------------------------------

def test_status_reporter_starts_idle():
    snap = StatusReporter("mqtt").snapshot()
    assert snap["state"] == "idle", "a reporter is idle until the adapter reports otherwise"
    assert snap["detail"] == "", "no detail before anything happens"
    assert isinstance(snap["since"], float), "since is a wall-clock timestamp"


def test_status_reporter_lifecycle_transitions():
    reporter = StatusReporter("mqtt")
    reporter.connecting("dialing broker")
    snap = reporter.snapshot()
    assert snap["state"] == "connecting" and snap["detail"] == "dialing broker", \
        "connecting() sets state+detail"
    reporter.ok("connected")
    snap = reporter.snapshot()
    assert snap["state"] == "ok" and snap["detail"] == "connected", "ok() sets state+detail"
    reporter.error("auth rejected")
    snap = reporter.snapshot()
    assert snap["state"] == "error" and snap["detail"] == "auth rejected", "error() sets state+detail"


def test_status_reporter_since_only_moves_on_actual_change():
    reporter = StatusReporter("mqtt")
    reporter.ok("connected")
    since1 = reporter.snapshot()["since"]
    reporter.ok("connected")  # identical state+detail -> _since must NOT move
    since2 = reporter.snapshot()["since"]
    assert since2 == since1, "re-asserting the same state+detail does not reset 'since'"
    reporter.error("dropped")  # a real change advances 'since'
    since3 = reporter.snapshot()["since"]
    assert since3 >= since1, "a genuine state change moves 'since' forward"


class _FakeNC:
    """Captures the subscription so the test can invoke the reply callback."""

    def __init__(self):
        self.subject = None
        self.cb = None

    async def subscribe(self, subject, cb):
        self.subject = subject
        self.cb = cb


class _FakeBus:
    def __init__(self):
        self.nc = _FakeNC()


class _FakeMsg:
    def __init__(self):
        self.payload = None

    async def respond(self, data):
        self.payload = data


async def test_status_reporter_serve_replies_with_snapshot():
    reporter = StatusReporter("mqtt")
    reporter.ok("connected to broker")
    bus = _FakeBus()
    await reporter.serve(bus)
    assert bus.nc.subject == status_subject("mqtt"), "subscribes on dida.status.<adapter>"
    msg = _FakeMsg()
    await bus.nc.cb(msg)  # simulate a status request arriving
    data = json.loads(msg.payload.decode())
    assert data["state"] == "ok", "reply carries the current state"
    assert data["detail"] == "connected to broker", "reply carries the current detail"
    assert isinstance(data["since"], float), "reply carries the since timestamp"
