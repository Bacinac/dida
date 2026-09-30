"""WebSocket fan-out: one bus subscription, N browsers.

Lifted out of app.py, which had grown to 1 122 lines by accumulating everything
that had nowhere else to go. This is the part with the clearest boundary — it
owns connected clients and nothing else, and it is the piece whose backpressure
behaviour is worth reading on its own rather than hunting for between route
handlers.

Behaviour is unchanged; this is a move, not a rewrite.
"""

from __future__ import annotations

import asyncio
import json
import logging

from dida_core import JournalEvent, StateUpdate
from fastapi import WebSocket

log = logging.getLogger("dida.api.hub")


class _Client:
    """One connected browser: its socket, its hidden-entity set, and a bounded
    outbound queue drained by a dedicated writer task. The writer is the SOLE
    sender on the socket (pongs are enqueued too), so there is never a concurrent
    send — which Starlette websockets don't allow."""

    __slots__ = ("hidden", "queue", "task", "ws")

    def __init__(self, ws: WebSocket, hidden: set[str]) -> None:
        self.ws = ws
        self.hidden = hidden
        # Bounded: a client that can't keep up fills this and gets dropped (below),
        # rather than growing without limit. 256 events is generous for a UI.
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self.task: asyncio.Task | None = None


class Hub:
    """Fans the engine events stream out to connected WebSocket clients.

    One bus subscription, N browsers. Each browser has its OWN bounded queue +
    writer task, so a slow/stalled client can never delay the fan-out to the
    others or block the bus callback: on_event only ever does a non-blocking
    put_nowait, and a client whose queue is full is dropped (it resyncs with a
    full snapshot on reconnect). This is the real backpressure isolation the old
    sequential `await ws.send_text` per client only claimed.
    """

    def __init__(self) -> None:
        self._clients: dict[WebSocket, _Client] = {}

    def add(self, ws: WebSocket, hidden: set[str]) -> None:
        client = _Client(ws, hidden)
        client.task = asyncio.create_task(self._writer(client))
        self._clients[ws] = client

    def remove(self, ws: WebSocket) -> None:
        client = self._clients.pop(ws, None)
        if client is not None and client.task is not None:
            client.task.cancel()

    def enqueue(self, ws: WebSocket, payload: str) -> None:
        """Queue a raw payload (e.g. a pong) to one client via its writer, so the
        writer stays the only sender. Drops the client if its queue is full."""
        client = self._clients.get(ws)
        if client is None:
            return
        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.remove(ws)

    async def _writer(self, client: _Client) -> None:
        """Drain one client's queue to its socket. The only place send_text runs."""
        try:
            while True:
                payload = await client.queue.get()
                await client.ws.send_text(payload)
        except (asyncio.CancelledError, Exception):
            log.debug("ws client dropped", exc_info=True)
            self.remove(client.ws)

    async def on_event(self, update: StateUpdate) -> None:
        if not self._clients:
            return
        payload = json.dumps(
            {
                "entity_id": update.entity_id,
                "capability": update.capability,
                "value": update.value,
                "unit": update.unit,
                "ts_ns": update.ts_ns,
                "device": update.device,
            }
        )
        for ws, client in list(self._clients.items()):
            if update.entity_id in client.hidden:
                continue  # this connection's user may not see this entity
            try:
                client.queue.put_nowait(payload)  # never blocks the fan-out
            except asyncio.QueueFull:
                # This client can't keep up — drop it rather than stall everyone.
                # The frontend store does a full snapshot resync on reconnect.
                self.remove(ws)

    async def on_journal(self, event: JournalEvent) -> None:
        """Fan a device event out live, so an open timeline shows an adapter
        dropping off as it happens rather than on the next open. Tagged with a
        `type` (state deltas carry none) so the store can tell them apart, and
        hidden-entity filtering is the SAME rule as state — an event names the
        entity it happened to, and a user who may not see the device may not see
        why it went quiet either."""
        if not self._clients:
            return
        payload = json.dumps({
            "type": "event",
            "ms": event.ts_ns // 1_000_000,
            "entity_id": event.entity_id,
            "device_key": event.device_key,
            "source": event.source,
            "kind": event.kind,
            "severity": event.severity,
            "message": event.message,
        })
        for ws, client in list(self._clients.items()):
            if event.entity_id and event.entity_id in client.hidden:
                continue
            try:
                client.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.remove(ws)


