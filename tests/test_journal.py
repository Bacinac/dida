"""The device-event journal: emit side (dida_core.journal) + sink (dida_journal).

Two contracts are worth pinning down, and they pull in OPPOSITE directions —
which is the whole point of testing them together:

  * `emit_journal` must NEVER raise. It is diagnostic; a dead bus must not undo
    the caller's real work. So it swallows, logs, and returns.
  * `JournalWriter.write` must ALWAYS raise on failure. It sits under a durable
    JetStream consumer whose ack means "the row is on disk" — swallowing here
    would ack a message whose row was never written, which is exactly the silent
    loss the command audit was rewritten to eliminate.

Run inside the api image (dida_core + clickhouse-connect installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "PYTHONPATH=/w/core/src:/w/services/journal/src python -m pytest tests/test_journal.py"
"""
from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from dida_core.events import JOURNAL_SUBJECT, JournalEvent, journal_subject
from dida_core.health import StatusReporter
from dida_core.journal import _MAX_DATA, _MAX_MESSAGE, SEVERITIES, JournalKind, emit_journal
from dida_journal.__main__ import COLUMNS, JournalWriter


class FakeBus:
    def __init__(self, explode: bool = False) -> None:
        self.published: list[JournalEvent] = []
        self._explode = explode

    async def publish_journal(self, event: JournalEvent) -> None:
        if self._explode:
            raise ConnectionError("nats is down")
        self.published.append(event)


# --- emit side ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_builds_the_event_and_serialises_data():
    bus = FakeBus()
    await emit_journal(
        bus, "automation_fired", entity_id="mqtt:kitchen_light", device_key="mqtt:kitchen",
        source="automation", severity="notice", message="Motion → light",
        data={"automation_id": 7, "value": True},
    )
    (event,) = bus.published
    assert event.kind == "automation_fired"
    assert event.entity_id == "mqtt:kitchen_light"
    assert event.device_key == "mqtt:kitchen"
    assert event.source == "automation"
    assert event.severity == "notice"
    assert json.loads(event.data) == {"automation_id": 7, "value": True}, \
        "data is carried as a JSON string, not a dict — msgspec struct field is String"
    assert event.ts_ns > 0, "the event is stamped at emit time"


@pytest.mark.asyncio
async def test_emit_omits_data_when_there_is_none():
    bus = FakeBus()
    await emit_journal(bus, "offline", entity_id="mqtt:x", source="engine")
    assert bus.published[0].data == "", "no detail → empty string, not the JSON 'null'"


@pytest.mark.asyncio
async def test_emit_downgrades_an_unknown_severity_instead_of_failing():
    # A typo must not become a ClickHouse value the timeline can't style — but it
    # also must not lose the event. Recorded as info, complaint goes to the log.
    bus = FakeBus()
    await emit_journal(bus, "reconnect", severity="URGENT!!", source="adapter:mqtt")
    assert bus.published[0].severity == "info"
    assert "URGENT!!" not in bus.published[0].severity


@pytest.mark.parametrize("severity", sorted(SEVERITIES))
@pytest.mark.asyncio
async def test_emit_passes_every_known_severity_through(severity: str):
    bus = FakeBus()
    await emit_journal(bus, "online", severity=severity)
    assert bus.published[0].severity == severity


@pytest.mark.asyncio
async def test_emit_truncates_a_runaway_message_and_payload():
    bus = FakeBus()
    await emit_journal(bus, "automation_error", message="x" * 50_000,
                       data={"traceback": "y" * 50_000})
    event = bus.published[0]
    assert len(event.message) == _MAX_MESSAGE, "a huge exception string is bounded"
    assert len(event.data) == _MAX_DATA, "so is its JSON detail"


@pytest.mark.asyncio
async def test_emit_never_raises_when_the_bus_is_down():
    # THE contract. A caller writes `await emit_journal(...)` in the middle of its
    # real work; if this propagated, a NATS blip would fail the work itself.
    bus = FakeBus(explode=True)
    await emit_journal(bus, "offline", entity_id="mqtt:x")  # must not raise
    assert bus.published == []


@pytest.mark.asyncio
async def test_emit_never_raises_on_unserialisable_detail():
    bus = FakeBus()
    await emit_journal(bus, "command_failed", data={"conn": object()})
    # default=str makes it serialisable rather than losing the event entirely.
    assert bus.published, "an exotic detail value is stringified, not dropped"


# --- sink side ---------------------------------------------------------------

class FakeCH:
    def __init__(self, explode: bool = False) -> None:
        self.rows: list[tuple[str, list, list]] = []
        self.closed = False
        self._explode = explode

    async def insert(self, table, rows, column_names):
        if self._explode:
            raise ConnectionError("clickhouse is down")
        self.rows.append((table, rows, column_names))

    async def close(self):
        self.closed = True


def _writer(client) -> JournalWriter:
    w = JournalWriter()
    w._client = client
    return w


@pytest.mark.asyncio
async def test_write_maps_the_event_onto_the_columns_in_order():
    ch = FakeCH()
    w = _writer(ch)
    await w.write(JournalEvent(
        ts_ns=1_700_000_000_500_000_000, kind="offline", entity_id="mqtt:x",
        device_key="mqtt:dev", source="engine", severity="warning",
        message="no report for 10 min", data='{"last_seen":"14:02"}',
    ))
    table, (row,), columns = ch.rows[0]
    assert table == "device_events"
    assert columns == COLUMNS
    assert dict(zip(columns, row, strict=True)) == {
        "ts": datetime.fromtimestamp(1_700_000_000.5, tz=UTC),
        "entity_id": "mqtt:x", "device_key": "mqtt:dev", "source": "engine",
        "kind": "offline", "severity": "warning",
        "message": "no report for 10 min", "data": '{"last_seen":"14:02"}',
    }, "row positions must line up with COLUMNS — a silent shift would mislabel every event"


@pytest.mark.asyncio
async def test_write_raises_and_drops_the_client_so_the_consumer_naks():
    # The opposite of emit: here swallowing would ack an unwritten row.
    ch = FakeCH(explode=True)
    w = _writer(ch)
    with pytest.raises(ConnectionError):
        await w.write(JournalEvent(ts_ns=1, kind="offline"))
    assert ch.closed, "the failed client is closed so the redelivery reconnects"
    assert w._client is None
    assert w.stats()["failed"] == 1


@pytest.mark.asyncio
async def test_write_counts_only_what_landed():
    ch = FakeCH()
    w = _writer(ch)
    await w.write(JournalEvent(ts_ns=1, kind="online"))
    await w.write(JournalEvent(ts_ns=2, kind="online"))
    assert w.stats()["written"] == 2
    assert w.stats()["failed"] == 0


# --- schema ------------------------------------------------------------------

def test_migration_matches_the_columns_the_writer_inserts():
    # The writer names its columns explicitly, so a column renamed in the DDL
    # without touching COLUMNS would only fail at runtime, on the first event.
    sql = Path(__file__).resolve().parents[1].joinpath("ch/migrations/0006_device_events.sql").read_text()
    body = sql.split("CREATE TABLE IF NOT EXISTS device_events (", 1)[1].split(") ENGINE", 1)[0]
    declared = [line.strip().split()[0] for line in body.strip().splitlines() if line.strip()]
    assert declared == COLUMNS, "device_events columns and JournalWriter.COLUMNS must agree"


def test_migration_sets_the_dedup_window():
    # Redelivery after an ambiguous insert must not duplicate a timeline entry.
    sql = Path(__file__).resolve().parents[1].joinpath("ch/migrations/0006_device_events.sql").read_text()
    assert "non_replicated_deduplication_window = 100" in sql


def test_journal_subject_is_its_own_stream_not_the_live_event_republish():
    # dida.events is the live state republish (every accepted update); putting
    # discrete events on it would drown them and couple two unrelated firehoses.
    from dida_core.events import ENGINE_EVENTS_SUBJECT
    assert journal_subject("engine") == f"{JOURNAL_SUBJECT}.engine"
    assert not journal_subject("engine").startswith(ENGINE_EVENTS_SUBJECT)


# --- adapter online/offline via StatusReporter --------------------------------

@pytest.mark.asyncio
async def test_status_reporter_journals_only_transitions():
    # StatusReporter.set is the ONE call site every adapter's connection
    # lifecycle already goes through, which is why the fleet-wide online/offline
    # journal hangs off it. It must fire on a CHANGE and stay silent otherwise —
    # an adapter that re-asserts "ok" every poll would otherwise flood the table.
    from dida_core.health import StatusReporter

    bus = FakeBus()
    rep = StatusReporter("mqtt")
    rep._bus = bus  # what serve(bus) does

    rep.connecting("dialing")
    rep.ok("connected to broker")
    rep.ok("connected to broker")          # same state, same detail
    rep.ok("connected to broker (2 nodes)")  # same state, NEW detail
    await asyncio.sleep(0)  # let the spawned emit tasks run
    await asyncio.sleep(0)

    kinds = [e.kind for e in bus.published]
    assert kinds == ["online"], f"one transition, one row — got {kinds}"
    assert bus.published[0].source == "adapter:mqtt"
    assert bus.published[0].severity == "info"


@pytest.mark.asyncio
async def test_status_reporter_journals_an_error_as_offline():
    from dida_core.health import StatusReporter

    bus = FakeBus()
    rep = StatusReporter("shelly")
    rep._bus = bus
    rep.ok()
    rep.error("auth rejected")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [e.kind for e in bus.published] == ["online", "offline"]
    offline = bus.published[-1]
    assert offline.severity == "error", "a broken adapter is not an info-level fact"
    assert offline.message == "auth rejected"


@pytest.mark.asyncio
async def test_status_reporter_without_a_bus_is_silent_not_broken():
    # An adapter that never called serve() (or a unit test) still sets status.
    from dida_core.health import StatusReporter

    rep = StatusReporter("virtual")
    rep.ok()          # must not raise
    rep.error("x")
    assert rep.snapshot()["state"] == "error"


def test_status_reporter_off_loop_records_the_state_and_skips_the_journal():
    # Deliberately NOT async: some adapters report status from a worker thread or
    # before the loop is up. spawn() needs a running loop, so without the guard
    # this would raise RuntimeError out of a plain status update — a diagnostic
    # turning a working adapter into a crash.
    from dida_core.health import StatusReporter

    rep = StatusReporter("govee")
    rep._bus = FakeBus()
    rep.ok("2 devices")          # must not raise
    assert rep.snapshot()["state"] == "ok"
    assert rep._bus.published == [], "nothing journalled without a loop to run it on"


@pytest.mark.asyncio
async def test_emit_coerces_none_fields_so_the_consumer_can_decode_them():
    # A caller passes fields straight off a StateUpdate, where `device` is None for
    # an ungrouped entity. msgspec encodes that None into a str-typed field happily
    # and blows up only on DECODE — at the consumer, which terminates it as poison
    # and silently drops the event. Measured on the live engine, with a fully green
    # unit suite behind it, so the round trip is what this asserts.
    import msgspec

    bus = FakeBus()
    await emit_journal(bus, "validation_rejected", entity_id="mqtt:x",
                       device_key=None, source=None, message=None)
    (event,) = bus.published
    assert (event.device_key, event.source, event.message) == ("", "", "")
    round_tripped = msgspec.msgpack.Decoder(JournalEvent).decode(
        msgspec.msgpack.Encoder().encode(event))
    assert round_tripped.kind == "validation_rejected", \
        "the event must survive the encode/decode the bus actually performs"



# --- app logs (the third firehose) -------------------------------------------

@pytest.mark.asyncio
async def test_logs_are_written_as_ONE_batch_not_row_by_row():
    # A ClickHouse insert costs the same round trip for 1 row as for 500, and logs
    # are the high-volume firehose — per-row here would turn one flush into
    # hundreds. The batch is also what the dedup window can drop on redelivery.
    from dida_core.events import LogRecord
    from dida_journal.__main__ import LOG_COLUMNS

    ch = FakeCH()
    w = _writer(ch)
    await w.write_logs([
        LogRecord(ts_ns=1_700_000_000_000_000_000, service="engine", level="INFO",
                  logger="dida.engine", message="alive"),
        LogRecord(ts_ns=1_700_000_001_000_000_000, service="api", level="ERROR",
                  logger="dida.api", message="boom", exc="Traceback…"),
    ])
    assert len(ch.rows) == 1, "one insert, not one per line"
    table, rows, columns = ch.rows[0]
    assert table == "app_logs" and columns == LOG_COLUMNS and len(rows) == 2
    assert dict(zip(columns, rows[1], strict=True))["exc"] == "Traceback…"
    assert w.stats()["written"] == 2


@pytest.mark.asyncio
async def test_an_empty_batch_does_not_touch_clickhouse():
    ch = FakeCH()
    await _writer(ch).write_logs([])
    assert ch.rows == []


@pytest.mark.asyncio
async def test_a_failed_log_batch_raises_so_the_whole_batch_redelivers():
    from dida_core.events import LogRecord

    ch = FakeCH(explode=True)
    w = _writer(ch)
    with pytest.raises(ConnectionError):
        await w.write_logs([LogRecord(ts_ns=1, service="engine", level="INFO",
                                      logger="dida.engine", message="x")])
    assert ch.closed and w._client is None
    assert w.stats()["failed"] == 1


def test_app_logs_migration_matches_the_columns_the_writer_inserts():
    from dida_journal.__main__ import LOG_COLUMNS

    sql = Path(__file__).resolve().parents[1].joinpath("ch/migrations/0007_app_logs.sql").read_text()
    body = sql.split("CREATE TABLE IF NOT EXISTS app_logs (", 1)[1].split(") ENGINE", 1)[0]
    declared = [line.strip().split()[0] for line in body.strip().splitlines() if line.strip()]
    assert declared == LOG_COLUMNS


def test_app_logs_expire_sooner_than_device_events():
    # Deliberate: logs are high volume and their value decays fast, while "the
    # bridge went offline" is still worth something next season.
    root = Path(__file__).resolve().parents[1] / "ch/migrations"
    assert "INTERVAL 30 DAY" in (root / "0007_app_logs.sql").read_text()
    assert "INTERVAL 180 DAY" in (root / "0006_device_events.sql").read_text()


@pytest.mark.asyncio
async def test_two_consumers_racing_to_connect_build_exactly_one_client():
    """The writer is shared by the event consumer and the log-batch consumer.

    Without a lock both see `_client is None`, both build a client, and the loser
    is overwritten and never closed — aiohttp reports "Unclosed client session"
    from the GC and the orphaned session's insert queue shuts down under whichever
    consumer still held a reference. That is not hypothetical: it is what the
    production log viewer showed on this feature's first run (27 leaked sessions).
    """
    import dida_journal.__main__ as mod

    built = []

    async def slow_client():
        await asyncio.sleep(0)      # a real connect yields; that is the whole window
        c = FakeCH()
        built.append(c)
        return c

    w = JournalWriter()
    orig, mod.ch_client = mod.ch_client, slow_client
    try:
        a, b = await asyncio.gather(w._ensure(), w._ensure())
    finally:
        mod.ch_client = orig
    assert len(built) == 1, f"one connect, not {len(built)} — the loser would leak"
    assert a is b is w._client


# --- vocabulary ----------------------------------------------------------------
# The timeline names every kind from the UI catalogue; one written outside
# JournalKind shows there as its raw key. Nothing type-checks the emitters, so
# this reads them: `command_fail` and `command_failed` both reached ClickHouse,
# and six kinds had no words at all.

ROOT = Path(__file__).resolve().parent.parent
EMITTERS = [ROOT / "core" / "src", *ROOT.glob("services/*/src"), *ROOT.glob("adapters/*/src")]
# Where the kind is a variable, and the constant that bounds it.
PASSED_ON = {
    ("dida_engine/__main__.py", "_journal"),
    ("dida_core/health.py", "set"),
    ("dida_adapter_baba/adapter.py", "_on_place"),
}


def _journal_calls():
    for src in EMITTERS:
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", getattr(node.func, "attr", None))
                    at = {"emit_journal": 1, "_journal": 0}.get(name)
                    if at is not None and len(node.args) > at:
                        yield str(path.relative_to(src)), fn, node.lineno, node.args[at]


def _assigned(fn, name: str) -> list[ast.expr]:
    """Every value the function gives `name`, unpacked assignments included."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                out.append(node.value)
            elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                out += [v for t, v in zip(target.elts, node.value.elts, strict=True)
                        if isinstance(t, ast.Name) and t.id == name]
    return out


def test_every_emitted_kind_is_in_the_vocabulary():

    kinds = set(get_args(JournalKind))
    seen = 0
    for where, fn, line, arg in _journal_calls():
        seen += 1
        said = _assigned(fn, arg.id) if isinstance(arg, ast.Name) else [arg]
        said = [x for one in said for x in ([one.body, one.orelse] if isinstance(one, ast.IfExp) else [one])]
        if isinstance(arg, ast.Name) and not (said and all(isinstance(one, ast.Constant) for one in said)):
            assert (where, fn.name) in PASSED_ON, f"{where}:{line} passes a kind nothing here bounds"
            continue
        for one in said:
            assert isinstance(one, ast.Constant), f"{where}:{line} builds its kind"
            assert one.value in kinds, f"{where}:{line} writes {one.value!r}, not in JournalKind"
    assert seen > 15


def test_the_bounded_kinds_are_in_the_vocabulary():

    kinds = set(get_args(JournalKind))
    assert set(StatusReporter._JOURNAL_KIND.values()) <= kinds
    baba = ast.parse((ROOT / "adapters/baba/src/dida_adapter_baba/adapter.py").read_text())
    place = next(n.value for n in baba.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "PLACE_KINDS")
    assert {c.value for c in place.elts} <= kinds
