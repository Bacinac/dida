from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4
from zoneinfo import ZoneInfo

TABLES = ("state_history", "state_history_1h", "state_history_1d")
ENERGY = "capability IN ('energy','power')"


class HistoryRollbackError(RuntimeError):
    pass


def _create_staging(query: Callable[[str], str], table: str, temporary: str) -> None:
    query(f"CREATE TABLE dida.{temporary} AS dida.{table}")
    if re.search(r"\bTTL\b", query(f"SHOW CREATE TABLE dida.{temporary}")):
        query(f"ALTER TABLE dida.{temporary} REMOVE TTL")


def _partitions(query: Callable[[str], str], table: str) -> set[str]:
    rows = query("SELECT DISTINCT partition_id FROM system.parts "  # noqa: S608
                 f"WHERE database='dida' AND table='{table}' AND active")
    partitions = set(rows.split())
    if any(not re.fullmatch(r"\d{6}", part) for part in partitions):
        raise ValueError(f"unexpected history partition in {table}")
    return partitions


def _replace_partitions(query: Callable[[str], str], table: str, staging: str) -> None:
    old = _partitions(query, table)
    new = _partitions(query, staging)
    for part in sorted(new):
        query(f"ALTER TABLE dida.{table} REPLACE PARTITION ID '{part}' FROM dida.{staging}")
    for part in sorted(old - new):
        query(f"ALTER TABLE dida.{table} DROP PARTITION ID '{part}'")


def _cutover(query: Callable[[str], str], staging: dict[str, str], previous: dict[str, str]) -> None:
    try:
        for table, temporary in staging.items():
            _replace_partitions(query, table, temporary)
    except Exception:
        try:
            for table, temporary in previous.items():
                _replace_partitions(query, table, temporary)
        except Exception as exc:
            raise HistoryRollbackError(
                f"History rollback failed; engine must remain stopped. Recovery tables: {previous}") from exc
        raise


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
            _create_staging(query, table, temporary)
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
                _create_staging(query, table, temporary)
                for part in sorted(_partitions(query, table)):
                    query(f"ALTER TABLE dida.{temporary} ATTACH PARTITION ID '{part}' FROM dida.{table}")
            for table, temporary in staging.items():
                query(f"INSERT INTO dida.{temporary} SELECT * FROM dida.{table} WHERE NOT ({ENERGY})")  # noqa: S608
            _cutover(query, staging, previous)
    except HistoryRollbackError:
        cleanup = False
        raise
    finally:
        if cleanup:
            for temporary in (*staging.values(), *previous.values()):
                query(f"DROP TABLE IF EXISTS dida.{temporary} SYNC")
