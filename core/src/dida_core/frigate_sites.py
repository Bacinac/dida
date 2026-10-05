"""The configured Frigate locations, read the same way by the frigate adapter
(MQTT + REST polling) and the api's camera proxy (login + the hosts it may
fetch from). Stored as one encrypted JSON array under the frigate adapter's
`sites` key, since each entry holds a password.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from dida_core.ids import slug


def site_key(name: str) -> str:
    return slug(name, default="site")


def camera_key(site: str, camera: str) -> str:
    return f"{site_key(site)}:{slug(camera)}"


def parse_sites(raw: str | None) -> list[dict]:
    """The configured locations. Stored as one JSON array (encrypted, since each
    holds a password) exactly like the esphome node list. A site needs at least a
    `url`; `name` defaults to the URL's host so a location always has a label."""
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for d in arr:
        if not isinstance(d, dict):
            continue
        url = str(d.get("url", "")).strip()
        if not url:
            continue
        name = str(d.get("name", "")).strip() or (urlsplit(url).hostname or url)
        out.append({
            "name": name,
            "url": url,
            "user": str(d.get("user", "")).strip(),
            "password": str(d.get("password", "")).strip(),
            "go2rtc": str(d.get("go2rtc", "")).strip(),
            "mqtt_url": str(d.get("mqtt_url", "")).strip(),
            "mqtt_user": str(d.get("mqtt_user", "")).strip(),
            "mqtt_password": str(d.get("mqtt_password", "")).strip(),
            "topic_prefix": str(d.get("topic_prefix", "")).strip() or "frigate",
            "tls": bool(d.get("tls")),
            "tls_insecure": bool(d.get("tls_insecure")),
        })
    keys = [site_key(site["name"]) for site in out]
    if len(set(keys)) != len(keys):
        raise ValueError("Frigate site names must have distinct identity keys")
    return out
