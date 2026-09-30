"""`FailureGate` — the status-truthfulness convention: an adapter whose backend
fails REPEATEDLY must show a red badge, not stay green with the last happy
detail. Count consecutive failures; flip `status.error` at the threshold;
recover to `ok` on the first success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dida_core.health import StatusReporter


class FailureGate:
    """Consecutive-failure counter driving a StatusReporter (fail-loud badges).

    `ok(detail)` resets the streak and reports ok; `fail(detail)` increments and
    flips the badge to error once `threshold` consecutive failures accumulate —
    so a single blip doesn't flap the badge, but a persistently dead backend
    can't hide behind a stale green.
    """

    def __init__(self, status: StatusReporter, *, threshold: int = 3) -> None:
        self._status = status
        self._threshold = threshold
        self._failures = 0

    @property
    def failing(self) -> bool:
        return self._failures >= self._threshold

    def ok(self, detail: str = "") -> None:
        self._failures = 0
        self._status.ok(detail)

    def reset(self) -> None:
        """Clear the failure streak WITHOUT touching the badge — for when the
        caller sets its own non-error status (e.g. an idle 'expected offline'
        that must not count as a fault)."""
        self._failures = 0

    def fail(self, detail: str = "") -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._status.error(detail)
