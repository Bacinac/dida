from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from dida_core.history_access import RECOVERY_KEY, HistoryRecoveryRequiredError, history_access
from dida_core.history_partitions import (
    HistoryRollbackError,
    create_staging,
    cutover,
    replace_partitions,
    snapshot_partitions,
)


async def _work(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _query_bridge(query):
    loop = asyncio.get_running_loop()
    return lambda sql: asyncio.run_coroutine_threadsafe(query(sql), loop).result()


async def _record(conn, record: dict) -> None:
    await conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", RECOVERY_KEY, json.dumps(record)
    )


async def _cleanup(conn, query, record: dict) -> None:
    for table in (*record["staging"].values(), *record["previous"].values()):
        await query(f"DROP TABLE IF EXISTS {record['database']}.{table} SYNC")
    await conn.execute("DELETE FROM app_settings WHERE key = $1", RECOVERY_KEY)


async def restore_history(pool, query, insert, files: dict[str, str], database: str) -> None:
    if await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY):
        raise HistoryRecoveryRequiredError("Recover the previous history restore before starting another")
    suffix = uuid4().hex
    record = {
        "database": database,
        "phase": "cutover",
        "staging": {table: f"history_restore_{suffix}_{table}" for table in files},
        "previous": {table: f"history_restore_{suffix}_{table}_previous" for table in files},
    }
    sync_query = _query_bridge(query)
    recorded = False
    try:
        for table, temporary in record["staging"].items():
            await _work(create_staging, sync_query, table, temporary, database)
            await insert(temporary, files[table])
        async with history_access(pool, exclusive=True) as conn:
            for table, temporary in record["previous"].items():
                await _work(snapshot_partitions, sync_query, table, temporary, database)
            recorded = True
            await _record(conn, record)
            try:
                await _work(cutover, sync_query, record["staging"], record["previous"], database)
            except (HistoryRollbackError, asyncio.CancelledError):
                raise
            except Exception:
                record["phase"] = "cleanup"
                await _record(conn, record)
                await _cleanup(conn, query, record)
                raise
            record["phase"] = "cleanup"
            await _record(conn, record)
            await _cleanup(conn, query, record)
    finally:
        if not recorded:
            for table in (*record["staging"].values(), *record["previous"].values()):
                await query(f"DROP TABLE IF EXISTS {database}.{table} SYNC")


async def recover_history(pool, query, database: str, tables: tuple[str, ...]) -> bool:
    async with history_access(pool, exclusive=True, recovery=True) as conn:
        raw = await conn.fetchval("SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY)
        if not raw:
            return False
        record = json.loads(raw)
        if record["database"] != database or record["phase"] not in ("cutover", "cleanup"):
            raise ValueError("History recovery does not match the configured database")
        if not record["previous"] or not set(record["previous"]) <= set(tables) or set(record["staging"]) != set(record["previous"]):
            raise ValueError("Invalid history recovery table set")
        for table, temporary in record["previous"].items():
            if not re.fullmatch(rf"history_restore_[0-9a-f]{{32}}_{re.escape(table)}_previous", temporary):
                raise ValueError("Invalid history recovery table name")
            if record["staging"][table] != temporary.removesuffix("_previous"):
                raise ValueError("Invalid history recovery staging table")
        if record["phase"] == "cutover":
            sync_query = _query_bridge(query)
            for table, temporary in record["previous"].items():
                await _work(replace_partitions, sync_query, table, temporary, database)
            record["phase"] = "cleanup"
            await _record(conn, record)
        await _cleanup(conn, query, record)
        return True
