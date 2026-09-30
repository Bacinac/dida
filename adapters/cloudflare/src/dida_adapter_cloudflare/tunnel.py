"""Tunnel mode — for an installation where DIDA owns the tunnel outright.

The house routes through a host-side `services.conf` that also drives Caddy, the
router's split DNS and the Homepage tiles; there, this adapter edits that file and
a host applier does the rest. A second installation has none of that: no Caddy, no
split DNS, no applier — the connectors run in DIDA's own compose and read a
`config.yml` DIDA writes. There, THAT file is the ingress, and this module is how
it is read and written.

The file stays the single source: it is parsed back into rows on every refresh,
so there is no second copy to drift from. Rows carry the same shape as source
mode (`host` is the bare subdomain under ZONE), which is what lets one control
plane, one validator and one set of entities serve both installations.

Two of the origin's traits are implied rather than stored, exactly as the host
renderer implies them: an `https://` origin gets `noTLSVerify` (these are internal
certificates), and the `nochunk` option gets `disableChunkedEncoding` (pveproxy
streams chunked responses that cloudflared cannot proxy over HTTP/2).
"""
from __future__ import annotations

import re
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

CATCHALL = "http_status:404"

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _service_host(origin: str) -> str:
    m = re.match(r"^[a-z0-9+.-]+://([^/:]+)", origin.strip(), re.I)
    return m.group(1) if m else ""


def _host_key(host: str) -> tuple:
    """Order origin hosts the way a person reads them: .11 before .100, not the
    string order that would put .100 first and reshuffle a file nobody edited."""
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts), "")
    return (1, (), host)


def parse_config(text: str, zone: str) -> tuple[dict, list[dict]]:
    """`config.yml` → (meta, rows). The catch-all is structural, not a route, so it
    is dropped here and re-added on render; an entry without a hostname is the
    catch-all in every file cloudflared accepts."""
    doc = _yaml.load(text) or {}
    meta = {
        "tunnel": doc.get("tunnel", ""),
        "credentials-file": doc.get("credentials-file", ""),
    }
    rows: list[dict] = []
    for entry in doc.get("ingress") or []:
        if not isinstance(entry, dict):
            continue
        fqdn = str(entry.get("hostname") or "").strip()
        if not fqdn:
            continue  # the catch-all
        origin = str(entry.get("service") or "").strip()
        suffix = f".{zone}"
        host = fqdn[: -len(suffix)] if fqdn.endswith(suffix) else fqdn
        orq = entry.get("originRequest") or {}
        opts = ["nochunk"] if orq.get("disableChunkedEncoding") else []
        rows.append({
            "host": host,
            "origin": origin,
            "serve": "wan",          # a tunnel has no LAN half to serve
            "opts": opts,
            "section": None,
            "icon": None,
        })
    return meta, rows


def render_config(meta: dict, rows: list[dict], zone: str) -> str:
    """Rows → `config.yml`, grouped by the origin host with a comment header, the
    way the file has always read. Comments never reach cloudflared's parser; they
    keep the file legible to whoever opens it on the box."""
    doc = CommentedMap()
    doc["tunnel"] = meta.get("tunnel", "")
    doc["credentials-file"] = meta.get("credentials-file", "")
    ingress = CommentedSeq()
    ordered = sorted(rows, key=lambda r: (_host_key(_service_host(r["origin"])), r["host"]))
    for row in ordered:
        entry = CommentedMap()
        entry["hostname"] = f"{row['host']}.{zone}"
        entry["service"] = row["origin"]
        orq = CommentedMap()
        if row["origin"].lower().startswith("https://"):
            orq["noTLSVerify"] = True
        if "nochunk" in (row.get("opts") or []):
            orq["disableChunkedEncoding"] = True
        if orq:
            entry["originRequest"] = orq
        ingress.append(entry)
    catchall = CommentedMap()
    catchall["service"] = CATCHALL
    ingress.append(catchall)
    doc["ingress"] = ingress

    buf = StringIO()
    _yaml.dump(doc, buf)
    host_of = {f"{r['host']}.{zone}": _service_host(r["origin"]) for r in ordered}
    out: list[str] = []
    last: object = object()
    for line in buf.getvalue().split("\n"):
        m = re.match(r"^\s*- hostname:\s*(\S+)", line)
        if m:
            host = host_of.get(m.group(1).strip("'\""))
            if host != last:
                if host:
                    if out and out[-1].strip():
                        out.append("")
                    out.append(f"  # ----- {host} -----")
                    out.append("")
                last = host
        out.append(line)
    return "\n".join(out)
