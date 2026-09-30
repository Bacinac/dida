"""Publish this service's log records onto the bus, so they outlive rotation.

The console handler STAYS. `docker logs` remains the live tail and the thing that
works when NATS is down — this is an addition, never a replacement, which is what
makes it safe to fail silently: a log line that cannot be published is still on
stdout.

The awkward part is threading. `logging` is synchronous and emits from whatever
thread called it (an executor, a signal handler, a library's own worker), while
the bus is asyncio and belongs to one loop. So `emit()` never touches the bus: it
formats, encodes, and drops the bytes into a bounded queue via a thread-safe
`call_soon_threadsafe`; a single publisher task owns the loop side. A full queue
DROPS, counted and reported — the alternative is a logging call that blocks a
service because its log sink is slow, which is a far worse failure than a gap in
a diagnostic table.
"""

from __future__ import annotations

import asyncio
import logging
import traceback

from dida_core.events import LogRecord, logs_subject

log = logging.getLogger("dida.logbus")

#: Bounded so a burst (a crash loop logging a traceback per second) cannot grow
#: without limit while ClickHouse is unreachable.
QUEUE_MAX = 5000

#: Never publish these — they would feed themselves. `dida.logbus` is this module
#: complaining about publishing; `nats` is the client whose failures are exactly
#: what breaks publishing in the first place.
MUTED_LOGGERS = ("dida.logbus", "nats")


def _format_exc(record: logging.LogRecord) -> str:
    return "".join(traceback.format_exception(*record.exc_info))[:8000]


class NatsLogHandler(logging.Handler):
    """A logging handler that mirrors records onto `dida.logs`."""

    def __init__(self, bus, service: str, *, level: int = logging.INFO,
                 mute: tuple[str, ...] = ()) -> None:
        super().__init__(level=level)
        self._bus = bus
        self._service = service
        self._subject = logs_subject(service)
        self._muted = MUTED_LOGGERS + tuple(mute)
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._dropped = 0
        self._published = 0
        self._task = self._loop.create_task(self._publisher())

    def stats(self) -> dict:
        return {"published": self._published, "dropped": self._dropped,
                "queued": self._queue.qsize()}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self._muted):
            return
        try:
            payload = self._bus.encode_log(LogRecord(
                ts_ns=int(record.created * 1e9),
                service=self._service,
                level=record.levelname,
                logger=record.name,
                message=record.getMessage()[:4000],
                # An adapter can tag a line with the device it is about by passing
                # `extra={"entity_id": ...}`; the timeline then shows the line next
                # to that device's events instead of only in a global list.
                entity_id=str(getattr(record, "entity_id", "") or "")[:512],
                exc=(_format_exc(record) if record.exc_info else ""),
            ))
        except Exception:  # noqa: BLE001
            return  # a record we cannot encode must not break the caller's logging
        try:
            self._loop.call_soon_threadsafe(self._offer, payload)
        except RuntimeError:
            # The loop is closed. This handler outlives it — nothing detaches it
            # from the root logger — so every line logged after shutdown used to
            # raise RuntimeError out of `logging`, into whatever happened to be
            # logging: an atexit hook, a __del__, the GC reporting an unclosed
            # aiohttp session (which this project has actually seen). A log call
            # must never be the thing that raises. Count it and move on.
            self._dropped += 1

    def _offer(self, payload: bytes) -> None:
        """Runs ON the loop thread (call_soon_threadsafe), so touching the queue
        without a lock is safe here and only here."""
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1

    async def _publisher(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._bus.publish_raw(self._subject, payload)
                self._published += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Silent by design: complaining here would log, which would emit,
                # which would fail again. The count is the signal, and stdout —
                # which never depended on this path — still has the line.
                self._dropped += 1

    def close(self) -> None:
        self._task.cancel()
        super().close()


def attach_log_bus(bus, service: str, *, level: int = logging.INFO,
                   mute: tuple[str, ...] = ()) -> NatsLogHandler | None:
    """Add the handler to the root logger. Returns None (and logs a warning) if it
    cannot be created, so a caller can wire this in unconditionally.

    `mute` adds to MUTED_LOGGERS. The SINK uses it: a log sink that consumes its
    own output amplifies — its "insert failed" ERROR is published, consumed, fails
    to insert, and logs again. Measured on the first boot before the table existed:
    92 rows of the journal complaining about the journal."""
    try:
        handler = NatsLogHandler(bus, service, level=level, mute=mute)
    except Exception as exc:
        log.warning("log bus unavailable (%s) — logs stay on stdout only", exc, exc_info=True)
        return None
    logging.getLogger().addHandler(handler)
    return handler

