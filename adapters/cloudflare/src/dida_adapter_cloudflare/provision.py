"""Tunnel provisioning — a NEW installation mints its own tunnel from the UI.

Until now the tunnel identity (credentials JSON + config.yml) had to be created
by hand with `cloudflared tunnel create` and copied into the state dir; enabling
the connectors without it crash-looped them forever. With the owner's OWN
Cloudflare API token stored as this adapter's secret, DIDA does the whole thing:
create the tunnel on THEIR account, write the credentials file, render the
initial config.yml and bring the connectors up. DNS rides the same token: every
route in tunnel mode gets its CNAME to <tunnel-id>.cfargotunnel.com, so a route
added in the UI actually resolves — no dashboard round-trip.

The pure parts (name validation, secret, credentials shape) live here untangled
from I/O so the rules are testable without an account.
"""

from __future__ import annotations

import base64
import json
import re
import secrets

_API = "https://api.cloudflare.com/client/v4"
_NAME_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def validate_tunnel_name(name: str) -> str | None:
    """The reason a tunnel name is unusable, or None. Cloudflare accepts more,
    but a lowercase-dns-label name is what every tool downstream agrees on."""
    if not _NAME_RE.fullmatch(name or ""):
        return "name must be a lowercase DNS label (a-z, 0-9, hyphens)"
    return None


def new_tunnel_secret() -> str:
    """32 random bytes, base64 — the shape `cloudflared tunnel create` uses."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def credentials_json(account_id: str, tunnel_id: str, secret: str) -> str:
    """The credentials file a locally-managed connector authenticates with —
    exactly the fields cloudflared writes, nothing more."""
    return json.dumps({
        "AccountTag": account_id,
        "TunnelSecret": secret,
        "TunnelID": tunnel_id,
    })


class CfError(RuntimeError):
    pass


def _result(payload: dict, what: str) -> dict | list:
    if not payload.get("success"):
        errs = "; ".join(str(e.get("message", e)) for e in payload.get("errors", [])) or "unknown error"
        raise CfError(f"{what}: {errs}")
    return payload.get("result")


class CfApi:
    """The few Cloudflare API calls provisioning needs, against the OWNER's token."""

    def __init__(self, token: str) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=_API, timeout=20.0,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def account_id(self) -> str:
        r = await self._client.get("/accounts", params={"per_page": 1})
        accounts = _result(r.json(), "list accounts")
        if not accounts:
            raise CfError("the token can see no account — scope it Account → Cloudflare Tunnel:Edit")
        return accounts[0]["id"]

    async def create_tunnel(self, account_id: str, name: str, secret: str) -> str:
        r = await self._client.post(
            f"/accounts/{account_id}/cfd_tunnel",
            json={"name": name, "tunnel_secret": secret, "config_src": "local"},
        )
        return _result(r.json(), "create tunnel")["id"]

    async def zone_id(self, zone: str) -> str:
        r = await self._client.get("/zones", params={"name": zone})
        zones = _result(r.json(), "find zone")
        if not zones:
            raise CfError(f"zone {zone!r} is not on this account — scope the token Zone → DNS:Edit for it")
        return zones[0]["id"]

    async def ensure_dns(self, zone_id: str, hostname: str, tunnel_id: str) -> None:
        """Upsert the proxied CNAME a tunnel route needs. Idempotent: an existing
        record pointing elsewhere is updated, a matching one left alone."""
        target = f"{tunnel_id}.cfargotunnel.com"
        r = await self._client.get(
            f"/zones/{zone_id}/dns_records", params={"name": hostname, "type": "CNAME"})
        existing = _result(r.json(), "read dns")
        body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
        if existing:
            rec = existing[0]
            if rec.get("content") == target and rec.get("proxied"):
                return
            r = await self._client.put(f"/zones/{zone_id}/dns_records/{rec['id']}", json=body)
            _result(r.json(), f"update dns {hostname}")
        else:
            r = await self._client.post(f"/zones/{zone_id}/dns_records", json=body)
            _result(r.json(), f"create dns {hostname}")
