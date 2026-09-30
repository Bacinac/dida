"""The household's address book: names, birth dates, and who gets announced.

DIDA is the house's one reader of the contacts book. A birth date is live
household state — who the people are, whose birthday it is — so it is read here,
once, and everything else asks DIDA for it (OPUS Library asks over HTTP when it
needs a birth year to tell two lookalike children apart).

Name normalisation, the birth-date rule, reading an export and folding a book
into the table are shared by the `contacts` adapter (which syncs from Google) and
the API (which takes an export file and answers lookups), so they live in core
where both already are. Talking to Google does not: that is the adapter's, and
the consent handshake is the API's.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import unicodedata

__all__ = [
    "age_turning",
    "birthday_offset",
    "digest",
    "from_csv",
    "from_vcard",
    "parse_birthday",
    "plain",
    "read_export",
    "store_book",
]

# Separators the digest is built from — a name carrying one would split a field.
_DIGEST_STRIP = re.compile(r"[=:;]")


def plain(name: str) -> str:
    """A name with nothing on it: no accents, no case, no order.

    Two books spell the same person differently — Bošković and Boskovic, Ana
    Marija Kovačić and Kovacic Ana Marija — and a match that insists on one
    spelling matches almost nobody."""
    flat = unicodedata.normalize("NFKD", name)
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    flat = flat.replace("đ", "d").replace("Đ", "D")
    return " ".join(sorted(re.findall(r"[a-z0-9]+", flat.lower())))


def parse_birthday(year, month, day) -> datetime.date | None:
    """A birthday with a year, or nothing.

    A birthday with no year is not half a date here: taking the day from the book
    and the year from somewhere else would produce a date nobody has ever held,
    and the year is the entire reason this is wanted. Google keeps those as
    --MM-DD; they are counted and reported, never invented."""
    try:
        if not year:
            return None
        return datetime.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def from_vcard(text: str) -> list[dict]:
    out, name = [], ""
    for line in text.splitlines():
        line = line.strip()
        head, _, value = line.partition(":")
        if head.upper() == "FN":
            name = value.strip()
        elif head.upper().startswith("BDAY") and name:
            raw = value.strip().replace("-", "")
            when = None
            if re.fullmatch(r"\d{8}", raw):
                when = parse_birthday(raw[:4], raw[4:6], raw[6:])
            out.append({"name": name, "born": when, "raw": value.strip(), "id": ""})
        elif line.upper() == "END:VCARD":
            name = ""
    return out


def from_csv(text: str) -> list[dict]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("Name") or " ".join(
            x for x in (row.get("First Name"), row.get("Middle Name"),
                        row.get("Last Name")) if x) or "").strip()
        raw = (row.get("Birthday") or "").strip()
        if not name or not raw:
            continue
        bits = re.findall(r"\d+", raw)
        when = parse_birthday(*bits[:3]) if len(bits) >= 3 and len(bits[0]) == 4 else None
        out.append({"name": name, "born": when, "raw": raw, "id": ""})
    return out


def read_export(text: str) -> list[dict]:
    """contacts.google.com exports vCard or CSV — the path that needs no consent
    screen and no token, kept as the way in when OAuth is refused or expired."""
    return from_vcard(text) if "BEGIN:VCARD" in text.upper() else from_csv(text)


def birthday_offset(born: datetime.date, today: datetime.date, within: int) -> int | None:
    """How many days from `today` until this birthday, or None if further off
    than `within`.

    29 February falls on the 28th in a common year. The alternative — matching
    (month, day) exactly — drops that person silently in three years out of four,
    and a birthday nobody announces looks exactly like a birthday nobody has."""
    for offset in range(max(within, 0) + 1):
        day = today + datetime.timedelta(days=offset)
        if (born.month, born.day) == (day.month, day.day):
            return offset
        if (born.month, born.day) == (2, 29) and (day.month, day.day) == (2, 28) \
                and not _is_leap(day.year):
            return offset
    return None


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def age_turning(born: datetime.date, today: datetime.date, offset: int) -> int:
    """The age reached on the birthday `offset` days from today."""
    return (today + datetime.timedelta(days=offset)).year - born.year


def digest(people: list[tuple[str, datetime.date]], today: datetime.date, within: int) -> str:
    """`0=Ana:34;1=Marko:12` — who has a birthday, how far off, and which age
    they turn. Deliberately not a sentence: the wording is Croatian, and Croatian
    lives in the rule that speaks it (where its plural agreement can be edited
    without a rebuild), never in an adapter.

    Ordered by day, then name, so the value only changes when the facts do."""
    rows = []
    for name, born in people:
        offset = birthday_offset(born, today, within)
        if offset is None:
            continue
        rows.append((offset, name, age_turning(born, today, offset)))
    return ";".join(
        f"{offset}={_DIGEST_STRIP.sub(' ', name).strip()}:{age}"
        for offset, name, age in sorted(rows, key=lambda r: (r[0], r[1]))
    )


async def store_book(pool, book: list[dict], *, source: str, prune: bool = False) -> dict:
    """Fold a contacts book into `people`, and report what it did.

    Matched on the contact it came from where that is known, and on the name
    otherwise — so somebody renamed in the book stays the same person here rather
    than becoming a stranger plus an orphan.

    `prune` drops rows from this source that the book no longer holds, and is for
    a COMPLETE read only: a file export is usually a subset, and pruning against
    one would empty the table. It takes `announce` with it, which is the honest
    outcome — the person is gone from the book. An EMPTY book never prunes, whatever
    the caller asks.
    """
    rows = await pool.fetch(
        "SELECT id, name, plain_name, born_on, contact_id, source FROM people"
    )
    by_contact = {r["contact_id"]: r for r in rows if r["contact_id"]}
    by_plain: dict[str, list] = {}
    for r in rows:
        by_plain.setdefault(r["plain_name"], []).append(r)

    added, updated, unchanged, dateless, seen = 0, 0, 0, [], set()
    for entry in book:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        contact_id = entry.get("id") or ""
        if entry.get("born") is None:
            dateless.append({"name": name, "says": entry.get("raw") or ""})
            continue
        key = plain(name)
        found = by_contact.get(contact_id) if contact_id else None
        if found is None:
            candidates = by_plain.get(key) or []
            # An ambiguous name is not a match: writing one of two same-named rows
            # would move a birth date onto the wrong person, silently.
            found = candidates[0] if len(candidates) == 1 else None
        if found is None:
            await pool.execute(
                "INSERT INTO people (name, plain_name, born_on, contact_id, source) "
                "VALUES ($1,$2,$3,$4,$5)",
                name, key, entry["born"], contact_id, source,
            )
            added += 1
        elif (found["name"], found["plain_name"], found["born_on"], found["contact_id"]) == \
                (name, key, entry["born"], contact_id):
            unchanged += 1
        else:
            await pool.execute(
                "UPDATE people SET name=$2, plain_name=$3, born_on=$4, contact_id=$5, "
                "source=$6, updated_at=now() WHERE id=$1",
                found["id"], name, key, entry["born"], contact_id, source,
            )
            updated += 1
        if contact_id:
            seen.add(contact_id)

    removed = 0
    # An empty book never prunes. A complete read that came back with nobody in it
    # is a Google or permission anomaly far more often than a household that has
    # left the address book, and the destructive reading of the two is unrecoverable.
    if prune and book:
        gone = await pool.fetch(
            "DELETE FROM people WHERE source = $1 AND contact_id <> '' "
            "AND NOT (contact_id = ANY($2)) RETURNING id",
            source, list(seen),
        )
        removed = len(gone)
    return {"read": len(book), "added": added, "updated": updated,
            "unchanged": unchanged, "removed": removed, "no_year": dateless}
