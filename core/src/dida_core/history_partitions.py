from __future__ import annotations

import re
from collections.abc import Callable


class HistoryRollbackError(RuntimeError):
    pass


def _name(database: str, table: str) -> str:
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in (database, table)):
        raise ValueError("Invalid history database or table name")
    return f"{database}.{table}"


def create_staging(query: Callable[[str], str], table: str, temporary: str, database: str = "dida") -> None:
    target, source = _name(database, temporary), _name(database, table)
    query(f"CREATE TABLE {target} AS {source}")
    if re.search(r"\bTTL\b", query(f"SHOW CREATE TABLE {target}")):
        query(f"ALTER TABLE {target} REMOVE TTL")


def partitions(query: Callable[[str], str], table: str, database: str = "dida") -> set[str]:
    _name(database, table)
    rows = query("SELECT DISTINCT partition_id FROM system.parts "  # noqa: S608
                 f"WHERE database='{database}' AND table='{table}' AND active")
    parts = set(rows.split())
    if any(not re.fullmatch(r"\d{6}", part) for part in parts):
        raise ValueError(f"Unexpected history partition in {table}")
    return parts


def replace_partitions(query: Callable[[str], str], table: str, staging: str, database: str = "dida") -> None:
    target, source = _name(database, table), _name(database, staging)
    old, new = partitions(query, table, database), partitions(query, staging, database)
    for part in sorted(new):
        query(f"ALTER TABLE {target} REPLACE PARTITION ID '{part}' FROM {source}")
    for part in sorted(old - new):
        query(f"ALTER TABLE {target} DROP PARTITION ID '{part}'")


def snapshot_partitions(query: Callable[[str], str], table: str, temporary: str, database: str = "dida") -> None:
    create_staging(query, table, temporary, database)
    for part in sorted(partitions(query, table, database)):
        query(f"ALTER TABLE {_name(database, temporary)} ATTACH PARTITION ID '{part}' FROM {_name(database, table)}")


def cutover(query: Callable[[str], str], staging: dict[str, str], previous: dict[str, str],
            database: str = "dida") -> None:
    try:
        for table, temporary in staging.items():
            replace_partitions(query, table, temporary, database)
    except Exception:
        try:
            for table, temporary in previous.items():
                replace_partitions(query, table, temporary, database)
        except Exception as exc:
            raise HistoryRollbackError(f"History rollback failed; recovery tables must be retained: {previous}") from exc
        raise
