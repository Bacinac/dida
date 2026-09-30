"""Emitting side of device reachability (the "is this thing actually there" signal).

An adapter calls `set_reachable()` whenever its verdict on a device CHANGES —
connected/disconnected, availability online/offline, a cloud online flag flipping.
Like the journal, it is best-effort: a service must never fail its real work
because a status ping could not go out, so everything here is swallowed and
logged.

Edge-triggered by design. Adapters hold a tiny per-device cache and only publish
on a change, so a node that stays up does not reprint "reachable" every poll — the
engine's row would churn and the timeline would fill with noise. The helper does
NOT keep that cache (an adapter restart should re-announce the current truth); the
caller owns it, which is one line and keeps this module stateless.
"""

from __future__ import annotations

import asyncio
import logging
import time

from dida_core.events import ReachabilityEvent

log = logging.getLogger("dida.reachability")

_MAX_DETAIL = 200

# Every verdict this process has issued, so it can be said again. Core NATS carries
# reachability — "the latest verdict wins and a missed one is corrected by the next"
# — and that sentence was false for an edge-triggered producer: an adapter publishes
# only on CHANGE, so there IS no next one. On 29.08 the denon adapter connected to
# the Marantz seven seconds before the engine had subscribed, its "reachable" went
# to nobody, and the device stayed marked down for 54 minutes with a live telnet
# session open and an alert firing. Every deploy did this to every device whose
# adapter reconnected first. Re-asserting makes the sentence true.
_LAST: dict[tuple[str, str], tuple[bool, str]] = {}
# Inside the alert's 300 s hold, so a lost verdict is corrected BEFORE it can fire a
# false alarm rather than after. The engine writes the row unconditionally but stamps
# `reachable_since` and journals only on a change, so a repeat costs one small write
# and changes nothing anybody reads.
REASSERT_S = 60


async def set_reachable(
    bus, device_key: str, adapter: str, reachable: bool, *, detail: str = ""
) -> None:
    """Publish that `device_key` is (un)reachable, as `adapter` sees it. Never raises."""
    if not device_key or not adapter:
        # A verdict with no subject is a caller bug, not a runtime condition — log
        # it where that caller's own logs are, and drop it rather than write a row
        # keyed on "".
        log.warning("reachability: ignoring verdict with empty device_key/adapter "
                    "(device=%r adapter=%r)", device_key, adapter)
        return
    detail = ("" if reachable else (detail or ""))[:_MAX_DETAIL]
    _LAST[(device_key, adapter)] = (bool(reachable), detail)
    try:
        await bus.publish_reachability(ReachabilityEvent(
            ts_ns=time.time_ns(), device_key=device_key, adapter=adapter,
            reachable=bool(reachable), detail=detail,
        ))
    except Exception as exc:
        log.warning("reachability publish failed (device=%s reachable=%s): %s",
                    device_key, reachable, exc, exc_info=True)


def forget_reachable(device_key: str, adapter: str) -> None:
    """Stop re-asserting a device this adapter no longer has (removed, or its
    location dropped). Without it the loop would keep insisting on a verdict about
    something that is gone."""
    _LAST.pop((device_key, adapter), None)


async def reassert_loop(bus, period: float = REASSERT_S) -> None:
    """Say every verdict again, forever. Never raises: a status ping that could not
    go out must not take the adapter down with it."""
    while True:
        await asyncio.sleep(period)
        for (device_key, adapter), (reachable, detail) in list(_LAST.items()):
            try:
                await bus.publish_reachability(ReachabilityEvent(
                    ts_ns=time.time_ns(), device_key=device_key, adapter=adapter,
                    reachable=reachable, detail=detail,
                ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("reachability re-assert failed (device=%s): %s", device_key, exc, exc_info=True)
