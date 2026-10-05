from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest
from dida_api import history_restore
from dida_api.history_restore import recover_history, restore_history
from dida_core import apply_ch_migrations, apply_migrations, ch_client, pg_pool
from dida_core.history_access import RECOVERY_KEY, HistoryBusyError, history_access
from dida_core.history_partitions import HistoryRollbackError
from dida_engine.history import CMD_COLUMNS, COLUMNS, HistoryWriter

TABLES = ("state_history", "state_history_1h", "state_history_1d", "command_history")


@pytest.fixture
async def storage(tmp_path):
    pool = await pg_pool(min_size=1, max_size=5)
    await apply_migrations(pool, "/w/db/migrations")
    await pool.execute("DELETE FROM app_settings WHERE key = $1", RECOVERY_KEY)
    client = await ch_client()
    await apply_ch_migrations(client, "/w/ch/migrations")
    for table in TABLES:
        await client.command(f"TRUNCATE TABLE {table}")
    now = datetime.now(UTC)

    async def seed(entity):
        await client.insert("state_history", [[now, entity, "temperature", "test", 10., None]],
                            column_names=COLUMNS, settings={"insert_deduplicate": 0})
        await client.insert("command_history", [[now, entity, "on_off", "turn_on", "test", ""]],
                            column_names=CMD_COLUMNS, settings={"insert_deduplicate": 0})

    await seed("restored")
    files = {}
    for table in TABLES:
        path = tmp_path / f"{table}.native"
        path.write_bytes(await client.raw_query(f"SELECT * FROM {table}", fmt="Native"))  # noqa: S608
        files[table] = str(path)
        await client.command(f"TRUNCATE TABLE {table}")
    await seed("previous")

    async def query(sql):
        return (await client.raw_query(sql, fmt="TSVRaw")).decode()

    async def insert(table, path):
        with open(path, "rb") as stream:
            await client.raw_insert(table, insert_block=stream.read(), fmt="Native")

    yield pool, client, query, insert, files
    await pool.execute("DELETE FROM app_settings WHERE key = $1", RECOVERY_KEY)
    tables = await client.query("SELECT name FROM system.tables WHERE database='dida' AND name LIKE 'history_restore_%'")
    for row in tables.result_rows:
        await client.command(f"DROP TABLE {row[0]} SYNC")
    await client.close()
    await pool.close()


async def entities(client):
    result = []
    for table in TABLES:
        rows = await client.query(f"SELECT DISTINCT entity_id FROM {table} ORDER BY entity_id")  # noqa: S608
        result.append([row[0] for row in rows.result_rows])
    return result


def writer(pool, client):
    history = HistoryWriter(pool)
    history._client = client
    history._schema_ready = True
    history._next_day_tz_check = float("inf")
    return history


async def test_restore_keeps_original_views_and_later_live_writes(storage):
    pool, client, query, insert, files = storage
    before = await query("SELECT name, uuid FROM system.tables WHERE database='dida' AND name LIKE 'state_history%' ORDER BY name")
    await restore_history(pool, query, insert, files, "dida")
    assert await entities(client) == [["restored"]] * 4
    assert await query("SELECT name, uuid FROM system.tables WHERE database='dida' AND name LIKE 'state_history%' ORDER BY name") == before
    history = writer(pool, client)
    history.enqueue("live", "temperature", "test", 20, time.time_ns())
    assert await history.flush()
    assert (await entities(client))[:3] == [["live", "restored"]] * 3
    assert not await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY)


async def test_maintenance_blocks_batches_and_commands_without_consuming_the_buffer(storage):
    pool, client, *_ = storage
    history = writer(pool, client)
    history.enqueue("live", "temperature", "test", 20, time.time_ns())
    async with history_access(pool, exclusive=True):
        assert await history.flush() is False
        assert history.stats()["buffered"] == 1
        with pytest.raises(HistoryBusyError):
            await history.insert_command("live", "on_off", "turn_on", "test", {}, time.time_ns())
    assert await history.flush()


async def test_partial_cutover_rolls_back_all_tiers_and_releases_writers(storage):
    pool, client, query, insert, files = storage
    failed = False

    async def failing(sql):
        nonlocal failed
        if not failed and sql.startswith("ALTER TABLE dida.state_history_1h REPLACE"):
            failed = True
            raise RuntimeError("cutover failed")
        return await query(sql)

    with pytest.raises(RuntimeError, match="cutover failed"):
        await restore_history(pool, failing, insert, files, "dida")
    assert await entities(client) == [["previous"]] * 4
    async with history_access(pool):
        pass
    assert not (await client.query("SELECT name FROM system.tables WHERE name LIKE 'history_restore_%'")).result_rows


async def test_failed_rollback_survives_the_request_and_has_explicit_recovery(storage):
    pool, client, query, insert, files = storage

    async def failing(sql):
        if sql.startswith("ALTER TABLE dida.state_history_1h REPLACE"):
            raise RuntimeError("cutover and rollback failed")
        return await query(sql)

    with pytest.raises(HistoryRollbackError):
        await restore_history(pool, failing, insert, files, "dida")
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY)
    assert json.loads(raw)["phase"] == "cutover"
    history = writer(pool, client)
    history.enqueue("live", "temperature", "test", 20, time.time_ns())
    assert await history.flush() is False
    assert history.stats()["buffered"] == 1
    assert await recover_history(pool, query, "dida", TABLES)
    assert await entities(client) == [["previous"]] * 4
    assert await history.flush()


async def test_cancelled_cutover_finishes_before_unlock_and_retains_recovery(storage):
    pool, client, query, insert, files = storage
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(sql):
        if sql.startswith("ALTER TABLE dida.state_history REPLACE"):
            entered.set()
            await release.wait()
        return await query(sql)

    task = asyncio.create_task(restore_history(pool, paused, insert, files, "dida"))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    history = writer(pool, client)
    history.enqueue("live", "temperature", "test", 20, time.time_ns())
    assert await history.flush() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY)
    assert await recover_history(pool, query, "dida", TABLES)
    assert await entities(client) == [["previous"]] * 4


async def test_failed_cleanup_recovers_without_reverting_completed_restore(storage):
    pool, client, query, insert, files = storage

    async def failing(sql):
        if sql.startswith("DROP TABLE"):
            raise RuntimeError("cleanup failed")
        return await query(sql)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await restore_history(pool, failing, insert, files, "dida")
    assert json.loads(await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY))["phase"] == "cleanup"
    assert await entities(client) == [["restored"]] * 4
    assert await recover_history(pool, query, "dida", TABLES)
    assert await entities(client) == [["restored"]] * 4


async def test_cancellation_after_recovery_record_retains_previous_partitions(storage, monkeypatch):
    pool, client, query, insert, files = storage
    recorded = asyncio.Event()
    original = history_restore._record

    async def pause_after_record(conn, record):
        await original(conn, record)
        recorded.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(history_restore, "_record", pause_after_record)
    task = asyncio.create_task(restore_history(pool, query, insert, files, "dida"))
    await asyncio.wait_for(recorded.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(history_restore, "_record", original)
    assert await recover_history(pool, query, "dida", TABLES)
    assert await entities(client) == [["previous"]] * 4


async def test_invalid_recovery_names_are_rejected_without_clickhouse_mutations(storage):
    pool, client, query, *_ = storage
    record = {"database": "dida", "phase": "cleanup",
              "previous": {"state_history": "history_restore_" + "a" * 32 + "_state_history_previous"},
              "staging": {"state_history": "state_history"}}
    await pool.execute("INSERT INTO app_settings (key, value) VALUES ($1, $2)", RECOVERY_KEY, json.dumps(record))
    with pytest.raises(ValueError, match="staging table"):
        await recover_history(pool, query, "dida", TABLES)
    assert await entities(client) == [["previous"]] * 4


@pytest.mark.parametrize("operation", ["schema", "timezone", "command"])
async def test_history_uses_the_locked_connection_with_a_single_connection_pool(storage, operation):
    _, client, *_ = storage
    pool = await pg_pool(min_size=1, max_size=1)
    try:
        history = writer(pool, client)
        if operation == "timezone":
            history._next_day_tz_check = 0
        else:
            history._schema_ready = False
        if operation == "command":
            await asyncio.wait_for(history.insert_command("live", "on_off", "turn_on", "test", {}, time.time_ns()), 10)
            result = await client.query("SELECT count() FROM command_history WHERE entity_id='live'")
            assert result.first_row == (1,)
        else:
            history.enqueue("live", "temperature", "test", 20, time.time_ns())
            assert await asyncio.wait_for(history.flush(), 10)
            assert history.stats()["buffered"] == 0
    finally:
        await pool.close()
