from __future__ import annotations

import json

from dida_core import decrypt_secret
from dida_core.entity_identity import rewrite_entity_references
from dida_core.frigate_sites import camera_key, parse_sites, site_key


async def migrate_sites(pool, secret: str) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", 0x44494441465249)
        raw = await conn.fetchval("SELECT value FROM adapter_config WHERE adapter = 'frigate' AND key = 'sites' FOR UPDATE")
        if raw is None:
            return {"entities": 0, "references": 0}
        sites = parse_sites(decrypt_secret(secret, raw, adapter="frigate", key="sites", raise_on_error=True))
        if not sites:
            return {"entities": 0, "references": 0}
        roots = await conn.fetch(
            "SELECT e.entity_id, e.device_key, d.site, s.value AS descriptor FROM entities e "
            "LEFT JOIN devices d ON d.device_key = e.device_key "
            "LEFT JOIN current_state s ON s.entity_id = e.entity_id AND s.capability = 'camera' "
            "WHERE e.adapter = 'frigate' AND e.entity_id ~ '^frigate:[^:]+$' ORDER BY e.entity_id")
        mapping, devices = {}, {}
        for root in roots:
            old = root["entity_id"]
            descriptor = root["descriptor"]
            if isinstance(descriptor, str):
                descriptor = json.loads(descriptor)
            descriptor = descriptor or {}
            owners = [site for site in sites if str(descriptor.get("snapshot", "")).startswith(site["url"].rstrip("/") + "/")]
            label = root["site"] or descriptor.get("site")
            if len(owners) != 1:
                owners = [site for site in sites if label and site_key(site["name"]) == site_key(label)]
            if not owners and not label and len(sites) == 1:
                owners = sites
            if len(owners) != 1:
                raise ValueError(f"Frigate camera {old} has no unambiguous site; assign its device site before migration")
            name = owners[0]["name"]
            new = f"frigate:{camera_key(name, old.split(':', 1)[1])}"
            for row in await conn.fetch(
                "SELECT entity_id FROM entities WHERE adapter = 'frigate' "
                "AND (entity_id = $1 OR starts_with(entity_id, $2)) FOR UPDATE", old, old + ":"):
                mapping[row["entity_id"]] = new + row["entity_id"][len(old):]
            if root["device_key"]:
                devices[root["device_key"]] = new
        if not mapping:
            return {"entities": 0, "references": 0}
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM entities WHERE entity_id = ANY($1::text[]))", list(mapping.values())):
            raise ValueError("Frigate site migration collides with an existing entity")
        for old, new in devices.items():
            if await conn.fetchval("SELECT 1 FROM devices WHERE device_key = $1", new):
                raise ValueError(f"Frigate site migration collides with device {new}")
            await conn.execute("UPDATE devices SET device_key = $2 WHERE device_key = $1", old, new)
            await conn.execute("UPDATE entities SET device_key = $2 WHERE device_key = $1 AND adapter = 'frigate'", old, new)
        for old, new in mapping.items():
            await conn.execute("UPDATE entities SET entity_id = $2 WHERE entity_id = $1", old, new)
        moved = await rewrite_entity_references(conn, mapping)
        return {"entities": len(mapping), "references": moved}
