"""Keeping the address book in order — at the source, and never on its own.

The book is kept by hand and shows it: names stripped of their diacritics, people
filed under no label, entries holding nothing but a name. DIDA reads that book
every morning, so correcting a name here would last exactly until tomorrow — the
fix has to land in Google or it is not a fix.

Which makes this the one place in DIDA that writes to somebody else's system, and
it is built so that it cannot do so quietly:

  * Nothing here runs on a schedule. Every change starts with somebody opening
    this page and asking for proposals.
  * A proposal is a `before` and an `after` on one named person. It is applied
    only if its id comes back in an apply call — the ones not ticked never move.
  * The apply re-reads the book first. Google's etags make a stale proposal fail
    loudly rather than overwrite an edit made in the meantime.

The daily sync (the `contacts` adapter) has no part in this and still only reads.

Duplicates are reported, not applied. The People API has no merge — building one
would mean choosing which of two records survives and copying fields between
them, while contacts.google.com already does exactly that, better, with one
click. Naming the pair and linking to it is the whole of the help worth giving.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from dida_core.adapter_config import decrypt_secret
from dida_core.people import plain
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, require_admin
from dida_api.common import resolve_assistant_client

log = logging.getLogger("dida.api.upkeep")

router = APIRouter(prefix="/contacts/upkeep", tags=["contacts"])

_TOKEN = "https://oauth2.googleapis.com/token"
_PEOPLE = "https://people.googleapis.com/v1"
# Everything the four checks need. `metadata` carries the etag, without which no
# update is accepted.
_FIELDS = "names,birthdays,emailAddresses,phoneNumbers,memberships,organizations,metadata"

# Restoring a name is a judgement about a person, not a lookup: no dictionary can
# tell Šimić from Simić, and getting it wrong renames somebody in their own
# address book. The most capable model, and a human confirming every line.
_MODEL = "claude-opus-5"
# Small enough that one bad batch is a small loss and the model keeps the whole
# family in view while it works.
_BATCH = 40
# What Google takes in one write call. Well under its documented ceiling, and the
# difference between one request and a hundred and forty-eight.
_BATCH_WRITE = 150

# The two this page files under. A person moved from one to the other has to LEAVE
# the first, or they end up in both — and anything else they carry (ICE) is not
# this page's business and is left alone.
_FILING = ("Work", "Private")

# Where a mail address says "this is work". Everything else is somebody's own.
_FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "gmx.com", "net.hr", "inet.hr", "vip.hr",
    "t-com.hr", "email.t-com.hr", "yandex.com", "aol.com", "zoho.com",
}

_SPELL_SYSTEM = """You restore Croatian diacritics to personal names.

Croatian writes č ć ž š đ. A name typed on a phone keyboard, imported from a SIM
card or synced from an old device usually lost them. Your job is to put back only
what was certainly there.

Rules:
- Return the given name and family name as they should be written in Croatian.
- Change nothing you are not sure of. Vedran, Mirela, Horvat, Boskovic→Bošković
  are all decidable; an unfamiliar or foreign name is not — leave it as it is.
- Non-Croatian names (Adalberto, Will, Tammee) keep their own spelling. Do not
  Croatianise anything.
- Never translate, shorten, expand or reorder a name. Diacritics only.
- `confident` is false when the name could plausibly be written either way
  (Simic may be Šimić or Simić). A false there still shows the suggestion; it is
  marked so the person reading knows to look twice.
"""

_SPELL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["names"],
    "properties": {
        "names": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "given", "family", "confident"],
                "properties": {
                    "id": {"type": "string"},
                    "given": {"type": "string"},
                    "family": {"type": "string"},
                    "confident": {"type": "boolean"},
                },
            },
        }
    },
}


def _why(resp: httpx.Response) -> str:
    """Google's own words. A bare status line sent a hundred and forty-eight
    identical failures to the screen with nothing in them to act on; the body
    said exactly which field was wrong."""
    try:
        return str((resp.json().get("error") or {}).get("message") or resp.text)[:200]
    except (AttributeError, ValueError):
        return f"{resp.status_code}: {resp.text[:160]}"


def _secret() -> str:
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(500, "DIDA_SECRET_KEY is not set — the Google token cannot be read.")
    return secret


async def _credentials(pool) -> tuple[str, str, str]:
    rows = {r["key"]: r["value"] for r in await pool.fetch(
        "SELECT key, value FROM adapter_config WHERE adapter = 'contacts'")}
    if "_oauth" not in rows:
        raise HTTPException(409, "Nisi povezan s Google kontaktima.")
    secret = _secret()
    client_secret = decrypt_secret(secret, rows.get("client_secret", ""),
                                   adapter="contacts", key="client_secret")
    blob = json.loads(decrypt_secret(secret, rows["_oauth"], adapter="contacts", key="_oauth"))
    return rows.get("client_id", ""), client_secret, blob.get("refresh_token", "")


async def _token(pool) -> str:
    client_id, client_secret, refresh = await _credentials(pool)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise HTTPException(502, f"Google je odbio token: {r.text[:200]}")
    scopes = set(r.json().get("scope", "").split())
    if "https://www.googleapis.com/auth/contacts" not in scopes:
        # Read-only grants reach every proposal and none of the writes. Saying so
        # here beats a 403 from the middle of an apply, half a list in.
        raise HTTPException(
            409, "Povezano je samo za čitanje. Odspoji pa se ponovno poveži — "
                 "zahtjev sada traži i pravo pisanja.")
    return r.json()["access_token"]


async def _book(token: str) -> list[dict]:
    out: list[dict] = []
    page = None
    async with httpx.AsyncClient(timeout=60) as c:
        while True:
            params = {"personFields": _FIELDS, "pageSize": 1000}
            if page:
                params["pageToken"] = page
            r = await c.get(f"{_PEOPLE}/people/me/connections", params=params,
                            headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            body = r.json()
            out += body.get("connections", [])
            page = body.get("nextPageToken")
            if not page:
                return out


async def _labels(token: str) -> dict[str, str]:
    """The user's own labels, by name → resource name. System groups are Google's
    (`myContacts`, `starred`) and are not something to file anybody under."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_PEOPLE}/contactGroups", params={"pageSize": 200},
                        headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return {g.get("formattedName", ""): g["resourceName"]
            for g in r.json().get("contactGroups", [])
            if g.get("groupType") == "USER_CONTACT_GROUP"}


# ── reading a person ──────────────────────────────────────────────────────────

def _name(person: dict) -> dict:
    return (person.get("names") or [{}])[0]


def _display(person: dict) -> str:
    n = _name(person)
    return n.get("displayName") or " ".join(
        x for x in (n.get("givenName"), n.get("familyName")) if x)


def _etag(person: dict) -> str:
    """The person's OWN etag, which is the one an update is checked against.

    A contact carries two: `person.etag` and `metadata.sources[].etag`. They look
    alike and are not interchangeable — sending the source etag fails every write
    with 400 "Request person.etag is different than the current person.etag",
    which reads like a stale-cache problem and is not one. Established against the
    live API, because the reference text says the other thing."""
    return person.get("etag") or (
        person.get("metadata") or {}).get("sources", [{}])[0].get("etag", "")


def _own_labels(person: dict) -> list[str]:
    return [m["contactGroupMembership"]["contactGroupResourceName"]
            for m in (person.get("memberships") or [])
            if (m.get("contactGroupMembership") or {}).get("contactGroupId")
            not in (None, "myContacts", "starred")]


def _label_names(person: dict, labels: dict[str, str]) -> list[str]:
    """The person's labels by the name a person reads, not by resource id."""
    mine = set(_own_labels(person))
    return [name for name, rid in labels.items() if rid in mine]


def _has_diacritics(text: str) -> bool:
    return any(unicodedata.combining(ch)
               for ch in unicodedata.normalize("NFKD", text)) or bool(re.search(r"[đĐ]", text))


# ── what a proposal is ────────────────────────────────────────────────────────
class Proposal(BaseModel):
    """What a check found, before it becomes a row on the page.

    Internal: the page shows the BOOK and fills a suggestion into the field it
    would change, so this never leaves the module — it is only how the four
    checks hand their findings to the assembler."""

    id: str
    kind: str
    contact: str
    name: str
    before: str = ""
    after: str = ""
    detail: str = ""
    context: str = ""
    labels: list[str] = Field(default_factory=list)
    applicable: bool = True
    sure: bool = True
    payload: dict = Field(default_factory=dict)


# ── the four checks ───────────────────────────────────────────────────────────

def _empty_proposals(book: list[dict], labels: dict[str, str] | None = None) -> list[Proposal]:
    """A card with a name on it and nothing else. It cannot ring, write, or have
    a birthday wished — whatever it was for is no longer in it."""
    out = []
    for p in book:
        if (p.get("emailAddresses") or p.get("phoneNumbers")
                or p.get("birthdays") or p.get("organizations")):
            continue
        name = _display(p)
        if not name:
            continue
        out.append(Proposal(
            id=f"empty:{p['resourceName']}", kind="empty", contact=p["resourceName"],
            name=name, before=name, after="—",
            labels=_label_names(p, labels or {}),
            detail="Nema ni broja, ni maila, ni rođendana, ni tvrtke.",
        ))
    return out


def _duplicate_findings(book: list[dict]) -> list[Proposal]:
    """Pairs worth a second look, by the three things that actually repeat: the
    name, an address, a number. Reported only — Google's own merge is the tool."""
    def _digits(v: str) -> str:
        return re.sub(r"\D", "", v)[-8:]

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in book:
        if _display(p):
            buckets[("ime", plain(_display(p)))].append(p)
        for e in p.get("emailAddresses") or []:
            if e.get("value"):
                buckets[("mail", e["value"].strip().lower())].append(p)
        for ph in p.get("phoneNumbers") or []:
            d = _digits(ph.get("value", ""))
            if len(d) >= 6:
                buckets[("broj", d)].append(p)

    seen: set[frozenset] = set()
    out = []
    for (why, value), people in sorted(buckets.items()):
        names = {p["resourceName"] for p in people}
        if len(names) < 2:
            continue
        key = frozenset(names)
        if key in seen:
            continue
        seen.add(key)
        who = ", ".join(sorted({_display(p) or "(bez imena)" for p in people}))
        out.append(Proposal(
            id=f"duplicate:{'+'.join(sorted(names))}", kind="duplicate",
            contact=sorted(names)[0], name=who, before=who, after="",
            detail=f"Dijele isti {why}: {value}. Spoji ih u Google kontaktima — "
                   f"njihovo spajanje zna što zadržati.",
            applicable=False,
        ))
    return out


def _label_proposals(book: list[dict], labels: dict[str, str]) -> list[Proposal]:
    """Everybody filed under nothing, sorted by the one thing the card already
    says about them: a company, or an address that belongs to one."""
    work, private = labels.get("Work"), labels.get("Private")
    if not (work and private):
        return []

    def _about(p: dict) -> str:
        bits = [e["value"] for e in (p.get("emailAddresses") or []) if e.get("value")]
        bits += [n["value"] for n in (p.get("phoneNumbers") or []) if n.get("value")]
        return " · ".join(bits[:3])

    out = []
    for p in book:
        if _own_labels(p) or not _display(p):
            continue
        org = next((o.get("name") or o.get("title") or ""
                    for o in (p.get("organizations") or [])), "")
        domains = {e.get("value", "").rsplit("@", 1)[-1].lower()
                   for e in (p.get("emailAddresses") or []) if "@" in e.get("value", "")}
        business = domains - _FREEMAIL
        if org:
            target, why = "Work", f"Radi u: {org}."
        elif business:
            target, why = "Work", f"Adresa na {sorted(business)[0]}, nije osobna."
        else:
            target, why = "Private", "Ni tvrtke ni poslovne adrese."
        out.append(Proposal(
            id=f"label:{target}:{p['resourceName']}", kind="label",
            contact=p["resourceName"], name=_display(p), before="(bez oznake)",
            after=target, detail=why, sure=bool(org or business),
            context=_about(p), labels=_label_names(p, labels),
        ))
    return out


async def _spelling_proposals(client, book: list[dict],
                              labels: dict[str, str]) -> list[Proposal]:
    candidates = [p for p in book if _display(p) and not _has_diacritics(_display(p))]
    if not client or not candidates:
        return []
    out: list[Proposal] = []
    for start in range(0, len(candidates), _BATCH):
        chunk = candidates[start:start + _BATCH]
        listing = "\n".join(
            json.dumps({"id": p["resourceName"],
                        "given": _name(p).get("givenName", ""),
                        "family": _name(p).get("familyName", "")}, ensure_ascii=False)
            for p in chunk)
        try:
            resp = await client.messages.create(
                model=_MODEL, max_tokens=4096, system=_SPELL_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": _SPELL_SCHEMA}},
                messages=[{"role": "user", "content": listing}],
            )
            answer = json.loads(next(b.text for b in resp.content if b.type == "text"))
        except Exception as exc:  # one bad batch must not lose the other proposals
            log.warning("upkeep: spelling batch %d failed: %s", start // _BATCH, exc, exc_info=True)
            continue
        by_id = {p["resourceName"]: p for p in chunk}
        for item in answer.get("names", []):
            p = by_id.get(item.get("id", ""))
            if p is None:
                continue
            given, family = item.get("given", ""), item.get("family", "")
            was = _name(p)
            if (given, family) == (was.get("givenName", ""), was.get("familyName", "")):
                continue
            if not _has_diacritics(f"{given}{family}"):
                continue  # a change that adds no diacritic is not this check's business
            out.append(Proposal(
                id=f"spelling:{p['resourceName']}", kind="spelling",
                contact=p["resourceName"], name=_display(p),
                labels=_label_names(p, labels),
                before=_display(p), after=" ".join(x for x in (given, family) if x),
                detail="" if item.get("confident") else "Moglo bi se pisati i drugačije — provjeri.",
                sure=bool(item.get("confident")),
                payload={"givenName": given, "familyName": family},
            ))
    return out

# ── what a row is ─────────────────────────────────────────────────────────────

class Suggestion(BaseModel):
    given: str
    family: str
    sure: bool = True
    why: str = ""


class PersonRow(BaseModel):
    """One contact, as the page shows it — not a proposal about one.

    The page began as a list of things to change, which meant a contact whose
    name was already right never appeared on it, and there was nowhere to give
    that person a birth date. So the page is the BOOK, every row editable, and a
    suggestion is a value filled in ahead of time rather than a separate item."""

    id: str
    name: str
    given: str = ""
    family: str = ""
    born: str = ""          # YYYY-MM-DD, or "" — a birthday with no year is not one
    born_raw: str = ""      # what Google holds when it is not a full date
    labels: list[str] = Field(default_factory=list)
    context: str = ""
    empty: bool = False
    # Anything else the name carries (a middle name). Shown because it is kept,
    # and kept because a write replaces the whole name.
    extra: str = ""
    suggest: Suggestion | None = None


class Finding(BaseModel):
    """Something to look at rather than something to do. Duplicates only: the
    People API has no merge, and contacts.google.com does it better."""

    id: str
    who: str
    detail: str


class ApplyItem(BaseModel):
    """One row as it stood on the screen. The server writes only what differs
    from the book — a row nobody touched costs nothing to send."""

    id: str = Field(..., max_length=200)
    given: str = Field("", max_length=200)
    family: str = Field("", max_length=200)
    born: str = Field("", max_length=10)     # YYYY-MM-DD, or "" to leave/clear
    target: str = Field("", max_length=100)  # which label to file under
    remove: bool = False


class ApplyIn(BaseModel):
    items: list[ApplyItem] = Field(..., min_length=1, max_length=600)


# Google computes these three from the parts; sending them back is at best
# redundant and at worst a stale display name pinned over a corrected one.
_DERIVED_NAME = ("metadata", "displayName", "displayNameLastFirst", "unstructuredName")


def _rename(person: dict, given: str, family: str) -> dict:
    """The person's name with two fields changed and everything else kept.

    `updateMask=names` replaces the WHOLE name, so sending just given and family
    deletes whatever else was in there. Exactly one contact in this book carries a
    middle name — and a middle name that vanishes during a spelling correction is
    the kind of loss nobody notices until the person is asked about it."""
    kept = {k: v for k, v in _name(person).items() if k not in _DERIVED_NAME}
    return {**kept, "givenName": given, "familyName": family}


def _extra_name(person: dict) -> str:
    """Anything in the name beyond given and family, for the row to show. Hiding
    it is how it got deleted."""
    kept = {k: v for k, v in _name(person).items()
            if k not in (*_DERIVED_NAME, "givenName", "familyName") and v}
    return " · ".join(f"{k}: {v}" for k, v in sorted(kept.items()))


# `batchUpdateContacts` takes ONE updateMask for the whole call, and a body that
# omits a masked field does not leave that field alone — Google CLEARS it. So the
# mask is constant and every body carries BOTH fields, the current value where
# nothing changed. Anything else means one row renaming itself can blank the name
# of every other person in the same batch, which is exactly what happened: eleven
# people had their names deleted while their birthdays were being added.
_WRITE_MASK = "names,birthdays"


def _birthdays_of(person: dict) -> list[dict]:
    """The birthdays as they stand, without Google's own metadata."""
    return [{"date": d["date"]} for d in (person.get("birthdays") or []) if d.get("date")]


def _write_body(person: dict, given: str, family: str, born: list[dict] | None) -> dict:
    """One contact's write. Complete by construction: both masked fields, always."""
    return {
        "etag": _etag(person),
        "names": [_rename(person, given, family)],
        "birthdays": _birthdays_of(person) if born is None else born,
    }


def _born_of(person: dict) -> tuple[str, str]:
    """(YYYY-MM-DD, what Google actually holds). A birthday with no year comes
    back as the raw text, because that is a thing to SEE and complete, not a date
    to invent — and the year is the whole reason the house keeps these."""
    days = person.get("birthdays") or []
    if not days:
        return "", ""
    date = days[0].get("date") or {}
    y, m, d = date.get("year"), date.get("month"), date.get("day")
    if y and m and d:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}", ""
    if m and d:
        return "", f"--{int(m):02d}-{int(d):02d}"
    return "", str(days[0].get("text") or "")


def _parse_born(value: str) -> dict | None:
    """"YYYY-MM-DD" into Google's shape, or None when it is not a whole date."""
    parts = value.strip().split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(x) for x in parts)
        datetime.date(y, m, d)  # rejects 31 February before Google has to
    except ValueError:
        return None
    return {"year": y, "month": m, "day": d}


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/survey")
async def survey(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """What the book looks like, without spending a model call on it."""
    token = await _token(request.app.state.pool)
    book = await _book(token)
    labels = await _labels(token)
    return {
        "contacts": len(book),
        "labels": {name: sum(1 for p in book if rid in _own_labels(p))
                   for name, rid in labels.items()},
        "unlabelled": sum(1 for p in book if _display(p) and not _own_labels(p)),
        "without_diacritics": sum(1 for p in book
                                  if _display(p) and not _has_diacritics(_display(p))),
        "without_birthday": sum(1 for p in book if not p.get("birthdays")),
        "empty": len(_empty_proposals(book, labels)),
        "duplicates": len(_duplicate_findings(book)),
    }


@router.post("/proposals")
async def proposals(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """The whole book, with the corrections already filled in.

    Nothing here has touched Google: this reads the book and asks a model about
    the names in it. A row becomes a change only when it comes back to /apply
    carrying something different from what is written here."""
    token = await _token(request.app.state.pool)
    book = await _book(token)
    labels = await _labels(token)
    client = await resolve_assistant_client(request.app.state)

    spelling = {p.contact: p for p in await _spelling_proposals(client, book, labels)}
    filing = {p.contact: p for p in _label_proposals(book, labels)}
    empty = {p.contact for p in _empty_proposals(book, labels)}

    rows: list[PersonRow] = []
    for person in book:
        name = _display(person)
        if not name:
            continue
        rid = person["resourceName"]
        was = _name(person)
        born, raw = _born_of(person)
        fix = spelling.get(rid)
        rows.append(PersonRow(
            id=rid, name=name,
            # The suggested spelling is what the field STARTS as, so accepting it
            # is doing nothing and rejecting it is typing over it.
            given=(fix.payload.get("givenName") if fix else None) or was.get("givenName", ""),
            family=(fix.payload.get("familyName") if fix else None) or was.get("familyName", ""),
            born=born, born_raw=raw,
            labels=_label_names(person, labels) or (
                [filing[rid].after] if rid in filing else []),
            context=" · ".join(
                [e["value"] for e in (person.get("emailAddresses") or []) if e.get("value")]
                + [n["value"] for n in (person.get("phoneNumbers") or []) if n.get("value")]
            )[:120],
            empty=rid in empty,
            extra=_extra_name(person),
            suggest=Suggestion(
                given=fix.payload.get("givenName", ""), family=fix.payload.get("familyName", ""),
                sure=fix.sure, why=fix.detail,
            ) if fix else None,
        ))

    rows.sort(key=lambda r: plain(r.name))
    return {
        "contacts": len(rows),
        "assistant": client is not None,
        "suggested": sum(1 for r in rows if r.suggest),
        "people": [r.model_dump() for r in rows],
        "duplicates": [Finding(id=f.id, who=f.name, detail=f.detail).model_dump()
                       for f in _duplicate_findings(book)],
    }


@dataclass
class _Plan:
    by_id: dict[str, dict]
    labels: dict[str, str]
    done: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    write: dict[str, dict] = field(default_factory=dict)   # person → the complete body to send
    remove: list[str] = field(default_factory=list)
    add_to_label: defaultdict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    drop_from_label: defaultdict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def fail(self, rid: str, why: str) -> None:
        who = _display(self.by_id.get(rid, {})) or rid
        self.failed.append({"id": rid, "error": f"{who}: {why}"[:220]})


def _born_change(item: ApplyItem, person: dict) -> tuple[bool, list[dict] | None, str | None]:
    """(moved, new birthdays, error). An empty list clears the date on purpose."""
    born_now, _ = _born_of(person)
    if item.born.strip() == born_now:
        return False, None, None
    if not item.born.strip():
        return True, [], None
    date = _parse_born(item.born)
    if date is None:
        return True, None, f"„{item.born}” nije cijeli datum"
    return True, [{"date": date}], None


def _plan_filing(plan: _Plan, item: ApplyItem, person: dict) -> None:
    target = item.target.strip()
    if not target:
        return
    if target not in plan.labels:
        plan.fail(item.id, f"oznaka {target!r} više ne postoji")
        return
    filed = _label_names(person, plan.labels)
    if target in filed:
        return
    plan.add_to_label[plan.labels[target]].append(item.id)
    for other in _FILING:
        if other != target and other in filed:
            plan.drop_from_label[plan.labels[other]].append(item.id)


def _plan_item(plan: _Plan, item: ApplyItem) -> None:
    person = plan.by_id.get(item.id)
    if person is None:
        plan.fail(item.id, "kontakt više ne postoji")
        return
    if item.remove:
        plan.remove.append(item.id)
        return

    was = _name(person)
    given, family = item.given.strip(), item.family.strip()
    name_moved = (given, family) != (was.get("givenName", ""), was.get("familyName", ""))
    if name_moved and not (given or family):
        plan.fail(item.id, "ime ne smije ostati prazno")
        return

    born_moved, born, error = _born_change(item, person)
    if error:
        plan.fail(item.id, error)
        return

    if name_moved or born_moved:
        # Whole body, both fields, whichever of the two actually moved.
        if not name_moved:
            given, family = was.get("givenName", ""), was.get("familyName", "")
        plan.write[item.id] = _write_body(person, given, family, born)

    _plan_filing(plan, item, person)


async def _send_writes(c: httpx.AsyncClient, head: dict, plan: _Plan) -> None:
    # One call for the lot. A hundred and forty-eight sequential writes ran
    # into Google's per-minute quota and came back as a wall of 429s.
    ids = list(plan.write)
    for start in range(0, len(ids), _BATCH_WRITE):
        chunk = {rid: plan.write[rid] for rid in ids[start:start + _BATCH_WRITE]}
        try:
            r = await c.post(f"{_PEOPLE}/people:batchUpdateContacts", headers=head,
                             json={"contacts": chunk, "updateMask": _WRITE_MASK,
                                   "readMask": _WRITE_MASK})
            if r.status_code != 200:
                raise ValueError(_why(r))
            results = r.json().get("updateResult", {})
            for rid in chunk:
                # Per contact: a batch can carry one bad etag without failing
                # the rest, and a silent omission is still a failure.
                if (results.get(rid) or {}).get("person"):
                    plan.done.append(rid)
                else:
                    plan.fail(rid, str((results.get(rid) or {}).get("status") or "nije upisano"))
        except Exception as exc:
            log.warning("upkeep: write batch failed: %s", exc, exc_info=True)
            for rid in chunk:
                plan.fail(rid, str(exc))


async def _send_removals(c: httpx.AsyncClient, head: dict, plan: _Plan) -> None:
    for start in range(0, len(plan.remove), _BATCH_WRITE):
        chunk = plan.remove[start:start + _BATCH_WRITE]
        try:
            r = await c.post(f"{_PEOPLE}/people:batchDeleteContacts", headers=head,
                             json={"resourceNames": chunk})
            if r.status_code != 200:
                raise ValueError(_why(r))
            plan.done += chunk
        except Exception as exc:
            log.warning("upkeep: delete batch failed: %s", exc, exc_info=True)
            for rid in chunk:
                plan.fail(rid, str(exc))


async def _send_filing(c: httpx.AsyncClient, head: dict, plan: _Plan) -> None:
    # Leaving before joining: briefly in both is tidier than briefly in
    # neither, if the second call is the one that fails.
    for group, people in plan.drop_from_label.items():
        try:
            r = await c.post(f"{_PEOPLE}/{group}/members:modify", headers=head,
                             json={"resourceNamesToRemove": people})
            if r.status_code != 200:
                raise ValueError(_why(r))
        except Exception as exc:
            log.warning("upkeep: unfiling from %s failed: %s", group, exc, exc_info=True)
    for group, people in plan.add_to_label.items():
        try:
            r = await c.post(f"{_PEOPLE}/{group}/members:modify", headers=head,
                             json={"resourceNamesToAdd": people})
            if r.status_code != 200:
                raise ValueError(_why(r))
            plan.done += people
        except Exception as exc:
            log.warning("upkeep: filing into %s failed: %s", group, exc, exc_info=True)
            for rid in people:
                plan.fail(rid, str(exc))


@router.post("/apply")
async def apply(body: ApplyIn, request: Request,
                _admin: AuthUser = Depends(require_admin)) -> dict:
    """Write the rows that differ from the book, and nothing else.

    The book is read again first. That is what turns a stale row into a loud
    failure rather than an overwrite: the etag is a minute old, and a card that
    has gained a phone number since is no longer the empty one anybody agreed to
    delete. What is NOT re-derived is the correction — it arrives with the row,
    because somebody may have typed it."""
    token = await _token(request.app.state.pool)
    book = await _book(token)
    plan = _Plan(by_id={p["resourceName"]: p for p in book}, labels=await _labels(token))
    for item in body.items:
        _plan_item(plan, item)

    async with httpx.AsyncClient(timeout=60) as c:
        head = {"Authorization": f"Bearer {token}"}
        await _send_writes(c, head, plan)
        await _send_removals(c, head, plan)
        await _send_filing(c, head, plan)

    # The book has moved and DIDA still holds the old names and dates. Waiting
    # until four in the morning to see your own correction is not an answer, so
    # this asks for a read; the adapter picks it up on its next tick and clears
    # it. The sync stays the adapter's — this only says that it is wanted.
    if plan.done:
        await request.app.state.pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ('contacts_sync_requested', '1') "
            "ON CONFLICT (key) DO UPDATE SET value = '1'")
    return {"applied": len(set(plan.done)), "failed": plan.failed}
