#!/usr/bin/env python3
"""Import a Firebase service-account key into app_settings (Fernet-encrypted),
so the notify adapter can send FCM v1 push. ONE credential per Firebase project,
shared by every DIDA install → it must live in each environment's DB (dev + prod
have separate databases), so run this against each.

    cat android/keystore/firebase-sa.json | \
        docker compose exec -T api python /app/scripts/set_fcm_sa.py

Reads the JSON from stdin; encrypts with DIDA_SECRET_KEY; upserts the
`fcm_service_account` setting. Idempotent — re-running replaces it (key rotation).
"""
import asyncio
import json
import os
import sys

from dida_core import encrypt_secret, pg_pool


async def main() -> None:
    raw = sys.stdin.read()
    sa = json.loads(raw)  # fail loud on garbage
    if sa.get("type") != "service_account" or "private_key" not in sa:
        sys.exit("not a service-account key (expected type=service_account with private_key)")
    enc = encrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), json.dumps(sa))
    pool = await pg_pool()
    try:
        await pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ('fcm_service_account', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            enc,
        )
    finally:
        await pool.close()
    print(f"fcm_service_account stored for project {sa.get('project_id')}")


if __name__ == "__main__":
    asyncio.run(main())
