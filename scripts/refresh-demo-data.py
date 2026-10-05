#!/usr/bin/env python3
"""Refresh the demo house from production, anonymised, into the dev instance.

The demo is a frozen snapshot; without this its sensors read the same numbers
forever. This pulls today's state from production (READ-ONLY), rewrites every
identifying detail, and leaves the dev database ready for `deploy/demo.sh` to
record and publish.

Everything here is idempotent and declarative: the rules live in one place, so
a rename that only exists in someone's shell history can't drift out of sync.
If production ever introduces a name the rules don't know, the privacy gate in
deploy/demo.sh refuses to publish — the pipeline fails closed.

    scripts/refresh-demo-data.py [--dry-run]

Needs: ssh to the production host, docker on this host.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import hashlib
import io
import json
import math
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from demo_history import HistoryRollbackError, replace_history


def _load_anon() -> dict:
    """The real person/place/geo mapping. Gitignored — it names real people and
    places — so a public checkout has only demo/anon.example.json. Fail loud
    rather than run the pipeline with an empty map (which would publish
    un-anonymised fixtures)."""
    p = Path(__file__).resolve().parent.parent / "demo" / "anon.json"
    if not p.exists():
        raise SystemExit(f"{p} missing — restore the gitignored demo mapping (schema in demo/anon.example.json)")
    return json.loads(p.read_text())


_A = _load_anon()
PROD = _A["prod_host"]
TABLES = ["floors", "areas", "entities", "current_state",
          "automations", "zones", "schedules"]
# entities is keyed on entity_id; the rest carry a surrogate identity column.
IDENTITY = {"floors", "areas", "automations", "zones", "schedules"}
GENERATED = {"entities": ["voice_effective"]}   # COPY cannot write generated columns

# ── anonymisation rules (loaded from the gitignored demo/anon.json) ─────────
# Children share one placeholder word on purpose (the demo must not hint at how
# many there are); adults and glued-identifier forms are handled the same way.
PEOPLE_CHILD = _A["people_child"]
PEOPLE_ADULT = _A["people_adult"]
GLUED = _A["glued"]


def camel(names: list[str]) -> str:
    """A name glued into a display identifier ("THNikaRoom", "LIGHT-NikaRoom-OFF")
    has letters on both sides, so the whole-token rule misses it; its capital
    initial is the boundary. Match the result case-sensitively."""
    return "|".join(n.capitalize() for n in names)


# Places, hostnames and camera-descriptor site labels — diacritic and double-
# encoded forms are all listed in the config, since value::text hides them.
PLACES = _A["places"]
ZONE_RENAME = _A["zone_rename"]
ZONE_PREFIX = _A["zone_prefix"]
# Rigid geo transform: translate + rotate about the old home so clusters and
# distances survive but the constellation can't be matched to a real address.
GEO_FROM = tuple(_A["geo_from"])
GEO_TO = tuple(_A["geo_to"])
GEO_ROT_DEG = _A["geo_rot_deg"]
ADMIN_AT = tuple(_A["admin_at"])

# app_settings holds presentation AND credentials side by side (API keys, the
# panel token, VAPID private key, network topology). Copy an explicit allowlist,
# never the table: a demo that mirrors it would publish the lot.
SETTINGS_ALLOW = [
    "camera_order", "camera_spans",   # the wall layout the owner arranged
    "panel_config", "entry_controls", "volume_presets",
    "announce_lang", "radio_player", "radio_current_id", "presence_hidden",
]


def sh(cmd: list[str], **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        # psql's own message is the useful part; a bare CalledProcessError hides it
        raise RuntimeError(f"{' '.join(cmd[:4])}… exited {r.returncode}\n{r.stderr.strip()[:800]}")
    return r.stdout


def prod_psql(sql: str) -> str:
    return sh(["ssh", "-o", "BatchMode=yes", PROD,
               f"docker exec dida-postgres psql -U dida -d dida -tA -c {sql!r}"])


def dev_psql(sql: str) -> str:
    return sh(["docker", "exec", "-i", "dida-postgres",
               "psql", "-U", "dida", "-d", "dida", "-tA", "-v", "ON_ERROR_STOP=1"],
              input=sql)


# ── identifiers ────────────────────────────────────────────────────────────
# MAC addresses name real hardware, private IPs draw the house's network map,
# and device UUIDs tie the demo back to a live installation. All three are
# rewritten deterministically (same input → same output on every run, so the
# demo stays stable) into ranges reserved for documentation.
SALT = "dida-public-demo"
MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
IP_RE = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
# The same private IP with dots swapped for underscores. The esphome adapter bakes
# a device's IP straight into its entity_id (`esphome:203_0_113_32`), so the LAN
# subnet rides the IDENTITY, not a value — and the dotted regex above never sees
# it. Match the underscore form and map it to the SAME doc address as the dotted
# one, so the two stay consistent wherever both appear.
UIP_RE = re.compile(r"\b\d{1,3}_\d{1,3}_\d{1,3}_\d{1,3}\b")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# TEST-NET blocks (RFC 5737) — reserved for documentation, routable nowhere.
DOC_NETS = ["192.0.2", "198.51.100", "203.0.113"]


# 02:… is the locally-administered bit: never a real vendor's block. The prefix
# also marks a MAC as already scrubbed, for the scrub and the privacy gate alike.
FAKE_MAC_PREFIX = "02:00:5E:"


def _digest(value: str, n: int) -> int:
    return int(hashlib.sha256((SALT + value).encode()).hexdigest()[:n], 16)


# The same address written without separators. Shelly bakes it into a device's
# own name (`shelly1g4-<mac>`), where it rides the entity_id and MAC_RE never
# sees it. A UUID's last group is also twelve hex digits, so UUIDs are taken out
# before looking; and twelve bare digits are far likelier a number than a MAC —
# rewriting one inside jsonb would break the value — so a letter is required.
BARE_MAC_RE = re.compile(r"(?<![0-9A-Za-z])(?=[0-9]*[a-fA-F])[0-9a-fA-F]{12}(?![0-9A-Za-z])")


def found_macs(text: str) -> set[str]:
    """Every MAC in `text`, with or without separators, stand-ins left out."""
    stand_in = FAKE_MAC_PREFIX.replace(":", "").lower()
    return ({m for m in MAC_RE.findall(text) if not m.upper().startswith(FAKE_MAC_PREFIX)}
            | {m for m in BARE_MAC_RE.findall(UUID_RE.sub(" ", text))
               if not m.lower().startswith(stand_in)})


def fake_mac(mac: str) -> str:
    """The stand-in, spelled the way `mac` was: one device written both ways
    must map to one stand-in."""
    bare = mac.replace(":", "")
    h = _digest(":".join(bare[i:i + 2] for i in range(0, 12, 2)).lower(), 8)
    fake = f"{FAKE_MAC_PREFIX}{(h >> 16) & 0xFF:02X}:{(h >> 8) & 0xFF:02X}:{h & 0xFF:02X}"
    if ":" in mac:
        return fake
    fake = fake.replace(":", "")
    return fake if mac.isupper() else fake.lower()


def remember() -> None:
    """Write the stand-ins handed out back to demo/anon.json, so the next run
    deals the same ones."""
    anon_path = Path(__file__).resolve().parent.parent / "demo" / "anon.json"
    anon_path.write_text(json.dumps(_A, indent=2, ensure_ascii=False) + "\n")


class StandIns:
    """One stand-in per real identifier that carries a person's name, remembered
    across runs (demo/anon.json "entity_map" / "device_map").

    Dev runs the real adapters, so a device the rewrite renamed comes straight
    back under its real id: the cast adapter reports the child's-room TV as
    `tv_<name>_room` on every status change. Numbered against whatever the table
    held at the time, that one TV drew a fresh stand-in on every run — ten copies
    in three days. Remembered, the same real id always gets the same stand-in, and
    a stand-in is never handed to a second identity, so a row already holding it
    is the same device."""

    def __init__(self, persisted: dict[str, str], taken: set[str]) -> None:
        self._map = persisted
        self._taken = taken | set(persisted.values())

    def __call__(self, real: str) -> str:
        if real not in self._map:
            new = re.sub(f"(?i)(?<![a-z])({'|'.join(PEOPLE_CHILD)})(?![a-z])", "child", real)
            new = re.sub(f"(?i)(?<![a-z])({'|'.join(PEOPLE_ADULT)})(?![a-z])", "resident", new)
            base, n = new, 1
            while new in self._taken:
                n += 1
                new = f"{base}{n}"
            self._map[real] = new
            self._taken.add(new)
        return self._map[real]


class DocAddresses:
    """Hand out one documentation address per real private address.

    Injective by construction — which two earlier schemes were not. Hashing the
    host octet collapsed distinct hosts onto one address; hashing only the /24 and
    keeping the host octet held only while every subnet drew a different doc net,
    and there are three of those: the day the house had a fourth VLAN,
    192.168.7.21 and 192.168.8.21 both landed on 198.51.100.21 and the
    injectivity check refused to run at all.

    The map is REMEMBERED (demo/anon.json "ip_map") rather than derived from the
    run's input, because the input is not stable: the scrub runs over whatever
    the dev database holds, dev's live adapters keep writing real identifiers
    back, and a previously-scrubbed row keeps its stand-in forever. Sequential
    assignment over that shifting set re-dealt the addresses every run — until a
    fresh key drew 192.0.2.1 while a leftover row already carried it, and the
    devices PK refused the merge. Addresses something in the database already
    uses are equally off-limits, for the same reason: a stand-in that exists is
    taken, whoever it once stood for.
    """

    def __init__(self, persisted: dict[str, str] | None = None,
                 taken: set[str] | None = None) -> None:
        self._map: dict[str, str] = dict(persisted or {})
        self._pool = [f"{net}.{host}" for net in DOC_NETS for host in range(1, 255)]
        self._used: set[str] = set(self._map.values()) | (taken or set())

    def __call__(self, ip: str) -> str:
        if ip not in self._map:
            nxt = next((a for a in self._pool if a not in self._used), None)
            if nxt is None:
                raise RuntimeError(f"out of documentation addresses ({len(self._pool)} used)")
            self._map[ip] = nxt
            self._used.add(nxt)
        return self._map[ip]

    @property
    def map(self) -> dict[str, str]:
        return dict(self._map)


def fake_uuid(u: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, SALT + u.lower()))


def _found_identifiers(uuid_stand_ins: set[str]) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """(macs, dotted IPs, underscored IPs, UUIDs, stand-in addresses already taken)."""
    macs: set[str] = set()
    ips: set[str] = set()
    uips: set[str] = set()
    uuids: set[str] = set()
    # Stand-ins already present in the database are prior runs' outputs living
    # in rows the scrub no longer recognises as real. They must never be handed
    # out again.
    doc_re = re.compile(r"\b(?:" + "|".join(n.replace(".", r"\.") for n in DOC_NETS)
                        + r")\.\d{1,3}\b")
    taken: set[str] = set()
    for table, col, _ in text_columns():
        rows = dev_psql(f"SELECT DISTINCT {col}::text FROM {table} WHERE {col} IS NOT NULL "  # noqa: S608
                        f"AND ({col}::text ~ '([0-9a-fA-F]{{2}}:){{5}}'"
                        f" OR {col}::text ~ '[0-9a-fA-F]{{12}}'"
                        f" OR {col}::text ~ '(10|192\\.168|172|192\\.0\\.2|198\\.51\\.100|203\\.0\\.113)\\.'"
                        f" OR {col}::text ~ '[0-9]+_[0-9]+_[0-9]+_[0-9]+'"
                        f" OR {col}::text ~ '[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-')")
        for line in rows.splitlines():
            macs.update(found_macs(line))
            ips.update(IP_RE.findall(line))
            # only the underscore forms that ARE private IPs
            uips.update(m for m in UIP_RE.findall(line) if IP_RE.search(m.replace("_", ".")))
            uuids.update(u for u in UUID_RE.findall(line) if u.lower() not in uuid_stand_ins)
            taken.update(doc_re.findall(line))
    return macs, ips, uips, uuids, taken


def _assert_injective(mapping: dict[str, str]) -> None:
    """Fail loud on any two identifiers colliding onto one replacement: a merge would
    silently fuse two entities (and violate current_state's PK), which is exactly
    the kind of quiet data corruption this refresh must never ship."""
    collisions: dict[str, list[str]] = {}
    for old, new in mapping.items():
        collisions.setdefault(new, []).append(old)
    merged = {new: olds for new, olds in collisions.items() if len(olds) > 1}
    if merged:
        raise RuntimeError(f"identifier scrub is not injective — would merge identities: {merged}")


def _rewrite_statements(mapping: dict[str, str]) -> list[str]:
    stmts = ["BEGIN;"]
    cascaded = cascaded_columns()
    # Longest identifier first: replace() is plain substring, and running
    # '10.0.0.1' before '10.0.0.11' rewrites the longer key's prefix
    # and strands it half-scrubbed under an address nobody allocated.
    ordered = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    # Dev's live adapters re-register the real identifier between runs, while the
    # previous run's row keeps the stand-in — the same identity spelled two ways.
    # Rewriting the real spelling then collides with the leftover on the unique
    # key. The leftover is the stale copy by construction: drop it and let the
    # freshly scrubbed row take the address. Injectivity (checked above) makes
    # the post-replace image unambiguous, so this can never delete a stranger.
    for table, col, others in unique_text_columns():
        if (table, col) in cascaded:
            continue
        same_key = "".join(f" AND a.{o} IS NOT DISTINCT FROM b.{o}" for o in others)
        stmts += [
            f"DELETE FROM {table} a USING {table} b WHERE a.{col} <> b.{col} "  # noqa: S608
            f"AND strpos(b.{col}, '{old}') > 0 "
            f"AND a.{col} = replace(b.{col}, '{old}', '{new}'){same_key};"
            for old, new in ordered
        ]
    for table, col, is_json in text_columns():
        if (table, col) in cascaded:
            continue
        cast = "jsonb" if is_json else "text"
        stmts += [
            f"UPDATE {table} SET {col} = replace({col}::text, '{old}', '{new}')::{cast} "  # noqa: S608
            f"WHERE strpos({col}::text, '{old}') > 0;"
            for old, new in ordered
        ]
    stmts.append("COMMIT;")
    return stmts


def _rename_snapshots(mapping: dict[str, str]) -> None:
    """The curated camera stills are named after the camera's full entity id
    (baba:<uuid>.jpg, frigate:<slug>.jpg). Only the part after the namespace
    is an identifier worth rewriting — map it with the same table so the demo
    still finds the file, and leave slug-named sources alone."""
    snaps = pathlib.Path(__file__).resolve().parent.parent / "demo-snapshots"
    for f in snaps.glob("*.jpg"):
        prefix, sep, ident = f.stem.rpartition(":")
        if not sep:
            print(f"  ! {f.name}: nema <adapter>:<id> oblik, preskacem")
            continue
        new = mapping.get(ident)
        if new and new != ident:
            f.rename(f.with_name(f"{prefix}:{new}.jpg"))


def scrub_identifiers() -> None:
    """Rewrite every MAC, private IP and UUID across the database."""
    # Nor may a stand-in be scrubbed again. A UUID has no reserved range to tell
    # it by (production holds a real version-8 one), so the scrub remembers what
    # it handed out. Re-hashing its own output moved the child's-room TV's key 35
    # times in three days, left the curated camera stills behind under ids no
    # camera had any more, and put A→B and B→C into one mapping, whose outcome
    # then depended on the order of the UPDATEs.
    uuid_map: dict[str, str] = _A.setdefault("uuid_map", {})
    macs, ips, uips, uuids, taken = _found_identifiers(set(uuid_map.values()))

    # The dotted and underscored spellings of one address deliberately resolve
    # to the SAME stand-in; the assignments themselves are remembered across
    # runs (see DocAddresses) and written back below.
    doc = DocAddresses(_A.get("ip_map"), taken)
    mapping: dict[str, str] = {m: fake_mac(m) for m in sorted(macs)}
    for m in sorted(ips):
        mapping[m] = doc(m)
    for m in sorted(uips):
        mapping[m] = doc(m.replace("_", ".")).replace(".", "_")
    for m in sorted(uuids):
        mapping[m] = uuid_map.setdefault(m.lower(), fake_uuid(m))
    if not mapping:
        return
    _assert_injective(mapping)

    _A["ip_map"] = doc.map
    remember()
    dev_psql("\n".join(_rewrite_statements(mapping)))
    _rename_snapshots(mapping)
    print(f"scrubbed {len(mapping)} identifiers")


def geo(lat: float, lon: float) -> tuple[float, float]:
    dx = (lon - GEO_FROM[1]) * math.cos(math.radians(GEO_FROM[0])) * 111320.0
    dy = (lat - GEO_FROM[0]) * 110540.0
    t = math.radians(GEO_ROT_DEG)
    rx, ry = dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)
    return (GEO_TO[0] + ry / 110540.0,
            GEO_TO[1] + rx / (math.cos(math.radians(GEO_TO[0])) * 111320.0))


def copy_tables(work: Path) -> None:
    """Production → dev, table by table, preserving ids so references survive."""
    # COPY writes identity values as given (OVERRIDING SYSTEM VALUE), so ids
    # carry over without touching the column definition.
    stmts = ["BEGIN;", "TRUNCATE current_state;", "TRUNCATE virtual_entities;"]
    for t in ("entities", "areas", "floors", "automations", "zones", "schedules"):
        stmts.append(f"DELETE FROM {t};")  # noqa: S608

    for t in TABLES:
        raw = sh(["ssh", "-o", "BatchMode=yes", PROD,
                  f"docker exec dida-postgres psql -U dida -d dida -c "  # noqa: S608
                  f"\"\\copy (SELECT * FROM {t}) TO STDOUT WITH CSV HEADER\""])
        lines = raw.splitlines()
        header = lines[0].split(",")
        drop = [header.index(c) for c in GENERATED.get(t, []) if c in header]
        if drop:
            rows = list(csv.reader(io.StringIO(raw)))
            keep = [i for i in range(len(header)) if i not in drop]
            out = io.StringIO()
            csv.writer(out).writerows([[r[i] for i in keep] for r in rows])
            raw, header = out.getvalue(), [header[i] for i in keep]
        (work / f"{t}.csv").write_text(raw)
        sh(["docker", "cp", str(work / f"{t}.csv"), f"dida-postgres:/tmp/demo-{t}.csv"])
        stmts.append(f'COPY {t} ({", ".join(header)}) FROM \'/tmp/demo-{t}.csv\' WITH CSV HEADER;')

    for t in IDENTITY:
        stmts.append(f"SELECT setval(pg_get_serial_sequence('{t}','id'), "  # noqa: S608
                     f"COALESCE((SELECT max(id) FROM {t}),1));")
    stmts.append("COMMIT;")
    dev_psql("\n".join(stmts))


def cascaded_columns() -> set[tuple[str, str]]:
    """Columns a foreign key keeps in step with their parent (ON UPDATE CASCADE).
    Rewriting the parent moves them; rewriting them first would point at a parent
    that does not exist yet."""
    rows = dev_psql(
        "SELECT kcu.table_name || '|' || kcu.column_name "
        "FROM information_schema.referential_constraints rc "
        "JOIN information_schema.key_column_usage kcu USING (constraint_schema, constraint_name) "
        "WHERE rc.constraint_schema = 'public' AND rc.update_rule = 'CASCADE'").splitlines()
    return {(t, c) for t, _, c in (r.partition("|") for r in rows if r.strip())}


def unique_text_columns() -> list[tuple[str, str, list[str]]]:
    """Each text column of a unique index, with the index's other columns. Read from
    pg_index rather than information_schema: a partial unique index (devices'
    (adapter, native_key) WHERE native_key IS NOT NULL) is not a constraint and
    never shows up there."""
    rows = dev_psql(
        "SELECT DISTINCT t.relname || '|' || a.attname || '|' || coalesce(("
        "  SELECT string_agg(o.attname, ',' ORDER BY o.attname) FROM pg_attribute o"
        "  WHERE o.attrelid = i.indrelid AND o.attnum = ANY(i.indkey) AND o.attnum <> a.attnum), '') "
        "FROM pg_index i "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public' "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indisunique AND a.atttypid IN ('text'::regtype, 'varchar'::regtype)").splitlines()
    out = []
    for r in rows:
        if r.strip():
            t, c, others = r.split("|")
            out.append((t, c, [o for o in others.split(",") if o]))
    return out


def text_columns() -> list[tuple[str, str, bool]]:
    rows = dev_psql(
        "SELECT table_name || '|' || column_name || '|' || data_type "
        "FROM information_schema.columns WHERE table_schema='public' "
        "AND data_type IN ('text','character varying','jsonb','json')").splitlines()
    out = []
    for r in rows:
        if not r.strip():
            continue
        t, c, d = r.split("|")
        out.append((t, c, d == "jsonb"))
    return out


def anonymise() -> None:
    stmts = ["BEGIN;"]

    # 0. the dev instance's own URL settings name the family domain (app_url /
    # public_url exist for push and share links, neither of which the demo
    # recording performs), and so does the Cloudflare adapter's DNS zone — drop
    # them rather than rewrite them, there is no demo value that would be true.
    stmts.append("DELETE FROM app_settings WHERE key IN ('app_url', 'public_url');")
    stmts.append("DELETE FROM adapter_config WHERE adapter = 'cloudflare' AND key = 'zone';")

    # 0c. A reverse-DNS application id carries the family domain with its labels
    # inverted, so `_host_re` — which reads forward — never sees it. Left alone it
    # reaches the demo inside ordinary state: the living-room TV publishes its
    # installed-app list, and one sideloaded package there refused the whole
    # publish. Invert the reserved stand-in the same way rather than dropping the
    # entry, so the list stays honest about there being a sideloaded app.
    for d in _A.get("domains", []):
        rev = ".".join(reversed(d.split(".")))
        for t, c, is_json in text_columns():
            stmts.append(
                f"UPDATE {t} SET {c} = replace({c}::text, '{rev}', 'com.example')"  # noqa: S608
                f"{'::jsonb' if is_json else ''} WHERE {c}::text LIKE '%{rev}%';")

    # 1. glued forms first — they are substrings, so token rules would miss them
    for old, new in GLUED.items():
        for col in ("entity_id", "device_key"):
            stmts.append(f"UPDATE entities SET {col} = replace({col}, '{old}', '{new}') "  # noqa: S608
                         f"WHERE {col} LIKE '%{old}%';")

    dev_psql("\n".join([*stmts, "COMMIT;"]))

    # 2. identifiers get their own pass, WITH numbering: three sibling trackers
    # (one per child) all rewrite to the same word and would collide on the
    # primary key. Text columns must not be treated this way — a display name
    # SHOULD read "Child" for all of them.
    #
    # A stand-in row that already exists is the earlier copy of the same device
    # (StandIns never hands one identity's stand-in to another): the real-named
    # row beside it is dev's adapter reporting that device again. The stand-in
    # keeps production's metadata, so the live copy is the one that goes.
    stmts = ["BEGIN;"]
    ids = [r for r in dev_psql(
        "SELECT entity_id FROM entities WHERE entity_id ~* "  # noqa: S608
        f"'(?<![a-z])({'|'.join(PEOPLE_CHILD + PEOPLE_ADULT)})(?![a-z])' ORDER BY entity_id"
    ).splitlines() if r.strip()]
    existing = {r for r in dev_psql("SELECT entity_id FROM entities").splitlines() if r.strip()}
    entity_ids = StandIns(_A.setdefault("entity_map", {}), existing)
    for old in ids:
        new = entity_ids(old)
        if new in existing:
            stmts.append(f"DELETE FROM entities WHERE entity_id='{old}';")  # noqa: S608
        else:
            stmts.append(f"UPDATE entities SET entity_id='{new}' WHERE entity_id='{old}';")  # noqa: S608
            existing.add(new)

    # 2b. device_key is the ADAPTER's key for the device, never the entity_id: a cast
    # speaker is entity `cast:tv_x` carrying key `tv_x`. Matching it against entity_id
    # values cannot fire, and once the entity_id itself is clean the row stops being
    # selected at all — so a name in the key survived every refresh, unreachable, and
    # the gate refused the deploy with no way to clear it. Keyed on its own value, and
    # numbered like the pass above because devices.device_key is unique.
    people = "|".join(PEOPLE_CHILD + PEOPLE_ADULT)
    keys = [r for r in dev_psql(
        f"SELECT device_key FROM entities WHERE device_key ~* '(?<![a-z])({people})(?![a-z])' "  # noqa: S608
        f"UNION SELECT device_key FROM devices WHERE device_key ~* '(?<![a-z])({people})(?![a-z])' "
        "ORDER BY 1"
    ).splitlines() if r.strip()]
    device_rows = {r for r in dev_psql("SELECT device_key FROM devices").splitlines() if r.strip()}
    device_keys = StandIns(_A.setdefault("device_map", {}), device_rows | {
        r for r in dev_psql("SELECT device_key FROM entities WHERE device_key IS NOT NULL"
                            ).splitlines() if r.strip()})
    for old in keys:
        new = device_keys(old)
        o, w = old.replace("'", "''"), new.replace("'", "''")
        stmts.append(f"UPDATE entities SET device_key='{w}' WHERE device_key='{o}';")  # noqa: S608
        if new in device_rows:
            stmts.append(f"DELETE FROM devices WHERE device_key='{o}';")  # noqa: S608
        else:
            stmts.append(f"UPDATE devices SET device_key='{w}' WHERE device_key='{o}';")  # noqa: S608
            device_rows.add(new)

    # 3. whole-token people in DISPLAY text — never in identifiers, handled above
    child = "|".join(PEOPLE_CHILD)
    adult = "|".join(PEOPLE_ADULT)
    for t, c, is_json in text_columns():
        if c in ("entity_id", "device_key"):
            continue
        cast = "jsonb" if is_json else "text"
        for pat, repl in ((child, "child"), (adult, "Resident")):
            stmts.append(
                f"UPDATE {t} SET {c} = regexp_replace({c}::text, "  # noqa: S608
                f"'(?i)(?<![a-z])({pat})(?![a-z])', '{repl}', 'g')::{cast} "
                f"WHERE {c}::text ~* '(?<![a-z])({pat})(?![a-z])';")
        for pat, repl in ((camel(PEOPLE_CHILD), "Child"), (camel(PEOPLE_ADULT), "Resident")):
            stmts.append(
                f"UPDATE {t} SET {c} = regexp_replace({c}::text, "  # noqa: S608
                f"'({pat})(?![a-z])', '{repl}', 'g')::{cast} "
                f"WHERE {c}::text ~ '({pat})(?![a-z])';")

    # 3b. A parked vehicle BABA has not matched to a person is named by its plate as
    # read. An empty name means "occupied, not yet read" and stays empty.
    name = "CASE WHEN value #>> '{}' LIKE '{%' THEN (value #>> '{}')::jsonb ->> 'name' END"
    stmts.append(
        "UPDATE current_state SET value = "  # noqa: S608
        "to_jsonb(jsonb_set((value #>> '{}')::jsonb, '{name}', '\"Guest\"')::text) "
        f"WHERE capability = 'parked_vehicle' AND {name} NOT IN ('', 'Resident', 'Child', 'child');")

    # 4. places, hostnames and camera site labels.
    # These live inside DOUBLE-ENCODED values: the jsonb holds a *string* whose
    # content is itself a JSON document, with ć written as the six characters
    # \\u0107. Casting with ::text doubles every backslash, so the obvious
    # replace() never matches — extract the decoded document with #>> '{}',
    # substitute there, and wrap it back up. replace() is literal (unlike LIKE,
    # where a backslash is an escape character), which is exactly what we want.
    for old, new in PLACES.items():
        o = old.replace("'", "''")
        stmts.append(
            f"UPDATE current_state SET value = to_jsonb(replace(value #>> '{{}}', '{o}', '{new}')) "  # noqa: S608
            f"WHERE jsonb_typeof(value) = 'string' AND strpos(value #>> '{{}}', '{o}') > 0;")

    # 5. after step 4, so hostnames keep their meaningful stand-ins
    stmts += _place_word_statements()

    stmts.append("COMMIT;")
    dev_psql("\n".join(stmts))
    remember()


def _place_word_statements() -> list[str]:
    """A place standing as a word of its own, which neither a hostname entry in
    `places` nor the geo move reaches: a peer installation is named after where it
    stands, so the place rides its devices' names and keys, the entity_ids built
    from those, and its cameras' site label. A renamed identifier that already
    exists is an earlier run's copy of the same device; the live re-report goes,
    as in anonymise() step 2."""
    stmts: list[str] = []
    cascaded = cascaded_columns()
    unique = {(t, c): others for t, c, others in unique_text_columns()}
    for old, new in _A["place_words"].items():
        for word, stand_in in ((old, new), (old.capitalize(), new.capitalize()), (old.upper(), new.upper())):
            pat = f"(?<![A-Za-z]){word}(?![A-Za-z])".replace("'", "''")
            w = stand_in.replace("'", "''")
            for t, c, is_json in text_columns():
                if (t, c) in cascaded:
                    continue
                if (t, c) in unique:
                    same_key = "".join(f" AND a.{o} IS NOT DISTINCT FROM b.{o}" for o in unique[(t, c)])
                    stmts.append(
                        f"DELETE FROM {t} b USING {t} a WHERE b.{c} ~ '{pat}' "  # noqa: S608
                        f"AND a.{c} = regexp_replace(b.{c}, '{pat}', '{w}', 'g'){same_key};")
                cast = "jsonb" if is_json else "text"
                stmts.append(
                    f"UPDATE {t} SET {c} = regexp_replace({c}::text, '{pat}', '{w}', 'g')::{cast} "  # noqa: S608
                    f"WHERE {c}::text ~ '{pat}';")
    return stmts


def move_the_world() -> None:
    """Zones and trackers get new names and new coordinates."""
    rows = [r for r in dev_psql(
        "SELECT id || '|' || name || '|' || latitude || '|' || longitude FROM zones ORDER BY id"
    ).splitlines() if r.strip()]
    seen: dict[str, int] = {}
    stmts = ["BEGIN;"]
    for r in rows:
        zid, name, lat, lon = r.split("|")
        base = ZONE_RENAME.get(name)
        if base is None:
            base = next((v for p, v in ZONE_PREFIX.items() if name.startswith(p)), "Place")
        seen[base] = seen.get(base, 0) + 1
        nname = base if seen[base] == 1 else f"{base} {seen[base]}"
        nlat, nlon = geo(float(lat), float(lon))
        stmts.append(f"UPDATE zones SET name='{nname}', latitude={nlat:.6f}, longitude={nlon:.6f} "  # noqa: S608
                     f"WHERE id={zid};")

    pts: dict[str, dict[str, float]] = {}
    for r in dev_psql("SELECT entity_id || '|' || capability || '|' || value::text FROM current_state "
                      "WHERE capability IN ('latitude','longitude')").splitlines():
        if not r.strip():
            continue
        eid, cap, val = r.split("|")
        with contextlib.suppress(ValueError):
            pts.setdefault(eid, {})[cap] = float(val)
    for eid, p in pts.items():
        if "latitude" not in p or "longitude" not in p:
            continue
        nlat, nlon = ADMIN_AT if eid == "presence:admin" else geo(p["latitude"], p["longitude"])
        stmts.append(f"UPDATE current_state SET value='{nlat:.6f}'::jsonb "  # noqa: S608
                     f"WHERE entity_id='{eid}' AND capability='latitude';")
        stmts.append(f"UPDATE current_state SET value='{nlon:.6f}'::jsonb "  # noqa: S608
                     f"WHERE entity_id='{eid}' AND capability='longitude';")

    # the stadium zone the admin sits in, and his textual location
    stmts.append("DELETE FROM zones WHERE name='Poljud';")
    stmts.append("INSERT INTO zones (name, latitude, longitude, radius_m, is_home) "
                 "VALUES ('Poljud', 43.519700, 16.430800, 140, false);")
    stmts.append("UPDATE current_state SET value='\"Poljud\"'::jsonb "
                 "WHERE entity_id='presence:admin' AND capability='location';")
    stmts.append("COMMIT;")
    dev_psql("\n".join(stmts))


def copy_settings() -> None:
    """Presentation settings only (see SETTINGS_ALLOW). Must run BEFORE the
    identifier scrub: the saved camera layout lists entity ids, and if it is
    copied afterwards it keeps the real UUIDs and matches nothing."""
    for key in SETTINGS_ALLOW:
        val = prod_psql(f"SELECT value FROM app_settings WHERE key = '{key}'").strip()  # noqa: S608
        if not val:
            continue
        v = val.replace("'", "''")
        dev_psql(f"INSERT INTO app_settings (key, value) VALUES ('{key}', '{v}') "  # noqa: S608
                 f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;")


def _prod_history(sql: str) -> bytes:
    command = shlex.join(["docker", "exec", "dida-clickhouse", "clickhouse-client", "-q", sql])
    return subprocess.run(["ssh", "-o", "BatchMode=yes", PROD, command],
                          capture_output=True, check=True).stdout


def _dev_history(sql: str) -> str:
    return sh(["docker", "exec", "dida-clickhouse", "clickhouse-client", "--format", "TSVRaw", "-q", sql])


def _insert_history(table: str, data: bytes) -> None:
    subprocess.run(["docker", "exec", "-i", "dida-clickhouse", "clickhouse-client", "-q",
                    f"INSERT INTO dida.{table} FORMAT Native"], input=data, capture_output=True, check=True)


@contextlib.contextmanager
def _pause_history_writer():
    running = sh(["docker", "inspect", "--format", "{{.State.Running}}", "dida-engine"]).strip() == "true"
    resume = True
    try:
        if running:
            sh(["docker", "stop", "dida-engine"])
        yield
    except HistoryRollbackError:
        resume = False
        raise
    finally:
        if running and resume:
            sh(["docker", "start", "dida-engine"])


def copy_history(days: int = 8) -> None:
    """Replace the demo's energy window without replaying it through the rollup views."""
    if type(days) is not int or days < 1:
        raise ValueError("history days must be a positive integer")
    lock_path = Path(__file__).resolve().parent.parent / "state" / ".demo-history.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cutoff = int(_prod_history("SELECT toUnixTimestamp(toStartOfHour(now('UTC')))").strip())
        snapshots = []
        for table, column, window in (("state_history_1h", "bucket", days),
                                      ("state_history", "ts", min(days, 3))):
            snapshots.append(_prod_history(
                f"SELECT * FROM dida.{table} WHERE capability IN ('energy','power') "  # noqa: S608
                f"AND {column} >= toDateTime({cutoff}) - INTERVAL {window} DAY "
                f"AND {column} < toDateTime({cutoff}) FORMAT Native"))
        replace_history(_dev_history, _insert_history, *snapshots, _pause_history_writer)

    # point the dashboard at meters that actually report (production's own config
    # names plugs that have gone quiet, which would render an empty chart)
    dev_psql(
        "INSERT INTO app_settings (key, value) VALUES ('energy_config', "
        "'{\"iammeter:meter:import_energy\":\"grid_import\","
        " \"iammeter:meter:export_energy\":\"grid_export\","
        " \"solar:inverter:energy_total\":\"solar\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;")


def _host_re() -> re.Pattern:
    """Matches `<label>.<family domain>` and the bare domain itself."""
    doms = "|".join(re.escape(d) for d in _A.get("domains", []))
    return re.compile(rf"\b(?:[a-z0-9-]+\.)*(?:{doms})\b", re.I) if doms else re.compile(r"(?!)")


def _demo_host(host: str) -> str:
    """A stable, meaningless stand-in under a reserved domain (RFC 2606 `.example`),
    keeping the label so the fixture still reads like a host and distinct hosts stay
    distinct. The label is kept only when it says nothing about the family — a label
    that is itself a place token is replaced by its hash, or the rewrite would leak
    the very word the gate refuses."""
    label = host.split(".")[0].lower()
    if not label or any(tok in label for tok in _A["place_tokens"]):
        label = "host" + hashlib.sha256(host.encode()).hexdigest()[:6]
    return f"{label}.example"


def _drop_named_alerts(fixtures: dict) -> int:
    """Dev runs the real adapters, so a device reappears under its real name between
    refreshes and an alert about it reaches /system/alerts — from ClickHouse or from
    the evaluator's memory, where no database pass reaches. Scrubbing ClickHouse
    before recording lost a race: renaming the device resolves its alert, and the
    evaluator writes that `resolved` row under the old name after the scrub. Such
    alerts are dev's own incidents, not demo material, so they are dropped here,
    from what was actually recorded."""
    people = re.compile(rf"(?i)(?<![a-z])({'|'.join(PEOPLE_CHILD + PEOPLE_ADULT)})(?![a-z])")
    glued = re.compile(rf"({camel(PEOPLE_CHILD + PEOPLE_ADULT)})(?![a-z])")
    dropped = 0
    for key, body in fixtures.items():
        if key.split("?", 1)[0] != "/system/alerts" or not isinstance(body, dict):
            continue
        for part in ("active", "history"):
            rows = body.get(part) or []
            keep = [r for r in rows if not any(
                p.search(json.dumps(r, ensure_ascii=False)) for p in (people, glued))]
            dropped += len(rows) - len(keep)
            body[part] = keep
    return dropped


def scrub_fixtures(path: Path) -> None:
    """Rewrite identifiers that never lived in the database out of recorded fixtures.

    An adapter's live status echoes the host it is connected to right now, so a
    fixture can carry a real LAN address no DB pass ever saw (measured live:
    /adapters and /system/stats naming the AVR). Scope is deliberately only what
    the privacy gate refuses — private IPs and real MACs — because everything
    else in the fixtures is already the DB scrub's output, and re-mapping those
    would break their agreement with snapshot filenames and floorplan configs.
    The persisted ip_map keeps one stand-in per host across both passes."""
    fixtures = json.loads(path.read_text())
    dropped = _drop_named_alerts(fixtures)
    text = json.dumps(fixtures)
    doc_re = re.compile(r"\b(?:" + "|".join(n.replace(".", r"\.") for n in DOC_NETS)
                        + r")\.\d{1,3}\b")
    doc = DocAddresses(_A.get("ip_map"), set(doc_re.findall(text)))
    repl: dict[str, str] = {m: fake_mac(m) for m in found_macs(text)}
    # Any host under a family domain, not just the ones someone remembered to list
    # in `places`. An adapter's live status text is generated at runtime and never
    # passes a DB scrub, so a URL inside it reaches the fixtures verbatim (measured:
    # a peer-adapter alert quoting https://<host>.<domain>/api/auth/login refused the
    # whole publish). The explicit `places` entries still win — they carry meaning
    # ("coast-nvr"); this only catches whatever they don't name, so a new hostname
    # can't silently become the next blocked demo.
    for host in set(_host_re().findall(text)):
        if host not in _A["places"]:
            repl[host] = _demo_host(host)
    # The same inversion as the DB pass: an adapter status line naming a package id
    # never passes a DB scrub, so it reaches the fixtures verbatim.
    for d in _A.get("domains", []):
        rev = ".".join(reversed(d.split(".")))
        for pkg in set(re.findall(rf"\b{re.escape(rev)}(?:\.[a-z0-9_]+)*", text, re.I)):
            repl[pkg] = "com.example" + pkg[len(rev):]
    for ip in set(IP_RE.findall(text)):
        repl[ip] = doc(ip)
    for uip in set(UIP_RE.findall(text)):
        if IP_RE.search(uip.replace("_", ".")):
            repl[uip] = doc(uip.replace("_", ".")).replace(".", "_")
    if not repl and not dropped:
        print("fixtures carry no live identifiers")
        return
    for old, new in sorted(repl.items(), key=lambda kv: len(kv[0]), reverse=True):
        text = text.replace(old, new)
    path.write_text(text)
    _A["ip_map"] = doc.map
    remember()
    print(f"fixture identifiers scrubbed: {len(repl)}; alerts naming a person dropped: {dropped}")


def verify() -> int:
    """Last line of defence before the fixtures are recorded."""
    people = "|".join(PEOPLE_CHILD + PEOPLE_ADULT)
    places = "|".join(_A["place_tokens"])
    words = "|".join(_A["place_words"])
    pat = f"(?<![a-z])({people}|{words})(?![a-z])|({places})"
    glued = f"({camel(PEOPLE_CHILD + PEOPLE_ADULT)})(?![a-z])"
    # The Cloudflare adapter registers one entity per tunnel route, named after its
    # public hostname, so the whole ingress inventory sits under the family domain
    # and no rewrite can make it demo material. The recorder DROPS those rows
    # (scrub_cloudflare) rather than anonymising them, so they never reach a
    # fixture — counting them here would fail a pipeline that is already correct.
    skip = {"entities": "entity_id NOT LIKE 'cloudflare:%'",
            "current_state": "entity_id NOT LIKE 'cloudflare:%'"}
    bad = []
    for t, c, _ in text_columns():
        where = f"({c}::text ~* '{pat}' OR {c}::text ~ '{glued}')" + (f" AND {skip[t]}" if t in skip else "")
        n = dev_psql(f"SELECT count(*) FROM {t} WHERE {where}").strip()  # noqa: S608
        if n and int(n) > 0:
            # the Croatian word "živo" contains "ivo"; translations are UI copy
            if (t, c) == ("translations", "value"):
                continue
            bad.append(f"{t}.{c}={n}")
    if bad:
        print("LEAK:", ", ".join(bad), file=sys.stderr)
        return 1
    print("clean")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--anonymise-only", action="store_true",
                    help="re-run the anonymisation passes over the dev database as it stands "
                         "(no production copy) — what deploy/demo.sh does before recording")
    ap.add_argument("--scrub-fixtures", metavar="FILE",
                    help="rewrite live-status identifiers (private IPs, MACs) out of a "
                         "recorded fixture file — what deploy/demo.sh runs before the gate")
    args = ap.parse_args()
    if args.scrub_fixtures:
        scrub_fixtures(Path(args.scrub_fixtures))
        return 0
    if args.dry_run:
        print("would refresh:", ", ".join(TABLES), "+ ClickHouse energy history")
        return 0

    # The snapshot does not stay anonymous on its own: dev runs the real adapters,
    # so every live report writes today's truth back over yesterday's rewrite — a
    # camera announced after the last refresh carried its real host into the
    # fixtures and the gate refused to publish. Re-running the passes over
    # whatever the database holds now costs seconds and is idempotent.
    if args.anonymise_only:
        print("== anonymising ==", flush=True)
        anonymise()
        print("== scrubbing identifiers ==", flush=True)
        scrub_identifiers()
        print("== verifying ==", flush=True)
        return verify()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print("== copying production tables ==", flush=True)
        copy_tables(work)
        print("== anonymising ==", flush=True)
        anonymise()
        print("== moving zones and trackers ==", flush=True)
        move_the_world()
        print("== presentation settings ==", flush=True)
        copy_settings()
        print("== scrubbing identifiers ==", flush=True)
        scrub_identifiers()
        print("== energy history ==", flush=True)
        copy_history()
        print("== verifying ==", flush=True)
        return verify()


if __name__ == "__main__":
    sys.exit(main())
