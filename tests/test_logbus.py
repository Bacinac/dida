"""The application-log bus: the handler that mirrors stdout into ClickHouse.

Two properties carry the whole design, and both are about NOT hurting the
service that logs:

  * `emit()` runs on WHATEVER thread called logging — an executor, a signal
    handler, a library's worker — while the bus belongs to one asyncio loop. So
    emit must never touch the bus or block; it encodes and hands the bytes over.
  * A full queue DROPS. A logging call that stalls a service because its log sink
    is slow is a far worse failure than a gap in a diagnostic table, and stdout —
    which never depended on this path — still has every line.

Run inside the api image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src python -m pytest tests/test_logbus.py"
"""
from __future__ import annotations

import asyncio
import logging

import msgspec
import pytest
from dida_core.events import LogRecord
from dida_core.logbus import QUEUE_MAX, NatsLogHandler

_decode = msgspec.msgpack.Decoder(LogRecord).decode


class FakeBus:
    def __init__(self, explode: bool = False) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._explode = explode
        self._enc = msgspec.msgpack.Encoder()

    def encode_log(self, record: LogRecord) -> bytes:
        return self._enc.encode(record)

    async def publish_raw(self, subject: str, payload: bytes) -> None:
        if self._explode:
            raise ConnectionError("nats is down")
        self.published.append((subject, payload))


def _record(name="dida.engine", level=logging.WARNING, msg="broker gone", **kw):
    r = logging.LogRecord(name, level, "f.py", 1, msg, None, None)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


async def _settle(handler):
    """Give call_soon_threadsafe and the publisher task a turn."""
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_record_reaches_the_bus_on_the_logs_subject():
    bus = FakeBus()
    h = NatsLogHandler(bus, "engine")
    h.emit(_record())
    await _settle(h)
    (subject, payload), = bus.published
    assert subject == "dida.logs.engine", "a service's records ride its own subject"
    rec = _decode(payload)
    assert (rec.service, rec.level, rec.logger, rec.message) == \
        ("engine", "WARNING", "dida.engine", "broker gone")
    h.close()


@pytest.mark.asyncio
async def test_a_line_can_name_the_device_it_is_about():
    # `extra={"entity_id": ...}` is what lets a device's timeline show its own log
    # lines next to its events instead of only a global list.
    bus = FakeBus()
    h = NatsLogHandler(bus, "adapter:mqtt")
    h.emit(_record(entity_id="mqtt:kitchen_light"))
    await _settle(h)
    assert _decode(bus.published[0][1]).entity_id == "mqtt:kitchen_light"
    h.close()


@pytest.mark.asyncio
async def test_the_sinks_own_loggers_are_never_published():
    # dida.logbus complaining about publishing, and the NATS client whose failure
    # IS the reason publishing broke, would both feed themselves.
    bus = FakeBus()
    h = NatsLogHandler(bus, "engine")
    h.emit(_record(name="dida.logbus"))
    h.emit(_record(name="nats.aio.client"))
    await _settle(h)
    assert bus.published == []
    h.close()


@pytest.mark.asyncio
async def test_extra_mutes_break_the_sinks_own_feedback_loop():
    # The journal mutes `dida.bus`: those are its own insert failures, and putting
    # them on the stream it consumes amplifies (measured: 92 rows of the journal
    # complaining about the journal, on the first boot before the table existed).
    bus = FakeBus()
    h = NatsLogHandler(bus, "journal", mute=("dida.bus",))
    h.emit(_record(name="dida.bus"))
    h.emit(_record(name="dida.journal.service"))
    await _settle(h)
    assert [_decode(p).logger for _, p in bus.published] == ["dida.journal.service"]
    h.close()


@pytest.mark.asyncio
async def test_a_full_queue_drops_and_counts_rather_than_blocking():
    # THE contract. Blocking here would mean a service stalls because its log sink
    # is slow — worse than a gap in a diagnostic table.
    bus = FakeBus()
    h = NatsLogHandler(bus, "engine")
    h._task.cancel()  # stop the drain so the queue genuinely fills
    for _ in range(QUEUE_MAX + 50):
        h._offer(b"x")
    assert h._queue.qsize() == QUEUE_MAX
    assert h.stats()["dropped"] == 50, "every overflow is counted, never silent"
    h.close()


@pytest.mark.asyncio
async def test_a_dead_bus_never_reaches_the_caller():
    bus = FakeBus(explode=True)
    h = NatsLogHandler(bus, "engine")
    h.emit(_record())          # must not raise
    await _settle(h)
    assert h.stats()["dropped"] == 1
    assert h.stats()["published"] == 0
    h.close()


@pytest.mark.asyncio
async def test_an_exception_rides_along_as_a_formatted_traceback():
    bus = FakeBus()
    h = NatsLogHandler(bus, "engine")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        r = logging.LogRecord("dida.engine", logging.ERROR, "f.py", 1, "failed", None, sys.exc_info())
        h.emit(r)
    await _settle(h)
    rec = _decode(bus.published[0][1])
    assert "ValueError: boom" in rec.exc, "the traceback is what makes an ERROR row useful"
    h.close()


def test_a_log_line_after_the_loop_closed_does_not_raise():
    """The handler outlives the loop — nothing detaches it from the root logger.

    So a line logged after shutdown reached `call_soon_threadsafe` on a closed loop
    and raised RuntimeError out of `logging`, into whatever happened to be logging at
    the time: an atexit hook, a __del__, the GC reporting an unclosed aiohttp session
    (which this project has actually seen). A log call must never be the thing that
    raises — it is the one call every error path is already making.

    Not async: the point is the loop being GONE, which a running one cannot express."""
    loop = asyncio.new_event_loop()
    handler = loop.run_until_complete(_make_handler())
    # Cancel the publisher before closing: a task still pending on a closed loop is
    # collected later and reported as an unraisable exception, which turns a passing
    # test into a warning that outlives it and lands in an unrelated suite's output.
    handler._task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    handler.emit(_record(msg="after the end"))     # must not raise
    assert handler.stats()["dropped"] >= 1, "a dropped line must still be counted"


async def _make_handler() -> NatsLogHandler:
    """Built INSIDE a loop — the handler binds the running one at construction."""
    return NatsLogHandler(FakeBus(), "test")
