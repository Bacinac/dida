"""Tuya Cloud onboarding — pull local keys from the Tuya IoT Platform, auto-map
each device's data points to DIDA capabilities, and locate each device's LAN IP
by a unicast probe over routing.

This is the ONE place the cloud is touched. It exists purely so a new user can
add Tuya devices from the UI (enter cloud creds → fetch → add) instead of
hand-extracting per-device local keys and editing JSON. The runtime path stays
fully local (tinytuya local protocol); the cloud key just unlocks it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from dida_core import probe_hosts, subnet_hosts

log = logging.getLogger("dida.adapter.tuya.cloud")

TUYA_PORT = 6668
_VERSIONS = (3.3, 3.4, 3.5)  # try in turn; heater was 3.3, newer gear 3.4/3.5

# Tuya standard DP code -> DIDA capability. Covers the common switch/plug/light/
# sensor instruction set; codes we don't know are surfaced (unmapped) so the
# device still onboards with whatever DIDA can actually drive.
_CODE_CAP: dict[str, str] = {
    "switch": "on_off", "switch_1": "on_off", "switch_led": "on_off",
    "switch_led_1": "on_off", "led_switch": "on_off",
    "bright_value": "brightness", "bright_value_v2": "brightness", "bright_value_1": "brightness",
    "temp_value": "color_temp", "temp_value_v2": "color_temp",
    "colour_data": "color_rgb", "colour_data_v2": "color_rgb",
    "cur_power": "power", "cur_voltage": "voltage", "cur_current": "current", "add_ele": "energy",
    "va_temperature": "temperature", "temp_current": "temperature",
    "va_humidity": "humidity", "humidity_value": "humidity",
    "bright_sensor_value": "illuminance", "battery_percentage": "battery", "residual_electricity": "battery",
}
_ENUM_CODES = {"work_mode", "mode", "level", "fan_speed_enum", "windspeed"}

# Zigbee sub-devices behind a gateway (locks, keypads, sensors) are sleepy and
# don't answer a local status poll — they're read from the CLOUD. Their status
# arrives as {code: value} (getstatus), so we map by CODE here (not dp id).
_CLOUD_CODE_CAP: dict[str, str] = {
    "lock_motor_state": "lock",
    "residual_electricity": "battery", "battery_percentage": "battery", "battery_state": "battery",
    "closed_opened": "contact",  # door sensor → Open/Closed (not a bare Yes/No)
    "temper_alarm": "binary", "tamper": "binary",
    # (alarm_lock intentionally NOT mapped — battery% + an automation covers "low battery";
    #  the device latches the alarm even at 100%, so it's redundant + misleading.)
}


def cloud_status_map(codes: list[str]) -> tuple[dict, list[str]]:
    """Status codes present on a cloud device -> ({code: cap}, unmapped codes)."""
    dps = {c: _CLOUD_CODE_CAP[c] for c in codes if c in _CLOUD_CODE_CAP}
    return dps, [c for c in codes if c and c not in _CLOUD_CODE_CAP]


def decode_cloud(cap: str, value: object) -> object:
    """A cloud status value -> canonical capability value (None if unusable)."""
    if cap == "lock":
        return bool(value)
    if cap == "battery":
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if cap in ("binary", "contact"):
        if isinstance(value, str):
            return value.strip().lower() in ("opened", "open", "true", "1", "on", "alarm")
        return bool(value)
    return value


def _values(info: dict) -> dict:
    """A DP's `values` may arrive as a dict or a JSON string — normalise to dict."""
    v = info.get("values")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return {}
    return v if isinstance(v, dict) else {}


def auto_dps(mapping: dict | None) -> tuple[dict, list[str]]:
    """Tuya DP mapping {dp_id: {code,type,values}} -> (DIDA dps map, unmapped codes).

    dps entries are what the adapter's device file uses: dp id -> capability
    string, or {"cap":"enum","options":[…],"raw":[…]} for a selectable enum.
    """
    dps: dict[str, object] = {}
    unmapped: list[str] = []
    for dp_id, info in (mapping or {}).items():
        if not isinstance(info, dict):
            continue
        code = str(info.get("code") or "").lower()
        typ = str(info.get("type") or "").lower()
        if code in _CODE_CAP:
            dps[str(dp_id)] = _CODE_CAP[code]
        elif code in _ENUM_CODES or typ == "enum":
            rng = _values(info).get("range")
            if isinstance(rng, list) and rng:
                dps[str(dp_id)] = {
                    "cap": "enum",
                    "options": [str(x).replace("_", " ").title() for x in rng],
                    "raw": [str(x) for x in rng],
                }
            else:
                unmapped.append(code or f"dp{dp_id}")
        else:
            unmapped.append(code or f"dp{dp_id}")
    return dps, unmapped


async def scan_open(subnets: list[str], timeout_s: float = 1.5) -> list[str]:
    """IPs with Tuya's local port open across the routed subnet(s) — unicast, so
    it works over L3 (no shared L2 / broadcast needed)."""

    async def probe(ip: str) -> str | None:
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(ip, TUYA_PORT), timeout=timeout_s)
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()  # release the transport now, not at GC
            return ip
        except (OSError, TimeoutError):
            return None

    return await probe_hosts(subnet_hosts(subnets), probe, 128)


def _probe_local(dev_id: str, key: str, ip: str) -> float | None:
    """Return the protocol version that yields a valid local status at `ip` for
    this device (id+key), or None. Confirms the IP↔device match over routing."""
    import tinytuya

    for ver in _VERSIONS:
        try:
            d = tinytuya.Device(dev_id, ip, key)
            d.set_version(ver)
            d.set_socketTimeout(1.2)
            data = d.status()
            if isinstance(data, dict) and isinstance(data.get("dps"), dict):
                return ver
        except Exception:
            log.debug("tuya: local probe of %s failed", ip, exc_info=True)
            continue
    return None


async def fetch_devices(
    api_id: str, api_secret: str, region: str, subnets: list[str],
    existing: dict[str, dict] | None = None,
) -> list[dict]:
    """Query the Tuya cloud for this account's devices (with local keys + DP
    maps), then locate each on the LAN. Returns full device dicts
    {id, name, key, ip, version, dps, unmapped, online} — the adapter caches
    these (keys stay server-side) and returns a sanitised view to the UI.

    `existing` maps id -> already-configured device: those keep their stored
    ip/version and are NOT re-probed (the adapter already holds a persistent
    socket to them, so a concurrent probe would collide and read as offline).
    """
    existing = existing or {}
    import tinytuya

    loop = asyncio.get_running_loop()

    client = await loop.run_in_executor(
        None, lambda: tinytuya.Cloud(apiRegion=region, apiKey=api_id, apiSecret=api_secret)
    )
    raw = await loop.run_in_executor(None, lambda: client.getdevices(include_map=True))
    if isinstance(raw, dict) and (raw.get("Error") or raw.get("success") is False):
        raise RuntimeError(str(raw.get("Error") or raw.get("msg") or "cloud error"))
    devices = raw if isinstance(raw, list) else raw.get("result", []) if isinstance(raw, dict) else []
    if not devices:
        # tinytuya swallows the error envelope and returns [] on failure (expired
        # IoT Core subscription, no linked app account, …). Surface the real reason
        # LOUD instead of a silent "0 devices found".
        env = await loop.run_in_executor(None, lambda: client.getdevices(verbose=True))
        if isinstance(env, dict) and env.get("success") is False:
            raise RuntimeError(str(env.get("msg") or f"Tuya cloud code {env.get('code')}"))

    open_ips = await scan_open(subnets)
    log.info("tuya cloud: %d devices from cloud, %d local Tuya IPs on LAN", len(devices), len(open_ips))

    # Build the device records first. A Zigbee sub-device (behind a gateway) can't
    # be reached locally, so it's a CLOUD device — read via getstatus, keyed by code.
    recs: list[dict] = []
    for dev in devices:
        if not isinstance(dev, dict) or not dev.get("id"):
            continue
        dev_id = str(dev["id"])
        rec = {
            "id": dev_id,
            "name": str(dev.get("name") or dev_id).strip(),
            "key": str(dev.get("key") or dev.get("local_key") or ""),
        }
        if dev.get("gateway_id") or dev.get("node_id"):  # Zigbee sub-device → cloud
            st = await loop.run_in_executor(None, lambda i=dev_id: client.getstatus(i))
            res = st.get("result") if isinstance(st, dict) else None
            codes = [x.get("code") for x in res if isinstance(x, dict)] if isinstance(res, list) else []
            dps, unmapped = cloud_status_map(codes)
            rec.update(dps=dps, unmapped=unmapped, cloud=True)
        else:
            dps, unmapped = auto_dps(dev.get("mapping"))
            rec.update(dps=dps, unmapped=unmapped, cloud=False)
        recs.append(rec)

    # Locate the LOCAL devices on the LAN concurrently. Cloud devices need no IP.
    # Each local device has a unique key, so only its own device answers a status
    # probe — the probes run in parallel, bounded by the slowest single device.
    async def locate(rec: dict) -> tuple[str | None, float]:
        if rec.get("cloud"):
            return None, 3.3
        # Already configured → trust its stored ip/version; re-probing would collide
        # with the adapter's live socket and falsely read as offline.
        prev = existing.get(rec["id"])
        if prev and prev.get("ip"):
            return str(prev["ip"]), float(prev.get("version", 3.3))
        if not rec["key"]:
            return None, 3.3
        for cand in open_ips:
            ver = await loop.run_in_executor(None, _probe_local, rec["id"], rec["key"], cand)
            if ver is not None:
                return cand, ver
        return None, 3.3

    located = await asyncio.gather(*(locate(r) for r in recs))
    out: list[dict] = []
    for rec, (ip, version) in zip(recs, located, strict=True):  # gather preserves 1:1 order
        online = bool(rec.get("cloud")) or ip is not None
        out.append({**rec, "ip": ip, "version": version, "online": online})
    return out
