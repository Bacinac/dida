"""Shared pytest fixtures for the DIDA suite."""
import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Reset the process-wide rate-limiters before EVERY test.

    pytest runs a whole group's tests in ONE process, and the limiters are
    module-level singletons. Without a reset, one integration suite's logins drain
    the token bucket and a LATER suite's login gets a spurious 429 — a
    shared-singleton ordering trap the old per-file `python tests/x.py` runner
    never hit. ASSISTANT_LIMITER is in the same list for the same reason.
    Imported lazily: only the api image has dida_api; in the automation/engine/
    adapter images it's a harmless no-op.
    """
    try:
        from dida_api.rate_limit import ASSISTANT_LIMITER, LOGIN

        LOGIN.clear()
        ASSISTANT_LIMITER.clear()
    except ImportError:
        pass
    yield


class FakeBroker:
    """Stands in for an adapter's `dida_core.broker.Broker`. Each op answers from
    `answers` — a value, an exception to raise, or a callable given the call's
    arguments — and every call is recorded in `calls` as (op, args)."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op, **args):
        self.calls.append((op, args))
        answer = self.answers.get(op)
        if isinstance(answer, Exception):
            raise answer
        return answer(**args) if callable(answer) else answer

    def asked(self, op) -> list[dict]:
        return [args for o, args in self.calls if o == op]
