from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4
from zoneinfo import ZoneInfo

from history_partitions import (
    HistoryRollbackError,
    create_staging,
    cutover,
    snapshot_partitions,
)

TABLES = ("state_history", "state_history_1h", "state_history_1d")
ENERGY = "capability IN ('energy','power')"


def replace_history(query: Callable[[str], str], insert: Callable[[str, bytes], None],
                    hourly: bytes, raw: bytes,
                    pause_writer: Callable[[], AbstractContextManager]) -> None:
    if query("SELECT name FROM system.tables WHERE database='dida' AND name LIKE 'demo_refresh_%'").strip():
        raise HistoryRollbackError("Unresolved demo history transaction; recover its staging tables before refreshing")
    suffix = uuid4().hex
    staging = {table: f"demo_refresh_{suffix}_{table}" for table in TABLES}
    previous = {table: f"{temporary}_previous" for table, temporary in staging.items()}
    cleanup = True
    try:
        for table, temporary in staging.items():
            create_staging(query, table, temporary)
        if hourly:
            insert(staging["state_history_1h"], hourly)
        if raw:
            insert(staging["state_history"], raw)
        comment = query("SELECT comment FROM system.tables WHERE database='dida' "
                        "AND name='state_history_1d'").strip()
        timezone = comment.removeprefix("day_tz=") if comment.startswith("day_tz=") else "UTC"
        ZoneInfo(timezone)
        timezone = timezone.replace("'", "''")
        query(f"INSERT INTO dida.{staging['state_history_1d']} "  # noqa: S608
              f"SELECT toStartOfDay(bucket, '{timezone}') AS day, entity_id, capability, "
              "avgMergeState(avg_v), minMergeState(min_v), maxMergeState(max_v), "
              "argMinMergeState(first_v), argMaxMergeState(last_v), countMergeState(cnt) "
              f"FROM dida.{staging['state_history_1h']} GROUP BY entity_id, capability, day")
        with pause_writer():
            for table, temporary in previous.items():
                snapshot_partitions(query, table, temporary)
            for table, temporary in staging.items():
                query(f"INSERT INTO dida.{temporary} SELECT * FROM dida.{table} WHERE NOT ({ENERGY})")  # noqa: S608
            cutover(query, staging, previous)
    except HistoryRollbackError:
        cleanup = False
        raise
    finally:
        if cleanup:
            for temporary in (*staging.values(), *previous.values()):
                query(f"DROP TABLE IF EXISTS dida.{temporary} SYNC")
