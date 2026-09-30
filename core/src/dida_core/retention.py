"""Data-retention: translate the Postgres retention policy into ClickHouse TTLs.

Postgres (`retention_class` / `retention_capability` / `retention_override`) is
the single source of truth for how long each resolution tier is kept, per
capability class. This module reads it and generates `ALTER TABLE … MODIFY TTL`
per ClickHouse tier — so editing a number in the UI re-applies retention live,
with no migration and no rebuild.

`materialize_ttl_after_modify = 0`: MODIFY TTL otherwise rewrites every existing
part to apply the new expression immediately (expensive on a big table, and it
would run on every boot). With it off, ClickHouse applies TTL lazily on future
merges — the correct behaviour for a policy that is re-synced on each boot.
"""

from __future__ import annotations

import logging

log = logging.getLogger("dida.retention")

# (table, timestamp column, retention_class field) for each stored tier.
# command_history rides the raw tier: a command's audit row lives exactly as
# long as the raw state rows of the capability it acted on.
_TIERS = (
    ("state_history", "ts", "days_raw"),
    ("state_history_1h", "bucket", "days_1h"),
    ("state_history_1d", "bucket", "days_1d"),
    ("command_history", "ts", "days_raw"),
)
# Fallback if the 'default' class row is somehow absent.
_DEFAULT_DAYS = {"days_raw": 90, "days_1h": 365, "days_1d": 730}


def _q(s: object) -> str:
    # ClickHouse honours BOTH backslash escapes and doubled quotes inside string
    # literals, so escape the backslash first (else a lone '\' before the closing
    # quote would escape it and break — or smuggle into — the generated TTL DDL).
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def _ttl_expr(tscol, field, classes, caps, overrides, fallback_days) -> str:
    """Build a ClickHouse TTL expression: tscol + toIntervalDay(multiIf(...)).

    Order = per-entity overrides first, then per-class capability groups, then the
    'default' class as the else branch. multiIf short-circuits, so an override for
    one entity's capability wins over that capability's class rule."""

    def days_of(class_name: str) -> int:
        c = classes.get(class_name) or classes.get("default")
        return int(c[field]) if c is not None else fallback_days

    branches: list[tuple[str, int]] = []
    for o in overrides:
        branches.append(
            (f"(entity_id = {_q(o['entity_id'])} AND capability = {_q(o['capability'])})",
             days_of(o["class_name"]))
        )
    by_class: dict[str, list[str]] = {}
    for row in caps:
        by_class.setdefault(row["class_name"], []).append(row["capability"])
    for class_name, cap_list in sorted(by_class.items()):
        if class_name == "default":
            continue  # default is the else branch
        in_list = ", ".join(_q(c) for c in sorted(cap_list))
        branches.append((f"capability IN ({in_list})", days_of(class_name)))

    else_days = days_of("default")
    if not branches:
        return f"{tscol} + toIntervalDay({else_days})"
    args: list[str] = []
    for cond, d in branches:
        args += [cond, str(d)]
    args.append(str(else_days))
    return f"{tscol} + toIntervalDay(multiIf({', '.join(args)}))"


async def apply_retention(ch, pool) -> None:
    """Read the Postgres policy and set each ClickHouse tier's TTL to match.

    `ch` is a clickhouse-connect AsyncClient, `pool` an asyncpg pool. Per-table
    failures are logged and skipped (a rollup table may not exist yet on a very
    first boot) — retention is best-effort and self-heals on the next call."""
    classes = {
        r["name"]: r
        for r in await pool.fetch(
            "SELECT name, days_raw, days_1h, days_1d FROM retention_class"
        )
    }
    caps = await pool.fetch("SELECT capability, class_name FROM retention_capability")
    overrides = await pool.fetch(
        "SELECT entity_id, capability, class_name FROM retention_override"
    )
    for table, tscol, field in _TIERS:
        expr = _ttl_expr(tscol, field, classes, caps, overrides, _DEFAULT_DAYS[field])
        try:
            await ch.command(
                f"ALTER TABLE {table} MODIFY TTL {expr} "
                "SETTINGS materialize_ttl_after_modify = 0"
            )
            log.info("retention: %s TTL synced", table)
        except Exception as exc:
            log.warning("retention: %s TTL not applied (%s)", table, exc, exc_info=True)
