from __future__ import annotations

import ast
import contextlib
import fcntl
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import clickhouse_connect
import pytest
from dida_core import apply_ch_migrations, ch_client
from dida_core.day_rollup import apply_house_day

from scripts.demo_history import TABLES, HistoryRollbackError, replace_history


@pytest.fixture
async def history():
    schema = await ch_client()
    await apply_ch_migrations(schema, "/w/ch/migrations")
    await apply_house_day(schema, "Europe/Zagreb")
    await schema.close()
    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "dida"),
        password=os.environ["CLICKHOUSE_PASSWORD"], database=os.environ.get("CLICKHOUSE_DB", "dida"))
    for table in TABLES:
        client.command(f"TRUNCATE TABLE {table}")
    client.command("DROP TABLE IF EXISTS demo_source_raw SYNC")
    client.command("DROP TABLE IF EXISTS demo_source_hour SYNC")
    client.command("CREATE TABLE demo_source_raw AS state_history")
    client.command("CREATE TABLE demo_source_hour AS state_history_1h")
    cutoff = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    client.insert("demo_source_raw", [
        [cutoff - timedelta(days=7), "meter:old", "energy", "demo", 5.0, None],
        [cutoff - timedelta(days=2, minutes=40), "meter:one", "energy", "demo", 10.0, None],
        [cutoff - timedelta(days=2, minutes=20), "meter:one", "energy", "demo", 30.0, None],
        [cutoff - timedelta(days=1), "meter:one", "power", "demo", 200.0, None],
    ], column_names=["ts", "entity_id", "capability", "adapter", "value_num", "value_str"])
    client.command("INSERT INTO demo_source_hour SELECT toStartOfHour(ts) AS bucket, entity_id, capability, "
                   "avgState(assumeNotNull(value_num)), minState(assumeNotNull(value_num)), maxState(assumeNotNull(value_num)), "
                   "argMinState(assumeNotNull(value_num), ts), argMaxState(assumeNotNull(value_num), ts), countState() "
                   "FROM demo_source_raw GROUP BY entity_id, capability, bucket")
    client.insert("state_history", [
        [cutoff - timedelta(days=100), "obsolete", "energy", "demo", 1000.0, None],
        [cutoff - timedelta(days=100), "thermometer", "temperature", "demo", 22.0, None],
    ], column_names=["ts", "entity_id", "capability", "adapter", "value_num", "value_str"],
                  settings={"insert_deduplicate": 0})
    yield client, cutoff
    client.close()


def snapshots(client, cutoff, block_size=1):
    stamp = int(cutoff.timestamp())
    return (
        client.raw_query("SELECT * FROM demo_source_hour FORMAT Native", settings={"max_block_size": block_size}),
        client.raw_query(f"SELECT * FROM demo_source_raw WHERE ts >= toDateTime({stamp}) - INTERVAL 3 DAY FORMAT Native",  # noqa: S608
                         settings={"max_block_size": block_size}),
    )


def query(client):
    return lambda sql: client.raw_query(sql, fmt="TSVRaw").decode()


def insert(client):
    return lambda table, data: client.raw_insert(
        table, insert_block=data, fmt="Native",
        settings={"min_insert_block_size_rows": 1, "min_insert_block_size_bytes": 0})


def pause(events):
    @contextmanager
    def paused():
        events.append("stop")
        try:
            yield
        finally:
            events.append("start")
    return paused


def contents(client):
    result = [client.query("SELECT ts, entity_id, capability, value_num FROM state_history "
                           "ORDER BY entity_id, capability, ts").result_rows]
    for table in TABLES[1:]:
        result.append(client.query(
            f"SELECT bucket, entity_id, capability, countMerge(cnt), avgMerge(avg_v), minMerge(min_v), "  # noqa: S608
            f"maxMerge(max_v), argMinMerge(first_v), argMaxMerge(last_v) FROM {table} "
            "GROUP BY bucket, entity_id, capability ORDER BY entity_id, capability, bucket").result_rows)
    return result


def energy_counts(client):
    return [client.query("SELECT count() FROM state_history WHERE capability IN ('energy','power')").first_row[0]] + [
        client.query(f"SELECT countMerge(cnt) FROM {table} WHERE capability IN ('energy','power')").first_row[0]  # noqa: S608
        for table in TABLES[1:]]


def test_repeated_snapshot_replaces_all_three_tiers_and_preserves_other_capabilities(history):
    client, cutoff = history
    events = []
    args = snapshots(client, cutoff)
    replace_history(query(client), insert(client), *args, pause(events))
    first = contents(client)
    assert energy_counts(client) == [3, 4, 4]
    assert all(any(row[1] == "thermometer" for row in rows) for rows in first)
    assert all(not any(row[1] == "obsolete" for row in rows) for rows in first)
    replace_history(query(client), insert(client), *snapshots(client, cutoff, block_size=3), pause(events))
    assert contents(client) == first
    assert events == ["stop", "start", "stop", "start"]
    assert client.query("SELECT count() FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%'").first_row == (0,)


def test_partition_replacement_preserves_view_bindings_and_future_live_rollups(history):
    client, cutoff = history
    identity = client.query("SELECT name, uuid FROM system.tables WHERE database='dida' "
                            "AND name IN ('state_history','state_history_1h','state_history_1d') ORDER BY name").result_rows
    replace_history(query(client), insert(client), *snapshots(client, cutoff), pause([]))
    assert client.query("SELECT name, uuid FROM system.tables WHERE database='dida' "
                        "AND name IN ('state_history','state_history_1h','state_history_1d') ORDER BY name").result_rows == identity
    client.insert("state_history", [[cutoff, "meter:one", "energy", "live", 50.0, None]],
                  column_names=["ts", "entity_id", "capability", "adapter", "value_num", "value_str"])
    assert energy_counts(client) == [4, 5, 5]


def test_an_empty_source_clears_energy_without_discarding_other_capabilities(history):
    client, _ = history
    replace_history(query(client), insert(client), b"", b"", pause([]))
    assert energy_counts(client) == [0, 0, 0]
    assert all(len(rows) == 1 and rows[0][1] == "thermometer" for rows in contents(client))


def test_failed_import_leaves_the_previous_history_and_writer_untouched(history):
    client, cutoff = history
    previous = contents(client)
    events = []

    def fail_raw(table, data):
        if table.endswith("_state_history"):
            raise RuntimeError("broken native import")
        insert(client)(table, data)

    with pytest.raises(RuntimeError, match="broken native"):
        replace_history(query(client), fail_raw, *snapshots(client, cutoff), pause(events))
    assert contents(client) == previous
    assert events == []
    assert client.query("SELECT count() FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%'").first_row == (0,)


def test_a_cutover_error_resumes_the_writer_and_cleans_staging(history):
    client, cutoff = history
    events = []
    previous = contents(client)
    failed = False

    def fail_cutover(sql):
        nonlocal failed
        if "ALTER TABLE dida.state_history_1h REPLACE PARTITION" in sql and not failed:
            failed = True
            raise RuntimeError("cutover failed")
        return query(client)(sql)

    with pytest.raises(RuntimeError, match="cutover failed"):
        replace_history(fail_cutover, insert(client), *snapshots(client, cutoff), pause(events))
    assert events == ["stop", "start"]
    assert contents(client) == previous
    assert client.query("SELECT count() FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%'").first_row == (0,)


def test_copy_history_exports_a_bounded_snapshot_and_is_stable_across_native_block_shapes(history, tmp_path):
    client, cutoff = history
    exports = []
    events = []
    block_size = 1

    def export(sql):
        exports.append(sql)
        if sql.startswith("SELECT toUnixTimestamp"):
            return str(int(cutoff.timestamp())).encode()
        sql = sql.replace("dida.state_history_1h", "dida.demo_source_hour")
        sql = sql.replace("dida.state_history", "dida.demo_source_raw")
        return client.raw_query(sql, settings={"max_block_size": block_size})

    source = ast.parse((Path(__file__).parents[1] / "scripts/refresh-demo-data.py").read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "copy_history")
    (tmp_path / "state").mkdir()
    namespace = {"Path": Path, "fcntl": fcntl, "__file__": str(tmp_path / "scripts/refresh-demo-data.py"),
                 "_prod_history": export, "_dev_history": query(client), "_insert_history": insert(client),
                 "_pause_history_writer": pause(events), "replace_history": replace_history, "dev_psql": lambda sql: None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "copy_history", "exec"), namespace)
    namespace["copy_history"]()
    first = contents(client)
    block_size = 3
    namespace["copy_history"]()
    assert contents(client) == first
    assert energy_counts(client) == [3, 4, 4]
    assert events == ["stop", "start", "stop", "start"]
    assert sum("INTERVAL 8 DAY" in sql for sql in exports) == 2
    assert sum("INTERVAL 3 DAY" in sql for sql in exports) == 2
    assert sum(f"< toDateTime({int(cutoff.timestamp())})" in sql for sql in exports) == 4


def test_failed_rollback_keeps_recovery_partitions_and_blocks_another_refresh(history):
    client, cutoff = history

    def fail_all_replacements(sql):
        if "REPLACE PARTITION" in sql:
            raise RuntimeError("storage unavailable")
        return query(client)(sql)

    try:
        with pytest.raises(HistoryRollbackError, match="recovery tables must be retained"):
            replace_history(fail_all_replacements, insert(client), *snapshots(client, cutoff), pause([]))
        assert client.query("SELECT count() FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%_previous'").first_row == (3,)
        with pytest.raises(HistoryRollbackError, match="Unresolved"):
            replace_history(query(client), insert(client), *snapshots(client, cutoff), pause([]))
    finally:
        names = client.query("SELECT name FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%'").result_rows
        for (name,) in names:
            client.command(f"DROP TABLE {name} SYNC")


def test_real_writer_pause_does_not_restart_an_engine_whose_history_rollback_failed():
    commands = []

    def command(args):
        commands.append(args)
        return "true" if args[1] == "inspect" else ""

    source = ast.parse((Path(__file__).parents[1] / "scripts/refresh-demo-data.py").read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_pause_history_writer")
    namespace = {"contextlib": contextlib, "sh": command, "HistoryRollbackError": HistoryRollbackError}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "pause_writer", "exec"), namespace)
    with pytest.raises(HistoryRollbackError), namespace["_pause_history_writer"]():
        raise HistoryRollbackError("recovery required")
    assert [args[1] for args in commands] == ["inspect", "stop"]
