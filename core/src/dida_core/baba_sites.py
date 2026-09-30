"""The configured BABA installs, read the same way by everyone who needs them.

A location is three planes on one box: the state plane (NATS, which the baba
adapter mirrors), the media plane (go2rtc) and the archive plane (BABA's REST
API). The adapter connects to the first and points the wall at the second; the api's
camera proxy fetches from the second and third with the install's credentials.
Both read this one parser, so a field added or renamed cannot mean one thing to
the mirror and another to the proxy.

Stored as one encrypted JSON array under the baba adapter's `sites` key: every
entry carries secrets (NATS password, go2rtc password, peer key).
"""

from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import dataclass, fields
from urllib.parse import urlsplit

from dida_core.ids import slug


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


@dataclass(frozen=True)
class BabaSite:
    name: str
    nats_url: str
    nats_user: str = ""
    nats_password: str = ""
    go2rtc: str = ""
    go2rtc_user: str = ""
    go2rtc_password: str = ""
    api_url: str = ""
    peer_key: str = ""

    @property
    def key(self) -> str:
        return site_key(self.name)

    def media_headers(self) -> dict[str, str]:
        """Headers that authenticate a request to this install's go2rtc.

        WHO answers decides, and the two URLs say which: a go2rtc sharing an
        origin with the REST API is served THROUGH BABA's web server (`/go2rtc/*`
        on one hostname — the only shape available to a site behind an HTTP
        tunnel, which has no path for a raw port). That proxy adds go2rtc's own
        credentials itself and authenticates US with the peer key, like the rest
        of its API. A distinct origin is go2rtc itself, which takes basic auth —
        it serves every camera's RTSP credentials over the same API, so BABA gates
        it. Either way the secret lives here, never in the browser's descriptor.
        """
        if self.api_url and origin(self.go2rtc) == origin(self.api_url):
            return self.archive_headers()
        if not self.go2rtc_password:
            return {}  # that install leaves go2rtc unauthenticated
        user = self.go2rtc_user or "baba"
        token = b64encode(f"{user}:{self.go2rtc_password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def archive_headers(self) -> dict[str, str]:
        return {"X-Peer-Key": self.peer_key} if self.peer_key else {}

    def plane(self, url: str) -> str | None:
        """Which of this install's HTTP planes `url` is on — "media", "archive" —
        or None when it is on neither. Matched on the configured base including its
        path, so a tunnelled site with both planes on one hostname still tells
        `/go2rtc/*` from `/api/*`."""
        for name, base in (("media", self.go2rtc), ("archive", self.api_url)):
            base = base.rstrip("/")
            if base and url.startswith(f"{base}/"):
                return name
        return None

    def headers_for(self, url: str) -> dict[str, str]:
        match self.plane(url):
            case "media":
                return self.media_headers()
            case "archive":
                return self.archive_headers()
        return {}


_FIELDS = tuple(f.name for f in fields(BabaSite))


def site_key(name: str) -> str:
    return slug(name, default="site")


def _entries(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [d for d in arr if isinstance(d, dict)] if isinstance(arr, list) else []


def parse_sites(raw: str | None) -> list[BabaSite]:
    """The configured installs. A location needs at least its NATS URL: that is
    the plane the mirror runs on, and without it there is nothing to mirror (the
    adapter names such entries instead, see `incomplete_sites`)."""
    out: list[BabaSite] = []
    for d in _entries(raw):
        f = {k: str(d.get(k, "") or "").strip() for k in _FIELDS}
        if not f["nats_url"]:
            continue
        f["name"] = f["name"] or urlsplit(f["nats_url"]).hostname or "BABA"
        f["api_url"] = f["api_url"].rstrip("/")
        out.append(BabaSite(**f))
    return out


def incomplete_sites(raw: str | None) -> list[str]:
    """Names of stored locations that `parse_sites` drops for want of a NATS URL."""
    return [str(d.get("name") or d.get("api_url") or "?")
            for d in _entries(raw) if not str(d.get("nats_url", "")).strip()]


def site_of(sites: list[BabaSite], name: str) -> BabaSite | None:
    """The install a camera descriptor's `site` names. A descriptor without one (a
    single-location setup) belongs to the only location, and to none when there
    are several: presenting the wrong house's key to a box is worse than a 401."""
    if name:
        return next((s for s in sites if s.name == name), None)
    return sites[0] if len(sites) == 1 else None
