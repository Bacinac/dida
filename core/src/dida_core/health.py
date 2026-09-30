"""An adapter's live connection status, answered over the bus as the UI's
fail-loud badge and journalled on the transitions that matter."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from dida_core.bus import Bus
    from dida_core.journal import JournalKind

log = logging.getLogger("dida.health")

#: Request/reply subject an adapter answers with its connection status. The UI
#: shows it as a fail-loud badge so a mis-typed broker/credential is visible
#: instead of a silent "no devices ever appear".
STATUS_SUBJECT_PREFIX = "dida.status."


def status_subject(adapter: str) -> str:
    return f"{STATUS_SUBJECT_PREFIX}{adapter}"


class StatusReporter:
    """An adapter's live connection status, answered over `dida.status.<name>`.

    An adapter that talks to an external backend (an MQTT broker, a gateway, a
    cloud API) sets its status at the lifecycle points — `idle` when nothing is
    configured yet, `connecting` while dialing, `ok` once connected, `error`
    with a human detail when it fails. The UI turns this into a coloured badge
    on the adapter card so onboarding fails loud instead of silently.

    States: ``idle`` | ``connecting`` | ``ok`` | ``error``.
    """

    #: State -> journal kind. `idle` and `connecting` are deliberately absent:
    #: they are transient boot states every adapter passes through, and recording
    #: them would bury the two transitions that actually matter in noise.
    _JOURNAL_KIND: ClassVar[dict[str, JournalKind]] = {"ok": "online", "error": "offline"}

    def __init__(self, adapter: str) -> None:
        self._adapter = adapter
        self._state = "idle"
        self._detail = ""
        self._since = time.time()
        self._bus: Bus | None = None

    def set(self, state: str, detail: str = "") -> None:
        changed = state != self._state
        if changed or detail != self._detail:
            self._since = time.time()
        self._state, self._detail = state, detail
        # Every adapter already routes its connection lifecycle through here, so
        # this one call site gives the whole fleet an online/offline journal —
        # transitions only, never a repeat of the state it is already in. Spawned
        # rather than awaited because `set()` is synchronous and called from
        # connection callbacks that must not block on the bus.
        kind = self._JOURNAL_KIND.get(state) if changed else None
        if kind and self._bus is not None:
            from home_core.tasks import spawn

            from dida_core.journal import emit_journal
            coro = emit_journal(
                self._bus, kind, source=f"adapter:{self._adapter}",
                severity="error" if state == "error" else "info",
                message=detail, data={"adapter": self._adapter},
            )
            try:
                spawn(coro, log=log, name=f"journal {kind} {self._adapter}")
            except RuntimeError:
                # No running loop — an adapter reporting status from a worker
                # thread or before the loop starts. The status itself is already
                # set; a diagnostic record must not turn that into a crash.
                coro.close()
                log.debug("status: no loop to journal %s for %s", kind, self._adapter)

    def idle(self, detail: str = "") -> None:
        self.set("idle", detail)

    def connecting(self, detail: str = "") -> None:
        self.set("connecting", detail)

    def ok(self, detail: str = "") -> None:
        self.set("ok", detail)

    def error(self, detail: str = "") -> None:
        self.set("error", detail)

    def snapshot(self) -> dict:
        return {"state": self._state, "detail": self._detail, "since": self._since}

    async def serve(self, bus: Bus) -> None:
        """Answer status requests on `dida.status.<adapter>` (request/reply), and
        from here on mirror every state TRANSITION into the event journal."""
        self._bus = bus

        async def _cb(msg) -> None:
            try:
                await msg.respond(json.dumps(self.snapshot()).encode())
            except Exception:
                log.debug("status: reply failed for %s", self._adapter, exc_info=True)

        await bus.nc.subscribe(status_subject(self._adapter), cb=_cb)
