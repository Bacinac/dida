"""Adapter onboarding + configuration routes. Extracted from app.py — this is the
Settings → Adapters domain: list every adapter's config schema + live status,
persist config/secrets (encrypted), network discovery, and the interactive
onboarding flows that can't be driven by plain config edits (ESPHome node
management, Tuya cloud pull, HomeKit pairing).

Bus/DB access goes through `request.app.state.{bus,pool}`; the module-level
helpers take `request` so they reach the same state without importing app.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from urllib.parse import urlsplit

from dida_core import (
    ADAPTER_CONFIG,
    decrypt_secret,
    encrypt_secret,
    fields_for,
    slug,
    status_subject,
    subnet_hosts,
)
from dida_core.adapter_config import DISCOVERABLE
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, require_admin
from dida_api.common import get_setting, vlan_networks

log = logging.getLogger("dida.api.adapters")
router = APIRouter(prefix="/adapters", tags=["adapters"])


def _adapter_secret() -> str:
    # Cross-service secret: MUST be the shared DIDA_SECRET_KEY that adapters and
    # medialib decrypt with. Never fall back to the per-instance JWT key
    # (app.state.secret_key) — that silently diverges from what every other
    # container uses and breaks all adapter secrets on the default install.
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(
            500,
            "DIDA_SECRET_KEY is not set — adapter secrets cannot be saved or read. "
            "Set it (the same value on api and every adapter with secrets) "
            "and restart the services.",
        )
    return secret


_SSDP_AUTO = {"denon", "heos", "dlna"}  # self-discover via SSDP — appear with no config


def _adapter_category(adapter: str) -> str:
    if adapter in DISCOVERABLE:
        return "discover"   # scan the network → click to add
    if adapter in _SSDP_AUTO:
        return "auto"       # shows up by itself once running
    return "account"        # needs credentials / manual setup (undiscoverable secret)


async def _adapter_status(app, adapter: str) -> dict | None:
    """Live connection status from an adapter (idle/connecting/ok/error), or None
    if it doesn't report / isn't answering. Fail-loud onboarding feedback. Takes the
    app (not a Request) so the alert evaluator's background loop can reuse it."""
    try:
        resp = await app.state.bus.nc.request(status_subject(adapter), b"", timeout=1.5)
        st = json.loads(resp.data)
        return st if isinstance(st, dict) and st.get("state") else None
    except Exception:
        log.debug("status of %s not answered", adapter, exc_info=True)
        return None


async def _scan_subnets(request: Request) -> list[str]:
    """Subnets discovery scans: DIDA's VLAN-presence subnets (auto-derived from the
    address netmgr obtained on each VLAN) UNION the manual IoT-subnets setting. The
    two COMPOSE — a VLAN foot scans its own segment on-link, and the manual subnets
    add any extra routed subnets DIDA can only reach over L3. A manual subnet already
    covered by a VLAN foot is rejected at write time (see update_settings), so the
    union only ever holds distinct segments."""
    pool = request.app.state.pool
    nets: set[str] = {str(n) for _, n in await vlan_networks(pool)}
    nets.update(s.strip() for s in (await get_setting(pool, "discovery_subnets") or "").split(",") if s.strip())
    # DIDA's own segment. lanprobe reports the address the host actually holds, and
    # scanning "the network I am on" is what a user means by Scan network — without
    # it an installation with no VLAN foot and no manual subnet scanned nothing at
    # all and reported "found none", which reads as "you have nothing".
    try:
        lan = json.loads(await get_setting(pool, "lan_status") or "{}")
        addr = str(lan.get("address", "")).strip()
        if addr:
            nets.add(str(ipaddress.ip_network(f"{addr}/24", strict=False)))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    scanned = sorted(nets)
    try:
        subnet_hosts(scanned)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return scanned


@router.get("")
async def list_adapter_config(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Config schema + current values for every UI-configurable adapter. Secret
    values never leave the server — only a configured/not flag and a plain value
    for non-secret fields."""
    names = sorted(ADAPTER_CONFIG)
    # Query every adapter's live status concurrently (short timeout) so the page
    # shows a fail-loud badge without serialising 18 round-trips.
    statuses = dict(zip(names, await asyncio.gather(*(_adapter_status(request.app, n) for n in names)), strict=True))
    # One query for every adapter's stored config (was N+1: a fetch per adapter),
    # grouped by adapter in Python.
    cfg_rows = await request.app.state.pool.fetch(
        "SELECT adapter, key, value FROM adapter_config WHERE adapter = ANY($1)", names
    )
    by_adapter: dict[str, dict[str, str]] = {}
    for r in cfg_rows:
        by_adapter.setdefault(r["adapter"], {})[r["key"]] = r["value"]
    out: list[dict] = []
    for adapter in names:
        db = by_adapter.get(adapter, {})
        fout = []
        for f in fields_for(adapter):
            value = "" if f.secret else (db.get(f.key) or f.default)
            fout.append({
                "key": f.key, "label": f.label, "type": f.type, "secret": f.secret,
                "placeholder": f.placeholder, "help": f.help, "options": list(f.options),
                "entity_cap": f.entity_cap, "group": f.group, "value": value,
                "configured": f.key in db,
            })
        out.append({
            "adapter": adapter, "fields": fout,
            "discoverable": adapter in DISCOVERABLE,
            "category": _adapter_category(adapter),
            "status": statuses.get(adapter),
            "label": db.get("_label") or "",  # human-friendly display name (optional)
        })
    return out


# Which adapters this installation actually runs. Each one is a compose profile and
# the runner is the only process that may start or stop them (it holds the Docker
# socket; this api, published through the tunnel, deliberately does not).

_RUNNER_CTL = "dida.runner.ctl"


async def _runner_ctl(request: Request, payload: dict, timeout_s: float = 20) -> dict:
    try:
        resp = await request.app.state.bus.nc.request(
            _RUNNER_CTL, json.dumps(payload).encode(), timeout=timeout_s
        )
    except Exception as exc:
        raise HTTPException(503, f"Runner ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error") and not result.get("profiles"):
        raise HTTPException(400, result["error"])
    return result


class RuntimeToggle(BaseModel):
    enabled: bool


@router.get("/runtime")
async def adapters_runtime(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Per profile: is it enabled, and how many of its containers are up."""
    return await _runner_ctl(request, {"action": "state"})


@router.post("/runtime/{profile}")
async def adapters_runtime_toggle(
    profile: str, body: RuntimeToggle, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Switch a profile on or off. Returns as soon as the runner accepts it — the
    containers come up (or go away) in the background, which is what keeps the
    reply from riding a connection the change itself may cut."""
    action = "enable" if body.enabled else "disable"
    return await _runner_ctl(request, {"action": action, "profile": profile})


@router.get("/discover-all")
async def discover_all(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Scan the network across every enabled discoverable adapter at once — the
    first-run flow. Returns found devices grouped by adapter (each pre-fills its
    config) and, per adapter whose scan did not complete, the reason."""
    subnets = await _scan_subnets(request)
    profiles = (await _runner_ctl(request, {"action": "state"})).get("profiles") or {}
    found: dict[str, list] = {}
    failed: dict[str, str] = {}
    for adapter in sorted(a for a in DISCOVERABLE if (profiles.get(a) or {}).get("enabled")):
        try:
            resp = await request.app.state.bus.nc.request(
                f"dida.discover.{adapter}", json.dumps({"subnets": subnets}).encode(), timeout=20
            )
            reply = json.loads(resp.data)
        except Exception as exc:
            log.warning("discover-all: %s did not answer: %s", adapter, exc, exc_info=True)
            failed[adapter] = str(exc) or type(exc).__name__
            continue
        if reply.get("error"):
            failed[adapter] = str(reply["error"])
        found[adapter] = reply.get("devices") or []
    return {"found": found, "failed": failed, "subnets": subnets}


@router.get("/{adapter}/discover")
async def discover_adapter(adapter: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Ask the adapter to scan the LAN for its devices (Shelly/Broadlink/…). The
    adapter — which is on the right network — replies with a list of found devices
    describing how a click fills the config form."""
    if adapter not in DISCOVERABLE:
        raise HTTPException(404, "adapter ne podržava skeniranje")
    subnets = await _scan_subnets(request)
    try:
        resp = await request.app.state.bus.nc.request(
            f"dida.discover.{adapter}", json.dumps({"subnets": subnets}).encode(), timeout=20
        )
    except Exception as exc:
        raise HTTPException(503, f"adapter ne odgovara: {exc}") from exc
    return json.loads(resp.data)


class AdapterConfigIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


@router.put("/{adapter}/config", status_code=204)
async def put_adapter_config(
    adapter: str, body: AdapterConfigIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    """Store config values. Secrets are encrypted; a blank secret field leaves the
    stored value untouched; a blank plain field clears it (falls back to env)."""
    fields = {f.key: f for f in fields_for(adapter)}
    if not fields:
        raise HTTPException(404, "unknown adapter")
    secret = _adapter_secret()
    pool = request.app.state.pool
    upsert = (
        "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, $2, $3) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
    )
    for key, raw in body.values.items():
        f = fields.get(key)
        if f is None:
            continue
        val = (raw or "").strip()
        if f.secret:
            if val:
                await pool.execute(upsert, adapter, key, encrypt_secret(secret, val))
        elif val:
            await pool.execute(upsert, adapter, key, val)
        else:
            await pool.execute(
                "DELETE FROM adapter_config WHERE adapter = $1 AND key = $2", adapter, key
            )


class AdapterLabelIn(BaseModel):
    label: str = ""


@router.put("/{adapter}/label", status_code=204)
async def put_adapter_label(
    adapter: str, body: AdapterLabelIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    """Human-friendly display name for an adapter, stored under the reserved
    `_label` key (not a ConfigField). Blank clears it (falls back to the name)."""
    if adapter not in ADAPTER_CONFIG:
        raise HTTPException(404, "unknown adapter")
    pool = request.app.state.pool
    label = body.label.strip()
    if label:
        await pool.execute(
            "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, '_label', $2) "
            "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            adapter, label,
        )
    else:
        await pool.execute(
            "DELETE FROM adapter_config WHERE adapter = $1 AND key = '_label'", adapter
        )


class DiscoveredIn(BaseModel):
    """One found device's apply spec (mirrors DiscoveredDevice on the client)."""

    set: dict[str, str] = Field(default_factory=dict)
    appendCsv: dict[str, str] = Field(default_factory=dict)
    appendJson: dict[str, dict] = Field(default_factory=dict)


@router.post("/{adapter}/discovered")
async def add_discovered(
    adapter: str, body: DiscoveredIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Apply one discovered device to an adapter's config — server-side, so it can't
    clobber earlier ones. The client never holds an encrypted field's value (it's
    masked), so it can't safely append to it; the server reads the current value
    (decrypted), appends, and re-saves (re-encrypted). Idempotent per device."""
    fields = {f.key: f for f in fields_for(adapter)}
    if not fields:
        raise HTTPException(404, "unknown adapter")
    secret = _adapter_secret()
    pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT key, value FROM adapter_config WHERE adapter = $1", adapter
    )
    stored = {r["key"]: r["value"] for r in rows}

    def current(key: str) -> str:
        f = fields[key]
        if key in stored:
            return decrypt_secret(secret, stored[key]) if f.secret else stored[key]
        return f.default

    updates: dict[str, str] = {}
    for key, val in body.set.items():
        if key in fields:
            updates[key] = val
    for key, val in body.appendCsv.items():
        if key not in fields:
            continue
        items = [s.strip() for s in updates.get(key, current(key)).split(",") if s.strip()]
        if val not in items:
            items.append(val)
        updates[key] = ", ".join(items)
    for key, obj in body.appendJson.items():
        if key not in fields:
            continue
        try:
            arr = json.loads(updates.get(key) or current(key) or "[]")
            if not isinstance(arr, list):
                arr = []
        except (json.JSONDecodeError, TypeError):
            arr = []
        arr.append(obj)
        updates[key] = json.dumps(arr, indent=2)

    upsert = (
        "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, $2, $3) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
    )
    for key, val in updates.items():
        f = fields[key]
        await pool.execute(
            upsert, adapter, key, encrypt_secret(secret, val) if f.secret else val
        )
    return {"ok": True}


# --- ESPHome node management (device-centric UI, no raw JSON) ------------
# The node list is the encrypted `config` array; the UI edits it through these
# so a user never touches JSON. `key` mirrors the adapter's device_key (slug of
# name-or-host) so the UI can line a node up with its registry fields.
_ESPHOME_SLUG = re.compile(r"[^a-z0-9_-]+")


def _esphome_key(name_or_host: str) -> str:
    return _ESPHOME_SLUG.sub("_", name_or_host.strip().lower()).strip("_") or "dev"


async def _esphome_nodes_raw(request: Request) -> list[dict]:
    row = await request.app.state.pool.fetchrow(
        "SELECT value FROM adapter_config WHERE adapter = 'esphome' AND key = 'config'"
    )
    if not row:
        return []
    try:
        arr = json.loads(decrypt_secret(_adapter_secret(), row["value"]))
    except (json.JSONDecodeError, TypeError):
        return []
    return [d for d in arr if isinstance(d, dict) and d.get("host")] if isinstance(arr, list) else []


async def _esphome_status(request: Request) -> dict:
    """Live per-node liveness from the adapter (connecting/online/offline/error),
    keyed by device_key. Empty if the adapter isn't answering (then the UI shows
    the adapter itself as down rather than guessing per node)."""
    try:
        resp = await request.app.state.bus.nc.request(
            "dida.esphome.ctl", json.dumps({"action": "status"}).encode(), timeout=3
        )
        return json.loads(resp.data).get("nodes", {}) or {}
    except Exception:
        log.debug("esphome node list not answered", exc_info=True)
        return {}


@router.get("/esphome/nodes")
async def esphome_nodes(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Configured ESPHome nodes (secrets masked) + live connection status, so the
    UI lists them as cards that show WHY a node isn't up (fail loud, not a silent
    perpetual 'Connecting…')."""
    status = await _esphome_status(request)
    out = []
    for d in await _esphome_nodes_raw(request):
        host = str(d.get("host", ""))
        name = str(d.get("name", "") or "")
        key = _esphome_key(name or host)
        st = status.get(key) or {}
        out.append({
            "name": name or None, "host": host, "key": key,
            "has_psk": bool(d.get("noise_psk")), "has_password": bool(d.get("password")),
            "state": st.get("state"), "code": st.get("code"),
            "reason": st.get("reason"), "entities": st.get("entities"),
        })
    return {"nodes": out, "adapter_up": bool(status)}


@router.delete("/esphome/nodes/{key}")
async def esphome_remove_node(key: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Remove one node from the config and drop its registry entities."""
    pool = request.app.state.pool
    nodes = await _esphome_nodes_raw(request)
    kept = [d for d in nodes if _esphome_key(str(d.get("name", "") or d.get("host", ""))) != key]
    val = encrypt_secret(_adapter_secret(), json.dumps(kept, indent=2))
    await pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ('esphome', 'config', $1) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", val
    )
    # current_state cascades on entity delete.
    await pool.execute(
        "DELETE FROM entities WHERE adapter = 'esphome' AND device_key = $1", key
    )
    return {"ok": True, "removed": len(nodes) - len(kept)}


class NodeEdit(BaseModel):
    """Editable connection params. Only fields present are applied; an empty
    string clears the field, absent keeps it (so an untouched secret is kept)."""

    name: str | None = None
    host: str | None = None
    noise_psk: str | None = None
    password: str | None = None


@router.put("/esphome/nodes/{key}")
async def esphome_edit_node(
    key: str, body: NodeEdit, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Edit one node's connection (host / noise_psk / password / name) in place —
    no remove+re-add. The adapter's supervise loop notices the changed signature
    and reconnects the node with the new params."""
    pool = request.app.state.pool
    nodes = await _esphome_nodes_raw(request)
    target = next(
        (d for d in nodes
         if _esphome_key(str(d.get("name", "") or d.get("host", ""))) == key), None
    )
    if target is None:
        raise HTTPException(404, "Nod ne postoji.")
    fields = body.model_dump(exclude_unset=True)
    for f in ("name", "host", "noise_psk", "password"):
        if f in fields:
            target[f] = (fields[f] or "").strip()
    if not str(target.get("host", "")).strip():
        raise HTTPException(400, "Host je obavezan.")
    new_key = _esphome_key(str(target.get("name", "") or target.get("host", "")))
    val = encrypt_secret(_adapter_secret(), json.dumps(nodes, indent=2))
    await pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ('esphome', 'config', $1) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", val
    )
    # If the identity slug changed (name/host edit), the node re-keys on reconnect;
    # drop the old registry entities so no ghost card lingers.
    if new_key != key:
        await pool.execute(
            "DELETE FROM entities WHERE adapter = 'esphome' AND device_key = $1", key
        )
    return {"ok": True, "key": new_key}


# --- Locations (sites) ---------------------------------------------------
# Both vision adapters serve several installs at once — Frigate NVRs and BABA
# boxes alike — so each keeps a LIST of locations in one encrypted `sites` blob
# (like the esphome node list). These are the per-location editors: add/edit/
# remove a location, with that location's cameras grouped under it in the UI
# (the client matches a camera's descriptor `site` to the location name).
#
# The storage, keying and camera-cleanup are shared below; what differs is only
# each adapter's field list, which stays explicit in its own routes. A location's
# KEY is derived from its name, so renaming one re-keys it — the editor sends the
# old key and writes the new name in place, which is why upsert matches on key.


def _site_key(name: str) -> str:
    return slug(name, default="site")


async def _sites_raw(request: Request, adapter: str, required: str) -> list[dict]:
    """The adapter's stored locations. `required` is the field a location cannot
    exist without (its address), so a half-written entry is dropped rather than
    resurfaced as a location pointing nowhere."""
    row = await request.app.state.pool.fetchrow(
        "SELECT value FROM adapter_config WHERE adapter = $1 AND key = 'sites'", adapter
    )
    if not row:
        return []
    try:
        arr = json.loads(decrypt_secret(_adapter_secret(), row["value"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return [d for d in arr if isinstance(d, dict) and d.get(required)] if isinstance(arr, list) else []


async def _save_sites(request: Request, adapter: str, sites: list[dict]) -> None:
    val = encrypt_secret(_adapter_secret(), json.dumps(sites, indent=2))
    await request.app.state.pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ($1, 'sites', $2) "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        adapter, val
    )


async def _drop_site_cameras(pool, name: str) -> int:
    """Delete the cameras that reported from a removed location, resolved through
    their descriptor's `site` — the only place the location is recorded, since
    entity ids stay flat so that adding a second install migrates nothing.

    A camera descriptor is a JSON document the adapter publishes as a STRING
    value, so `current_state.value` is a jsonb string, not a jsonb object, and
    `value->>'site'` on it is always NULL. That silently matched nothing: removing
    a location left every one of its cameras behind as a ghost tile — the exact
    thing this function exists to prevent. `#>> '{}'` unwraps the string back to
    the document before the field is read."""
    rows = await pool.fetch(
        "SELECT entity_id FROM current_state WHERE capability = 'camera' "
        "AND ((value #>> '{}')::jsonb) ->> 'site' = $1", name
    )
    for r in rows:
        eid = r["entity_id"]
        # the camera device + its zone/scene/person children (`<eid>:…`)
        await pool.execute("DELETE FROM entities WHERE entity_id = $1 OR entity_id LIKE $2", eid, f"{eid}:%")
        await pool.execute("DELETE FROM current_state WHERE entity_id = $1 OR entity_id LIKE $2", eid, f"{eid}:%")
    return len(rows)


@router.get("/frigate/sites")
async def frigate_sites(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Configured Frigate locations (password masked) so the UI lists them as
    cards, each with its own setup + the cameras that report from it."""
    out = []
    for d in await _sites_raw(request, "frigate", "url"):
        name = str(d.get("name", "") or "")
        out.append({
            "key": _site_key(name or str(d.get("url", ""))),
            "name": name,
            "url": str(d.get("url", "")),
            "user": str(d.get("user", "")),
            "go2rtc": str(d.get("go2rtc", "")),
            "has_password": bool(d.get("password")),
        })
    return {"sites": out}


class SiteEdit(BaseModel):
    """Add or edit one location. A blank password on edit keeps the stored one."""

    name: str = ""
    url: str = ""
    user: str = ""
    password: str | None = None
    go2rtc: str = ""


@router.put("/frigate/sites/{key}")
async def frigate_save_site(
    key: str, body: SiteEdit, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Upsert a location by key. `key` == "" (or an unknown key) adds a new one;
    an existing key edits in place. The adapter's refresh loop picks it up live."""
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "URL je obavezan.")
    name = body.name.strip() or url
    sites = await _sites_raw(request, "frigate", "url")
    target = next((d for d in sites if _site_key(str(d.get("name", "") or d.get("url", ""))) == key), None)
    if target is None:
        target = {}
        sites.append(target)
    target["name"] = name
    target["url"] = url
    target["user"] = body.user.strip()
    target["go2rtc"] = body.go2rtc.strip()
    if body.password:                      # blank keeps the existing secret
        target["password"] = body.password
    await _save_sites(request, "frigate", sites)
    return {"ok": True, "key": _site_key(name)}


@router.delete("/frigate/sites/{key}")
async def frigate_remove_site(key: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Remove a location + drop the cameras that reported from it (registry +
    current_state), so no ghost tiles linger."""
    pool = request.app.state.pool
    sites = await _sites_raw(request, "frigate", "url")
    gone = [d for d in sites if _site_key(str(d.get("name", "") or d.get("url", ""))) == key]
    kept = [d for d in sites if _site_key(str(d.get("name", "") or d.get("url", ""))) != key]
    await _save_sites(request, "frigate", kept)
    removed_cams = 0
    for site in gone:
        removed_cams += await _drop_site_cameras(pool, str(site.get("name", "") or ""))
    return {"ok": True, "removed_sites": len(gone), "removed_cameras": removed_cams}


# --- BABA locations (sites) ----------------------------------------------
# Same editor for BABA installs. A location is {name, nats_url, nats_user,
# nats_password, go2rtc, go2rtc_user, go2rtc_password, api_url, peer_key}: the
# state plane (NATS), the media plane (go2rtc, basic auth) and the archive plane
# (BABA's REST API, peer key) — three planes on one box, three credentials.


@router.get("/baba/sites")
async def baba_sites(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Configured BABA installs, secrets masked to a has/hasn't flag."""
    out = []
    for d in await _sites_raw(request, "baba", "nats_url"):
        name = str(d.get("name", "") or "")
        out.append({
            "key": _site_key(name or str(d.get("nats_url", ""))),
            "name": name,
            "nats_url": str(d.get("nats_url", "")),
            "nats_user": str(d.get("nats_user", "")),
            "has_nats_password": bool(d.get("nats_password")),
            "go2rtc": str(d.get("go2rtc", "")),
            "go2rtc_user": str(d.get("go2rtc_user", "")),
            "api_url": str(d.get("api_url", "")),
            "has_go2rtc_password": bool(d.get("go2rtc_password")),
            "has_peer_key": bool(d.get("peer_key")),
        })
    return {"sites": out}


class BabaSiteEdit(BaseModel):
    """Add or edit one BABA install. A blank secret on edit keeps the stored one."""

    name: str = ""
    nats_url: str = ""
    nats_user: str = ""
    nats_password: str | None = None
    go2rtc: str = ""
    go2rtc_user: str = ""
    go2rtc_password: str | None = None
    api_url: str = ""
    peer_key: str | None = None


@router.put("/baba/sites/{key}")
async def baba_save_site(
    key: str, body: BabaSiteEdit, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Upsert a location by key. `key` == "" (or an unknown key) adds a new one;
    an existing key edits in place. The adapter's supervise loop picks it up live
    and reconnects ONLY this location."""
    nats_url = body.nats_url.strip()
    if not nats_url:
        raise HTTPException(400, "NATS adresa je obavezna.")
    # A password inside the URL is shown back in the form and can become the
    # location's name; it has its own masked field.
    if urlsplit(nats_url).username is not None:
        raise HTTPException(400, "Korisnik i lozinka NATS-a upisuju se u zasebna polja, ne u adresu.")
    name = body.name.strip() or urlsplit(nats_url).hostname or "BABA"
    sites = await _sites_raw(request, "baba", "nats_url")
    target = next(
        (d for d in sites if _site_key(str(d.get("name", "") or d.get("nats_url", ""))) == key), None
    )
    if target is None:
        target = {}
        sites.append(target)
    target["name"] = name
    target["nats_url"] = nats_url
    target["nats_user"] = body.nats_user.strip()
    target["go2rtc"] = body.go2rtc.strip()
    target["go2rtc_user"] = body.go2rtc_user.strip()
    target["api_url"] = body.api_url.strip().rstrip("/")
    if body.nats_password:                 # blank keeps the existing secret
        target["nats_password"] = body.nats_password
    if body.go2rtc_password:
        target["go2rtc_password"] = body.go2rtc_password
    if body.peer_key:
        target["peer_key"] = body.peer_key
    await _save_sites(request, "baba", sites)
    return {"ok": True, "key": _site_key(name)}


async def lift_baba_nats_credentials(pool) -> None:
    """Locations saved before the NATS user and password had fields of their own
    carry them inside `nats_url`, where the editor showed them back in clear. They
    move out once, at boot, byte for byte — nats-py sends the URL's userinfo
    without percent-decoding it, and so must the fields."""
    row = await pool.fetchrow("SELECT value FROM adapter_config WHERE adapter = 'baba' AND key = 'sites'")
    if not row:
        return
    sites = json.loads(decrypt_secret(_adapter_secret(), row["value"]))
    moved: list[str] = []
    for d in sites if isinstance(sites, list) else []:
        if not isinstance(d, dict):
            continue
        u = urlsplit(str(d.get("nats_url", "")))
        if u.username is None:
            continue
        if u.password is None:
            log.warning("baba location %s: a NATS token in the URL has no field to move to — "
                        "enter a user and password in Settings → Adapters", d.get("name"))
            continue
        d["nats_user"], d["nats_password"] = u.username, u.password
        d["nats_url"] = u._replace(netloc=u.netloc.rpartition("@")[2]).geturl()
        moved.append(str(d.get("name") or u.hostname))
    if moved:
        await pool.execute(
            "UPDATE adapter_config SET value = $1, updated_at = now() WHERE adapter = 'baba' AND key = 'sites'",
            encrypt_secret(_adapter_secret(), json.dumps(sites, indent=2)),
        )
        log.info("baba: NATS credentials moved out of the URL for %s", ", ".join(moved))


@router.delete("/baba/sites/{key}")
async def baba_remove_site(key: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Remove a location + drop the cameras that reported from it. This is the ONLY
    path that deletes a BABA install's entities: the adapter's own roster prune
    never crosses locations, precisely so one house cannot wipe another's."""
    pool = request.app.state.pool
    sites = await _sites_raw(request, "baba", "nats_url")
    gone = [d for d in sites if _site_key(str(d.get("name", "") or d.get("nats_url", ""))) == key]
    kept = [d for d in sites if _site_key(str(d.get("name", "") or d.get("nats_url", ""))) != key]
    await _save_sites(request, "baba", kept)
    removed_cams = 0
    for site in gone:
        removed_cams += await _drop_site_cameras(pool, str(site.get("name", "") or ""))
    return {"ok": True, "removed_sites": len(gone), "removed_cameras": removed_cams}


# --- Tuya cloud onboarding ----------------------------------------------
# Tuya devices need a per-device local key, only obtainable from the Tuya cloud.
# The user enters their Tuya IoT Platform creds (Settings → Adapters); the adapter
# pulls keys + DP maps and locates each device on the LAN — onboarding is a UI
# flow, not hand key-extraction. Keys stay server-side (adapter → DB, encrypted).
_TUYA_CTL = "dida.tuya.ctl"


@router.get("/tuya/cloud-devices")
async def tuya_cloud_devices(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Pull this account's Tuya devices from the cloud + locate them on the LAN.
    Returns a sanitised list (no keys); the adapter caches the full dicts."""
    subnets = await _scan_subnets(request)
    try:
        resp = await request.app.state.bus.nc.request(
            _TUYA_CTL,
            json.dumps({"action": "cloud_devices", "subnets": subnets}).encode(),
            timeout=100,
        )
    except Exception as exc:
        raise HTTPException(503, f"Tuya adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error") == "no_creds":
        raise HTTPException(400, "Unesi Tuya cloud Access ID + Secret (i regiju) pa spremi.")
    if result.get("error"):
        raise HTTPException(400, f"Tuya cloud: {result['error']}")
    return result


class TuyaCloudAdd(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/tuya/cloud-add")
async def tuya_cloud_add(body: TuyaCloudAdd, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Persist the chosen fetched devices (by id) to the tuya config; the adapter
    brings them online on its next supervise pass."""
    try:
        resp = await request.app.state.bus.nc.request(
            _TUYA_CTL, json.dumps({"action": "add", "ids": body.ids}).encode(), timeout=30,
        )
    except Exception as exc:
        raise HTTPException(503, f"Tuya adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error"):
        raise HTTPException(400, f"Tuya: {result['error']}")
    return result


@router.delete("/tuya/devices/{key}")
async def tuya_remove_device(key: str, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Remove one Tuya device (by device_key) from the config + drop its entities."""
    try:
        resp = await request.app.state.bus.nc.request(
            _TUYA_CTL, json.dumps({"action": "remove", "keys": [key]}).encode(), timeout=15,
        )
    except Exception as exc:
        raise HTTPException(503, f"Tuya adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error"):
        raise HTTPException(400, f"Tuya: {result['error']}")
    return result


# --- HomeKit pairing (interactive onboarding) ---------------------------
# HomeKit accessories are added by PAIRING (an 8-digit setup code), not by
# editing config — so the API drives the homekit adapter (the HAP controller on
# the LAN) over a NATS request/reply control subject.
_HOMEKIT_CTL = "dida.homekit.ctl"


@router.get("/homekit/discovered")
async def homekit_discovered(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Accessories the controller sees on the network, with a `pairable` flag."""
    try:
        resp = await request.app.state.bus.nc.request(
            _HOMEKIT_CTL, json.dumps({"action": "discover"}).encode(), timeout=15
        )
    except Exception as exc:
        raise HTTPException(503, f"HomeKit adapter ne odgovara: {exc}") from exc
    return json.loads(resp.data)


class HomekitPairIn(BaseModel):
    device_id: str
    pin: str
    name: str = ""


@router.post("/homekit/pair")
async def homekit_pair(body: HomekitPairIn, request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Pair an accessory by its setup code; the adapter persists it (encrypted)
    and brings it online live."""
    try:
        resp = await request.app.state.bus.nc.request(
            _HOMEKIT_CTL,
            json.dumps({
                "action": "pair", "device_id": body.device_id,
                "pin": body.pin, "name": body.name,
            }).encode(),
            timeout=45,
        )
    except Exception as exc:
        raise HTTPException(503, f"HomeKit adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


# --- Android TV connect (ADB / developer mode) -----------------------------
# Android TV boxes (NVIDIA Shield, Google/Sony/Philips TVs) are driven over ADB
# (python-androidtv). Connecting is a one-time key-accept prompt ON the device, so
# the API just asks the adapter (the process on the LAN) to kick a connect; the
# user accepts the "Allow debugging?" dialog on the TV. Same request/reply shape
# as the other adapter control planes.
_ANDROIDTV_CTL = "dida.androidtv.ctl"


async def _androidtv_ctl(request: Request, payload: dict, timeout_s: float) -> dict:
    try:
        resp = await request.app.state.bus.nc.request(
            _ANDROIDTV_CTL, json.dumps(payload).encode(), timeout=timeout_s
        )
    except Exception as exc:
        raise HTTPException(503, f"Android TV adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@router.get("/androidtv/status")
async def androidtv_status(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Whether the box is connected over ADB + the saved host (drives the UI card)."""
    return await _androidtv_ctl(request, {"action": "status"}, 5)


@router.post("/androidtv/connect")
async def androidtv_connect(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Kick an immediate ADB (re)connect so the 'Allow debugging?' prompt appears
    on the TV now; the user accepts it there."""
    return await _androidtv_ctl(request, {"action": "connect"}, 10)


# --- Cloudflare tunnel (ingress routes) ---------------------------------
# The cloudflare adapter owns the locally-managed tunnel's config.yml (single
# source of truth). These proxy CRUD over its ingress list through the
# `dida.cloudflare.ctl` control plane. The adapter writes services.conf and waits
# for the host applier to report; a rejected change is rolled back there, so a 200
# here means the ingress really changed. Admin-only — this is the whole homelab.

_CLOUDFLARE_CTL = "dida.cloudflare.ctl"


async def _cloudflare_ctl(request: Request, payload: dict, timeout_s: float = 35) -> dict:
    try:
        resp = await request.app.state.bus.nc.request(
            _CLOUDFLARE_CTL, json.dumps(payload).encode(), timeout=timeout_s
        )
    except Exception as exc:
        raise HTTPException(503, f"Cloudflare adapter ne odgovara: {exc}") from exc
    result = json.loads(resp.data)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@router.get("/cloudflare/tunnel")
async def cloudflare_tunnel_state(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Whether this installation has an ingress, and whether the owner's API
    token is set — what decides if 'create a tunnel' is even offered."""
    return await _cloudflare_ctl(request, {"action": "tunnel_state"}, timeout_s=10)


class TunnelCreateIn(BaseModel):
    name: str


@router.post("/cloudflare/tunnel")
async def cloudflare_tunnel_create(
    body: TunnelCreateIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Mint this installation's tunnel on the OWNER's Cloudflare account (their
    token, stored as this adapter's secret) and bring the connectors up."""
    return await _cloudflare_ctl(request, {"action": "tunnel_create", "name": body.name.strip()}, timeout_s=60)


class CloudflareRoute(BaseModel):
    hostname: str
    service: str
    serve: str = "both"          # both = LAN + WAN | lan = split-DNS only | wan = tunnel only
    opts: list[str] = []         # nochunk — Proxmox' console needs unchunked responses
    section: str | None = None   # Homepage section; without one no tile is created
    icon: str | None = None


@router.get("/cloudflare/routes")
async def cloudflare_routes(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Every ingress route (hostname → service + flags) from the tunnel config."""
    return await _cloudflare_ctl(request, {"action": "list"}, 10)


@router.post("/cloudflare/routes")
async def cloudflare_add_route(
    body: CloudflareRoute, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Add a route to services.conf; the host applies Caddy, the tunnel, DNS,
    split-DNS and the Homepage tile, and the reply carries what it did."""
    return await _cloudflare_ctl(request, {"action": "add", **body.model_dump()})


@router.put("/cloudflare/routes/{hostname}")
async def cloudflare_edit_route(
    hostname: str, body: CloudflareRoute, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Edit a route in services.conf; the host re-applies everything downstream."""
    payload = {"action": "edit", **body.model_dump()}
    payload["hostname"] = hostname  # the path is authoritative for which route to edit
    return await _cloudflare_ctl(request, payload)


@router.delete("/cloudflare/routes/{hostname}")
async def cloudflare_remove_route(
    hostname: str, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Remove a route from services.conf. The Caddy vhost and the tunnel rule go with
    it; the DNS record and the Homepage tile are left, since neither is safe to delete
    on the strength of one absent row."""
    result = await _cloudflare_ctl(request, {"action": "remove", "hostname": hostname})
    # The route's entity mirrors the row that just went away, and the adapter only
    # ever publishes routes that still exist — so without this the entity lingers
    # in the registry forever with nothing left to reconcile it. Deliberately NOT
    # the delete_device path: that writes a removed_devices tombstone, which would
    # make re-adding the same hostname produce a route with no entity.
    #
    # The id comes from the adapter: route entities are keyed by FQDN while this
    # endpoint is given the bare subdomain, and deriving it here would delete nothing.
    eid = result.get("entity_id") or f"cloudflare:{slug(hostname)}"
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        await con.execute("DELETE FROM current_state WHERE entity_id = $1", eid)
        await con.execute("DELETE FROM entities WHERE entity_id = $1", eid)
    return result


# --- Zigbee onboarding + mesh diagnostics (over the MQTT adapter's bus) ---------
# The MQTT adapter speaks zigbee2mqtt's bridge API for us, so a device is paired,
# the mesh is mapped, and a node is renamed/removed from DIDA — the z2m console is
# never needed. All admin-only; each maps to one control-channel action.
_MQTT_CTL = "dida.mqtt.ctl"


async def _mqtt_ctl(request: Request, payload: dict, timeout_s: float) -> dict:
    try:
        resp = await request.app.state.bus.nc.request(
            _MQTT_CTL, json.dumps(payload).encode(), timeout=timeout_s
        )
    except Exception as exc:
        raise HTTPException(503, f"MQTT adapter ne odgovara: {exc}") from exc
    return json.loads(resp.data)


def _raise_on_z2m_error(result: dict) -> dict:
    """A bridge action that z2m refused comes back status=error (+ a reason); a
    transport-level failure came back as our own {error}. Surface both loud."""
    if result.get("error") or result.get("status") == "error":
        raise HTTPException(400, result.get("error") or "zigbee2mqtt odbio zahtjev")
    return result


class PermitJoinIn(BaseModel):
    on: bool = True
    seconds: int = Field(254, ge=0, le=254)


@router.post("/zigbee/permit-join")
async def zigbee_permit_join(
    body: PermitJoinIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Open (or close) the Zigbee pairing window. While it is open, put the new
    device into pairing mode; interview progress arrives on its device timeline."""
    return _raise_on_z2m_error(await _mqtt_ctl(
        request, {"action": "permit_join", "on": body.on, "seconds": body.seconds}, 20))


class NetworkScanIn(BaseModel):
    routes: bool = False


@router.get("/zigbee/network-map")
async def zigbee_network_map(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """The last cached mesh map (instant). `scanning` is true while a scan runs;
    `ts_ns` is when the cached map was taken. Kick a fresh scan with POST."""
    return await _mqtt_ctl(request, {"action": "networkmap"}, 5)


@router.post("/zigbee/network-map")
async def zigbee_network_scan(
    body: NetworkScanIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Start a mesh scan (slow and disruptive — on demand only) and return at once
    with scanning=true. Poll the GET until `ts_ns` advances."""
    return await _mqtt_ctl(
        request, {"action": "networkmap", "refresh": True, "routes": body.routes}, 5)


@router.get("/zigbee/availability")
async def zigbee_availability(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Whether zigbee2mqtt's availability feature is on — the signal that turns a
    silent Zigbee device into 'unreachable' in DIDA. null if not yet known."""
    return await _mqtt_ctl(request, {"action": "availability"}, 5)


class AvailabilityIn(BaseModel):
    enabled: bool


@router.post("/zigbee/availability")
async def zigbee_set_availability(
    body: AvailabilityIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Turn zigbee2mqtt availability on/off. z2m applies it live (no restart) and
    persists it to its own config — so this is configured from DIDA, not by hand."""
    return _raise_on_z2m_error(await _mqtt_ctl(
        request, {"action": "set_availability", "enabled": body.enabled}, 25))


class RenameIn(BaseModel):
    model_config = {"populate_by_name": True}
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


@router.post("/zigbee/rename")
async def zigbee_rename(
    body: RenameIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    return _raise_on_z2m_error(await _mqtt_ctl(
        request, {"action": "rename", "from": body.from_, "to": body.to}, 20))


class RemoveIn(BaseModel):
    id: str = Field(min_length=1)
    force: bool = False


@router.post("/zigbee/remove")
async def zigbee_remove(
    body: RemoveIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> dict:
    """Remove a device from the Zigbee network. `force` deletes it locally even if
    the device can't be reached to leave cleanly (last resort — it may rejoin)."""
    return _raise_on_z2m_error(await _mqtt_ctl(
        request, {"action": "remove", "id": body.id, "force": body.force}, 35))
