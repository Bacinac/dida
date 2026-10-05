from __future__ import annotations

import json

from dida_core.references import (
    AREA_REFERENCES,
    SETTING_REFERENCES,
    WALKED_COLUMNS,
    entity_references,
    path_references,
    rewrite_paths,
    rewrite_references,
    rewrite_setting,
    setting_references,
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def rewrite_entity_references(conn, mapping: dict[str, str]) -> int:
    moved = 0
    for table, column in WALKED_COLUMNS:
        for row in await conn.fetch(
            f"SELECT id, {column} AS body FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
        ):
            body = _json(row["body"])
            if not (entity_references(body) & mapping.keys()):
                continue
            await conn.execute(
                f"UPDATE {table} SET {column} = $2::text::jsonb WHERE id = $1",  # noqa: S608
                row["id"], json.dumps(rewrite_references(body, mapping)))
            moved += 1
    for row in await conn.fetch(f"SELECT id, {', '.join(AREA_REFERENCES)} FROM areas"):  # noqa: S608
        for column, paths in AREA_REFERENCES.items():
            body = _json(row[column])
            if not (path_references(body, paths) & mapping.keys()):
                continue
            await conn.execute(
                f"UPDATE areas SET {column} = $2::text::jsonb WHERE id = $1",  # noqa: S608
                row["id"], json.dumps(rewrite_paths(body, paths, mapping)))
            moved += 1
    for row in await conn.fetch("SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
                                [key for key, paths in SETTING_REFERENCES.items() if paths]):
        if not (setting_references(row["key"], row["value"]) & mapping.keys()):
            continue
        await conn.execute("UPDATE app_settings SET value = $2, updated_at = now() WHERE key = $1",
                           row["key"], rewrite_setting(row["key"], row["value"], mapping))
        moved += 1
    for old, new in mapping.items():
        await conn.execute(
            "DELETE FROM user_access_rules old USING user_access_rules new "
            "WHERE old.user_id = new.user_id AND old.kind = new.kind AND old.scope = 'entity' "
            "AND new.scope = 'entity' AND old.ref = $1 AND new.ref = $2", old, new)
        moved += len(await conn.fetch(
            "UPDATE user_access_rules SET ref = $2 WHERE scope = 'entity' AND ref = $1 RETURNING ref", old, new))
    return moved
