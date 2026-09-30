"""Emitting side of the device-event journal (PROPOSAL-LOGS-HISTORY §3.2).

The journal is DIAGNOSTIC. That single fact sets its whole contract: a service
must never fail, block or slow its real work because an event could not be
recorded. So `emit()` swallows everything — a dead bus, an unencodable payload —
and logs it rather than propagating. The read side is durable (JetStream +
ack-after-insert); the write side is deliberately best-effort.

The consumer (the `journal` service) is on the other end of `dida.journal`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from dida_core.events import JournalEvent

log = logging.getLogger("dida.journal")

# The closed set the timeline styles and filters on. Validated HERE, at emit time,
# where a typo is the emitting service's own bug and shows up in its own log — not
# in ClickHouse, where a bad value would fail an INSERT the durable consumer must
# then nak and eventually dead-letter.
SEVERITIES = frozenset({"debug", "info", "notice", "warning", "error"})

# What the device timeline names. A kind outside this set reaches the timeline as
# its raw key, so a new one is added here and given its words in the UI catalogue.
JournalKind = Literal[
    "automation_error", "automation_fired", "automation_stale", "breaker_trip",
    "command_failed", "device_left", "device_renamed", "dhcp_lease", "manual_override",
    "manual_released", "offline", "online", "pairing", "pairing_failed", "removed", "restored",
    "validation_rejected", "vehicle_arrived", "vehicle_left", "vlan_down", "vlan_up",
]

_MAX_MESSAGE = 2000   # a runaway exception string must not become a 10 MB row
_MAX_DATA = 8000


async def emit_journal(
    bus,
    kind: JournalKind,
    *,
    entity_id: str | None = "",
    device_key: str | None = "",
    source: str | None = "",
    severity: str = "info",
    message: str | None = "",
    data: dict | None = None,
) -> None:
    """Record that something happened. Never raises — see the module docstring."""
    if severity not in SEVERITIES:
        log.warning("journal: unknown severity %r for kind=%s — recording as info", severity, kind)
        severity = "info"
    try:
        payload = json.dumps(data, ensure_ascii=False, default=str) if data else ""
        # None → "". Callers pass fields straight off a StateUpdate, where `device`
        # is legitimately None for an ungrouped entity. msgspec ENCODES that None
        # into a str-typed field without complaint and only fails on DECODE, at the
        # consumer — which terminates it as a poison frame and drops the event.
        # Measured: the engine's first live validation_rejected was lost exactly
        # this way while every unit test passed.
        event = JournalEvent(
            ts_ns=time.time_ns(),
            kind=kind,
            entity_id=entity_id or "",
            device_key=device_key or "",
            source=source or "",
            severity=severity,
            message=(message or "")[:_MAX_MESSAGE],
            data=payload[:_MAX_DATA],
        )
        await bus.publish_journal(event)
    except Exception as exc:
        # Loud but non-fatal: the caller's actual job already succeeded (or failed
        # on its own terms) and must not be undone by the journal.
        log.warning("journal emit failed (kind=%s entity=%s): %s", kind, entity_id, exc, exc_info=True)
