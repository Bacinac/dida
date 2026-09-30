"""First-boot seeding: the environment's one and only say.

An installation's own values — the LAN address renderers reach it on, the public
origin, the NIC the VLANs hang off, the service credentials — live in
`app_settings` and are edited in Settings. The environment is how a fresh install
STARTS with sensible ones (install.sh detects the LAN address, the operator pastes
a key), and that is where its involvement ends: these rows are written once, never
overwritten, and nothing reads the variables again.

That distinction matters more than it looks. A value read from both places has no
answer to "which one is true" — the cutover that moved DIDA to a new host left an
.env pointing at the old one, and the half of the system that preferred it cast
audio at a machine that no longer existed. One source, one answer.
"""

from __future__ import annotations

import logging
import os

from dida_core import encrypt_secret, seed_app_setting

log = logging.getLogger("dida.api.seed")

# setting key -> environment variable it is seeded from.
PLAIN = {
    "lan_ip": "DIDA_LAN_IP",
    "opus_url": "DIDA_OPUS_URL",
    "app_url": "DIDA_APP_URL",
    "public_url": "DIDA_PUBLIC_URL",
    "net_parent": "DIDA_NET_PARENT",
    "announce_lang": "DIDA_ANNOUNCE_LANG",
}
# Same, for values stored Fernet-encrypted (the shape stored_api_key reads back).
SECRET = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "opus_token": "DIDA_OPUS_TOKEN",
}
# A value every installation must have a row for, even on a host that declared
# nothing: netmgr would otherwise build "<empty>.20" as an interface name.
DEFAULTS = {"net_parent": "ens18"}


async def seed_from_env(pool) -> None:
    """Copy whatever the environment declared into `app_settings`, once."""
    seeded: list[str] = []
    for key, env in PLAIN.items():
        value = os.environ.get(env, "").strip() or DEFAULTS.get(key, "")
        if value and await _write(pool, key, value):
            seeded.append(key)
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    for key, env in SECRET.items():
        value = os.environ.get(env, "").strip()
        if not (value and secret):
            continue
        if await _write(pool, key, encrypt_secret(secret, value)):
            seeded.append(key)
    if seeded:
        log.info("seeded from the environment (first boot, not read again): %s",
                 ", ".join(sorted(seeded)))


async def _write(pool, key: str, value: str) -> bool:
    before = await pool.fetchval("SELECT 1 FROM app_settings WHERE key = $1", key)
    await seed_app_setting(pool, key, value)
    return before is None
