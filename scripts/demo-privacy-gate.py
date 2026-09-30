#!/usr/bin/env python3
"""Refuse to publish demo fixtures that still name a person, a place or a device.

Names hide behind two layers here, and both have bitten this pipeline:

  * diacritics — the place is "Coast House", so a plain "coast" pattern matches
    nothing at all;
  * double encoding — camera descriptors are JSON *inside* a JSON string, with
    ć written as \\u0107, so a flat text search never sees the field.

So this walks the structure, parses nested JSON, decodes escapes and strips
diacritics before comparing. It exits non-zero on any hit: the deploy stops and
yesterday's demo stays up, which is the correct failure.

    scripts/demo-privacy-gate.py <fixtures.json>
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import re
import sys
import unicodedata


def _load_anon() -> dict:
    """Real people/places to refuse — gitignored (see demo/anon.example.json).
    Missing config is a hard stop: a gate with no patterns would pass everything,
    the worst possible failure for a privacy gate."""
    p = pathlib.Path(__file__).resolve().parent.parent / "demo" / "anon.json"
    if not p.exists():
        raise SystemExit(f"{p} missing — the gate cannot run without the real name/place list")
    return json.loads(p.read_text())


_A = _load_anon()
PEOPLE = re.compile(r"(?<![a-z])(" + "|".join(_A["people_child"] + _A["people_adult"]) + r")(?![a-z])")
# A name glued into a display identifier ("THPaulaRoom") has letters on both sides,
# so PEOPLE misses it; its capital initial is the boundary, which lowercasing erases.
GLUED = re.compile(r"(" + "|".join(
    n.capitalize() for n in _A["people_child"] + _A["people_adult"]) + r")(?![a-z])")
PLACES = re.compile("|".join(_A["place_tokens"]))

# The wiring identifies the house as surely as its name: a private IPv4 draws the
# LAN's map (and an ESPHome node is NAMED after its address), a MAC pins real
# hardware to a vendor. refresh-demo-data.py rewrites both into ranges reserved for
# documentation, so anything still here means a value written AFTER the last
# anonymisation pass — the very leak that reached the fixtures once. The stand-ins
# are deliberately unmatched: doc space is not RFC1918, and 02:00:5E is the
# locally-administered prefix that pass mints.
PRIVATE_IP = re.compile(
    r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
REAL_MAC = re.compile(r"\b(?!02:00:5e)[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")

hits: dict[str, set[str]] = {}


def plain(text: str) -> str:
    """Decode \\uXXXX escapes, strip diacritics."""
    if "\\u" in text:
        with contextlib.suppress(UnicodeDecodeError, UnicodeEncodeError):
            text = text.encode("utf-8", "surrogatepass").decode("unicode_escape")
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def check(where: str, text: str) -> None:
    cased = plain(text)
    flat = cased.lower()
    found = {m.group(0) for m in PEOPLE.finditer(flat)}
    found |= {m.group(0) for m in GLUED.finditer(cased)}
    found |= {m.group(0) for m in PLACES.finditer(flat)}
    found |= {m.group(0) for m in PRIVATE_IP.finditer(flat)}
    found |= {m.group(0) for m in REAL_MAC.finditer(flat)}
    if found:
        hits.setdefault(where, set()).update(found)


def walk(node: object, where: str) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            check(where, str(k))
            walk(v, where)
    elif isinstance(node, list):
        for v in node:
            walk(v, where)
    elif isinstance(node, str):
        check(where, node)
        s = node.strip()
        if s.startswith(("{", "[")):          # JSON nested inside a JSON string
            with contextlib.suppress(ValueError):
                walk(json.loads(s), where)


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    for key, value in data.items():
        walk(value, key)
    if hits:
        print("REFUSING: personal names or places in fixtures:",
              {k: sorted(v) for k, v in hits.items()}, file=sys.stderr)
        return 1
    print(f"clean — {len(data)} endpoints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
