"""Admin settings routes — API keys (Fernet secrets in app_settings), TTS
language, and network discovery/VLAN config. Extracted from app.py. Reads/writes
the app_settings key/value store; secrets are never returned (only a last-4 hint).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from dida_core import clear_app_setting, encrypt_secret, host_setting, set_app_setting, subnet_hosts
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, require_admin
from dida_api.common import get_setting, key_hint, resolved_key, stored_api_key, vlan_networks

log = logging.getLogger("dida.api.settings")

router = APIRouter(tags=["settings"])


class SettingsOut(BaseModel):
    anthropic_configured: bool
    anthropic_hint: str | None
    openai_configured: bool
    openai_hint: str | None
    owntracks_configured: bool  # payload-encryption key for the OwnTracks receiver
    announce_lang: str  # TTS default language code (e.g. "hr"); editable here
    lan_ip: str  # THIS host's address on the LAN the AV devices are on, e.g. 192.0.2.10
    opus_url: str  # where OPUS · Player answers, as the devices reach it
    opus_configured: bool  # url + token both set (the media page has a shelf)
    radio_player: str  # the player radio:tuner plays the OPUS stations on ("" = none)
    app_url: str  # the origin people open DIDA on (the tunnel), used for links + OAuth
    net_parent: str  # host NIC the VLAN sub-links hang off (netmgr)
    # Where the Zigbee2MQTT console answers, for the link on the Zigbee card. Built
    # from THIS host's LAN address and the port the compose file publishes, because
    # both already exist and a third copy in the frontend is a third thing to be
    # wrong. Empty when the host address is unset — a link nobody can follow is
    # worse than none.
    zigbee_console_url: str
    discovery_subnets: str  # CSV of IoT subnets to scan, e.g. "203.0.113.0/24"
    lan_status: dict  # {address, hostname, interfaces: [{iface, address, mac, lan, default_route}]} — lanprobe
    managed_vlans: str  # CSV of VLAN ids DIDA brings up a sub-interface for
    vlan_status: dict  # {vlan_id: {iface, address, mac}} — obtained by netmgr
    vlan_config: dict  # {vlan_id: {mac, hostname}} — user overrides for netmgr


class SettingsIn(BaseModel):
    # Only provided keys are touched; "" clears that key (falls back to env).
    anthropic_api_key: str | None = Field(default=None, max_length=400)
    openai_api_key: str | None = Field(default=None, max_length=400)
    owntracks_secret: str | None = Field(default=None, max_length=200)
    announce_lang: str | None = Field(default=None, max_length=16)
    lan_ip: str | None = Field(default=None, max_length=64)
    opus_url: str | None = Field(default=None, max_length=200)
    opus_token: str | None = Field(default=None, max_length=200)
    radio_player: str | None = Field(default=None, max_length=200)
    app_url: str | None = Field(default=None, max_length=200)
    net_parent: str | None = Field(default=None, max_length=32)
    discovery_subnets: str | None = Field(default=None, max_length=400)
    managed_vlans: str | None = Field(default=None, max_length=200)
    vlan_config: dict | None = None  # {vlan_id: {mac, hostname}}


class SettingsTestIn(BaseModel):
    provider: str  # "anthropic" | "openai" | "opus" | "owntracks"
    api_key: str | None = Field(default=None, max_length=400)
    url: str | None = Field(default=None, max_length=300)  # opus: typed base URL


# Where the Zigbee console's own configuration is mounted, read-only and one file.
Z2M_CONFIG = os.environ.get("DIDA_Z2M_CONFIG", "/z2m/configuration.yaml")
_Z2M_TOKEN = re.compile(r"^\s*auth_token:\s*(\S+)\s*$", re.M)


@router.get("/settings/zigbee-token")
def zigbee_token(_admin: AuthUser = Depends(require_admin)) -> dict:
    """The Zigbee console's auth token, so an admin does not have to go and read a
    file on the host to open a console DIDA just linked them to.

    Deliberately its OWN request rather than a field on /settings: that payload is
    fetched by pages that have no business carrying a secret, and this one is asked
    for only when somebody presses the button. The token stays where it lives — in
    the console's config — and is read from there each time, so rotating it there is
    the whole of rotating it."""
    try:
        text = Path(Z2M_CONFIG).read_text()
    except OSError as exc:
        raise HTTPException(404, f"zigbee2mqtt config not readable: {exc}") from exc
    m = _Z2M_TOKEN.search(text)
    if not m:
        raise HTTPException(404, "zigbee2mqtt has no auth_token — the console is open")
    return {"token": m.group(1)}


@router.get("/settings", response_model=SettingsOut)
async def get_settings(request: Request, _admin: AuthUser = Depends(require_admin)) -> SettingsOut:
    pool = request.app.state.pool
    a = await resolved_key(pool, "anthropic")
    o = await resolved_key(pool, "openai")
    lang = await get_setting(pool, "announce_lang") or "hr"
    subnets = await get_setting(pool, "discovery_subnets") or ""
    # Host values live in the DB and only there — the environment seeded them once,
    # at first boot, and has had no say since.
    lan_ip = await host_setting(pool, "lan_ip")
    opus_url = await host_setting(pool, "opus_url")
    opus_token = await get_setting(pool, "opus_token")
    app_url = await host_setting(pool, "app_url") or await host_setting(pool, "public_url")
    net_parent = await host_setting(pool, "net_parent")
    vlans = await get_setting(pool, "managed_vlans") or ""
    try:
        lstatus = json.loads(await get_setting(pool, "lan_status") or "{}")
    except (json.JSONDecodeError, TypeError):
        lstatus = {}
    try:
        vstatus = json.loads(await get_setting(pool, "vlan_status") or "{}")
    except (json.JSONDecodeError, TypeError):
        vstatus = {}
    try:
        vconfig = json.loads(await get_setting(pool, "vlan_config") or "{}")
    except (json.JSONDecodeError, TypeError):
        vconfig = {}
    # The console lives on the same machine as DIDA, on its own published port; the
    # token stays OUT of the link (the console asks for it once and remembers) —
    # a secret in an address ends up in browser history and in every referrer.
    z2m = f"http://{lan_ip}:{os.environ.get('DIDA_Z2M_PORT', '8092')}" if lan_ip else ""
    return SettingsOut(
        anthropic_configured=bool(a), anthropic_hint=key_hint(a),
        openai_configured=bool(o), openai_hint=key_hint(o),
        owntracks_configured=bool(await get_setting(pool, "owntracks_secret")),
        announce_lang=lang, discovery_subnets=subnets, lan_status=lstatus,
        lan_ip=lan_ip, opus_url=opus_url, opus_configured=bool(opus_url and opus_token),
        radio_player=await get_setting(pool, "radio_player") or "",
        app_url=app_url, net_parent=net_parent,
        zigbee_console_url=z2m,
        managed_vlans=vlans, vlan_status=vstatus, vlan_config=vconfig,
    )


def _check_openai(key: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200, "ok"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        log.debug("settings: OpenAI key check failed", exc_info=True)
        return False, str(e)


async def _check_anthropic(key: str) -> tuple[bool, str]:
    try:
        await AsyncAnthropic(api_key=key).models.list()  # validates auth, no tokens
        return True, "ok"
    except Exception as e:
        log.debug("settings: Anthropic key check failed", exc_info=True)
        return False, str(e)


async def _check_opus(url: str, token: str) -> tuple[bool, str]:
    """One call the media page itself makes — proves the address and the token
    together."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(f"{url}/api/radio/stations", headers={"X-OPUS-Token": token})
    except Exception as e:
        log.debug("settings: OPUS check failed", exc_info=True)
        return False, f"server nedostupan ({e.__class__.__name__})"
    if r.status_code in (401, 403):
        return False, "token odbijen"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    return True, "ok"


async def _check_owntracks(pool) -> tuple[bool, str]:
    """A symmetric payload key has no upstream to ping — the honest end-to-end
    signal is whether location fixes still ARRIVE and decrypt: the freshest
    presence update. A wrong key makes every encrypted frame drop at decrypt
    (logged), so this timestamp going stale is exactly the failure to surface."""
    ts_ns = await pool.fetchval(
        "SELECT max(ts_ns) FROM current_state WHERE entity_id LIKE 'presence:%'"
    )
    if not ts_ns:
        return False, "nijedna lokacija još nije primljena"
    age_min = int((time.time() - ts_ns / 1e9) / 60)
    if age_min > 24 * 60:
        return False, f"zadnja lokacija prije {age_min // 60} h"
    return True, f"zadnja lokacija prije {age_min} min"


@router.post("/settings/test")
async def test_setting(body: SettingsTestIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> dict:
    """Validate a key WITHOUT storing it. Uses the typed value(s) if given, else
    the stored/env ones. Lets the UI verify before saving — one uniform surface
    for every key on the Settings page."""
    pool = request.app.state.pool
    provider = body.provider
    if provider not in ("anthropic", "openai", "opus", "owntracks"):
        raise HTTPException(400, "unknown provider")
    if provider == "owntracks":
        ok, detail = await _check_owntracks(pool)
        return {"ok": ok, "detail": detail}
    if provider == "opus":
        url = (body.url or "").strip().rstrip("/") or await host_setting(pool, "opus_url")
        key = (body.api_key or "").strip() or await stored_api_key(pool, "opus_token")
        if not url:
            return {"ok": False, "detail": "URL nije upisan"}
        if not key:
            return {"ok": False, "detail": "token nije upisan"}
        ok, detail = await _check_opus(url, key)
        return {"ok": ok, "detail": "radi" if ok else detail}
    key = (body.api_key or "").strip() or await resolved_key(pool, provider)
    if not key:
        return {"ok": False, "detail": "ključ nije upisan"}
    ok, detail = (
        await _check_anthropic(key) if provider == "anthropic"
        else await asyncio.to_thread(_check_openai, key)
    )
    return {"ok": ok, "detail": "radi" if ok else detail}


def _seal(value: str) -> str:
    return encrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), value)


async def _store(pool, key: str, value: str | None, *, secret: bool = False) -> None:
    """None leaves the setting as it is; an empty value clears it."""
    if value is None:
        return
    value = value.strip()
    if value:
        await set_app_setting(pool, key, _seal(value) if secret else value)
    else:
        await clear_app_setting(pool, key)


def _host_value(key: str, value: str | None) -> str | None:
    """A host value, checked. Cleared by an empty string, which falls back to the
    env seed — so an installation is corrected here instead of by editing .env on
    the box and redeploying (the cutover that lost the media base URL)."""
    if value is None:
        return None
    value = value.strip().rstrip("/") if key.endswith("url") else value.strip()
    if not value:
        return ""
    if key.endswith("url") and not value.startswith(("http://", "https://")):
        raise HTTPException(400, f"{key} must start with http:// or https://")
    if key == "lan_ip":
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            raise HTTPException(400, "lan_ip must be an IPv4 address") from None
    return value


async def _media_player(pool, player: str | None) -> str | None:
    if not player or not player.strip():
        return player
    player = player.strip()
    if not await pool.fetchval(
        "SELECT 1 FROM current_state WHERE entity_id = $1 AND capability = 'media_transport'",
        player,
    ):
        raise HTTPException(400, f"{player} is not a media player")
    return player


async def _discovery_subnets(pool, subnets: str | None) -> str | None:
    """Extra routed subnets discovery scans over L3, on top of the VLAN-presence
    subnets (they compose — see _scan_subnets). One a VLAN foot already covers is
    refused loudly: the foot supersedes a routed scan of the same range, so storing
    it would be a redundant no-op."""
    if not subnets or not subnets.strip():
        return subnets
    subnets = subnets.strip()
    vlan_nets = await vlan_networks(pool)
    for tok in (s.strip() for s in subnets.split(",") if s.strip()):
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            raise HTTPException(400, f"Invalid subnet: {tok}") from None
        for vid, vnet in vlan_nets:
            if net.version == vnet.version and net.subnet_of(vnet):
                raise HTTPException(
                    400,
                    f"{tok} is already covered by VLAN {vid} ({vnet}) — the VLAN "
                    "foot supersedes a routed scan of the same range. Remove it "
                    "here, or scan a different subnet.",
                )
    try:
        subnet_hosts(s for s in subnets.split(",") if s.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return subnets


def _managed_vlans(vlans: str | None) -> str | None:
    """Only digits and commas are stored: netmgr validates the range and only ever
    touches <parent>.<vlan> interfaces."""
    if vlans is None:
        return None
    return ",".join(t.strip() for t in vlans.split(",") if t.strip().isdigit())


def _vlan_config(config: dict | None) -> str | None:
    """Per-VLAN overrides netmgr applies: a fixed MAC (so the router can pin a
    reserved lease) and a DHCP host-name. Only a valid MAC or hostname is kept."""
    if config is None:
        return None
    clean: dict[str, dict] = {}
    for vid, c in config.items():
        if not str(vid).isdigit() or not isinstance(c, dict):
            continue
        mac = re.sub(r"[^0-9a-fA-F:]", "", str(c.get("mac", ""))).lower()
        host = re.sub(r"[^A-Za-z0-9-]", "", str(c.get("hostname", "")))[:63]
        entry: dict[str, str] = {}
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            entry["mac"] = mac
        if host:
            entry["hostname"] = host
        if entry:
            clean[str(vid)] = entry
    return json.dumps(clean) if clean else ""


@router.put("/settings", status_code=204)
async def put_settings(body: SettingsIn, request: Request,
                       _admin: AuthUser = Depends(require_admin)) -> None:
    """Store/clear secret keys. Never logged; never returned by GET."""
    pool = request.app.state.pool
    for key in ("anthropic_api_key", "openai_api_key", "owntracks_secret"):
        await _store(pool, key, getattr(body, key), secret=True)
    request.app.state.anthropic_client = None
    request.app.state.anthropic_key = None
    await _store(pool, "announce_lang", body.announce_lang)
    for key in ("lan_ip", "opus_url", "app_url", "net_parent"):
        await _store(pool, key, _host_value(key, getattr(body, key)))
    await _store(pool, "opus_token", body.opus_token, secret=True)
    await _store(pool, "radio_player", await _media_player(pool, body.radio_player))
    await _store(pool, "discovery_subnets", await _discovery_subnets(pool, body.discovery_subnets))
    await _store(pool, "managed_vlans", _managed_vlans(body.managed_vlans))
    await _store(pool, "vlan_config", _vlan_config(body.vlan_config))
