"""Dangling entity references — the failure this installation cannot currently see.

An `entity_id` is referenced from JSON the database cannot police: automation
definitions, scene states, area configs, schedule params, settings. There is no foreign key
anywhere except `current_state`. So when an entity stops existing — renamed in
zigbee2mqtt, deleted in the UI, an adapter retired — every rule that named it keeps
loading, keeps evaluating, and simply never fires again. Nothing is logged, no
container goes unhealthy, and the only symptom is that the house quietly stopped
doing something it used to do.

This finds them. It does NOT repair them: rewriting a reference is a decision (is
this the same device under a new name, or a different device?), and a repair that
guesses wrong is worse than a list that is read.

WHAT COUNTS AS KNOWN is the load-bearing part. Besides real entities it includes
computed helpers, virtual entities, and the api's own RELAY targets — `radio:tuner`
has no adapter and no registry row by design, and reporting it would have made the
first live run cry wolf about three perfectly working automations. A detector that
is wrong once is a detector nobody opens again.
"""

from __future__ import annotations

from dida_core import (
    AREA_REFERENCES,
    SETTING_REFERENCES,
    WALKED_COLUMNS,
    entity_references,
    path_references,
    setting_references,
)
from fastapi import APIRouter, Depends, Request

from dida_api import radio_tuner
from dida_api.auth import AuthUser, require_admin

router = APIRouter(tags=["system"])

# Entity ids the api answers for itself, with no adapter and no `entities` row.
# Derived from the module that implements them, never re-typed: a copied string
# here would keep passing after the relay was renamed, and start reporting a
# working automation as broken.
API_RELAYS = frozenset({radio_tuner.TUNER})

async def known_entity_ids(pool) -> set[str]:
    """Everything a reference is allowed to point at."""
    known = set(API_RELAYS)
    for table in ("entities", "computed_helpers", "virtual_entities"):
        rows = await pool.fetch(f"SELECT entity_id FROM {table}")  # noqa: S608
        known.update(r["entity_id"] for r in rows)
    return known


async def find_orphans(pool) -> list[dict]:
    """Every stored object that names an entity which no longer exists."""
    known = await known_entity_ids(pool)
    out: list[dict] = []

    def report(kind: str, id_, name: str, refs: set[str]) -> None:
        missing = sorted(r for r in refs if r not in known)
        if missing:
            out.append({"kind": kind, "id": id_, "name": name, "missing": missing})

    for table, column in WALKED_COLUMNS:
        for row in await pool.fetch(f"SELECT id, name, {column} AS body FROM {table}"):  # noqa: S608
            report(table, row["id"], row["name"], entity_references(row["body"]))

    for row in await pool.fetch(f"SELECT id, name, {', '.join(AREA_REFERENCES)} FROM areas"):  # noqa: S608
        report("areas", row["id"], row["name"], set().union(
            *(path_references(row[col], paths) for col, paths in AREA_REFERENCES.items())))

    for row in await pool.fetch(
            "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
            [key for key, paths in SETTING_REFERENCES.items() if paths]):
        report("settings", row["key"], row["key"], setting_references(row["key"], row["value"]))

    return out


@router.get("/system/orphans")
async def system_orphans(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Admin-only: it names every automation in the house and what it points at."""
    orphans = await find_orphans(request.app.state.pool)
    return {
        "orphans": orphans,
        "count": len(orphans),
        # Distinct missing ids, for the "one rename broke six rules" case: the list
        # above repeats the same id per rule, and the count that matters to whoever
        # has to fix it is how many THINGS went missing.
        "missing": sorted({m for o in orphans for m in o["missing"]}),
    }
