"""A migration carries the schema, not one installation's house.

That sentence is the commit message of `cdd36f9` (2026-07-28), which deleted 26
migrations from the repository. They were real and they had been applied: lux
thresholds for this kitchen, hysteresis for that hallway, a rename of one slider.
Good changes, wrong place — a migration runs on EVERY installation, so tuning one
house through it means a clean install elsewhere either inherits a stranger's
thresholds or, once the file is deleted to stop that, silently misses them.

The decision worked. Measured today: the house carries 69 applied versions of
which 26 have no file left (and therefore no checksum), while Cabin — installed
after the cleanup — carries exactly the 43 that exist, with every checksum
present. The house's own tuning lives on in its database and its nightly dumps,
which is where installation state belongs.

What the decision did NOT get was anything to enforce it. It is a rule that
depends on remembering, and this repository has now watched two such rules fail:
the CI image list drifted twice under exactly that arrangement. So the rule
becomes a check.

The fixtures are the real deleted migrations, recovered from the commit that
removed them. A guard proven against a synthetic example proves the example.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"

# A concrete device, named by an adapter that discovers real hardware. `esphome:`
# alone is a namespace; `esphome:203_0_113_35` is one node on one LAN.
_DEVICE_NAMESPACES = (
    "esphome", "shelly", "tuya", "baba", "mqtt", "govee", "broadlink", "frigate",
    "cast", "dlna", "denon", "heos", "harmony", "androidtv", "volumio", "landroid",
    "roidmi", "midea", "panasonic", "smartthings", "iammeter", "unifi", "dreame",
    "samsungtv", "opus",
)
_ENTITY = re.compile(rf"\b({'|'.join(_DEVICE_NAMESPACES)}):[a-z0-9_]+", re.I)
# Row identity in a table that holds an installation's own content.
_ROW_ID = re.compile(r"\bWHERE\s+id\s*=\s*\d+", re.I)
_NAMED_ROW = re.compile(r"\b(automations|scenes)\b[^;]{0,400}?\bname\s*=\s*'", re.I | re.S)


# A translation seed is product copy: the rows land identically on every
# installation, and the English side is UI text that may legitimately quote an
# example entity id — the media-source field's help reads
# "kitchen=cast:kitchen_speaker". Translating that is not carrying a house.
# Exempted as a whole STATEMENT rather than line-by-line: the real file writes one
# row per line, but a guard that depends on where somebody wrapped a line is a
# guard that breaks on reformatting.
# Terminated on a semicolon at END OF LINE, not on the first semicolon: the
# translated strings contain them mid-sentence ("Any HEOS device; empty =
# auto-discovery."), so a naive match ends the statement 170 rows early and the
# rest of the seed is scanned as if it were an UPDATE.
_TRANSLATION_INSERT = re.compile(
    r"INSERT\s+INTO\s+translations\b.*?;\s*$", re.I | re.S | re.M)


def _code(sql: str) -> str:
    """The statements, with the commentary and the translation seed removed.

    A migration's comment legitimately names the device it was reasoned about —
    the one recovered here explains a 4.9 lux measurement on a specific sensor.
    Reading the comments would flag the explanation instead of the change, which
    is the mistake the upgrade-guard suite already made once. The translation seed
    comes out for the same reason: what it contains is the product's own words."""
    lines = [ln.split("--", 1)[0] for ln in sql.splitlines()]
    body = "\n".join(ln for ln in lines if ln.strip())
    return _TRANSLATION_INSERT.sub(" ", body)


def _offences(sql: str) -> list[str]:
    code = _code(sql)
    found = []
    if m := _ENTITY.search(code):
        found.append(f"names a device: {m.group(0)}")
    if m := _ROW_ID.search(code):
        found.append(f"targets a row by id: {m.group(0)}")
    if _NAMED_ROW.search(code):
        found.append("targets an automation/scene by name")
    return found


def _migrations() -> list[pathlib.Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


# --- the rule holds now ----------------------------------------------------------


def test_the_migration_set_is_not_empty():
    """A guard that runs over nothing passes over everything."""
    assert len(_migrations()) > 40


@pytest.mark.parametrize("path", _migrations(), ids=lambda p: p.name)
def test_a_migration_does_not_carry_one_installations_house(path):
    """It runs on EVERY installation. A threshold tuned for this kitchen either
    lands in a stranger's house or, once the file is removed to stop that,
    silently stops being part of a fresh install."""
    offences = _offences(path.read_text())
    assert not offences, f"{path.name}: " + "; ".join(offences)


def test_generic_data_migrations_are_still_allowed():
    """The rule is about SCOPE, not about touching data. Four surviving
    migrations do write to `entities`/`automations` — over a column, for every
    row, on any installation. Flagging those would push people to hand-run SQL
    instead, which is worse: the same edit with no record that it happened."""
    allowed = [p for p in _migrations()
               if re.search(r"\bUPDATE\s+(entities|automations)\b", _code(p.read_text()), re.I)]
    assert allowed, "expected the generic data migrations to still be here"
    for p in allowed:
        assert not _offences(p.read_text()), p.name


# --- and the guard is proven against the migrations that broke it ----------------


# Recovered from `cdd36f9`, the commit that deleted them — a guard checked against
# an invented example proves the example.
_REAL_OFFENDERS = {
    "0046_kitchen_bright_off.sql": """
-- Kitchen light: a third trigger that turns the ceiling light OFF once there is
-- enough daylight. Verified the light adds <5 lux to esphome:203_0_113_35.
UPDATE automations SET definition = $json${"script": "turn_off(\\"esphome:203_0_113_54:kitchen_light\\")"}$json$::jsonb
 WHERE id = 66 AND name = 'LIGHT-Kitchen';
""",
    "0053_rename_intensity.sql": """
UPDATE entities SET name = 'Intenzitet košnje' WHERE entity_id = 'landroid:mower:intensity';
""",
    "0020_harmony_remote.sql": """
INSERT INTO areas (name) VALUES ('Dnevni boravak');
UPDATE automations SET enabled = false WHERE id = 12;
""",
}


@pytest.mark.parametrize("name,sql", sorted(_REAL_OFFENDERS.items()))
def test_the_guard_catches_the_migrations_that_caused_the_rule(name, sql):
    assert _offences(sql), f"{name} would pass — the guard does not catch what it exists for"


def test_a_comment_that_merely_mentions_a_device_is_not_an_offence():
    """The recovered migration explains a lux measurement on a named sensor in its
    comment. Reasoning about a device is how a good migration gets written; the
    rule is about what the SQL touches."""
    sql = """
-- Measured on esphome:203_0_113_35: the light adds <5 lux, well under the
-- 20 lux threshold, so there is no feedback loop. See automation id = 66.
ALTER TABLE automations ADD COLUMN IF NOT EXISTS last_error TEXT;
"""
    assert _offences(sql) == []


def test_ui_copy_may_quote_an_example_device():
    """The media-source help text reads "kitchen=cast:kitchen_speaker" and is
    translated into Croatian. That row is identical on every installation — it is
    the product's words, not this house's wiring."""
    sql = """
INSERT INTO translations (key, lang, value) VALUES
  ('kitchen=cast:kitchen_speaker, bedroom=cast:bedroom_speaker', 'hr',
   'kuhinja=cast:kitchen_speaker, spavaća=cast:bedroom_speaker');
"""
    assert _offences(sql) == []


def test_the_translation_exemption_cannot_be_used_as_cover():
    """Only the row shape is exempt, not the file. A migration that seeds copy AND
    retunes one house is still caught on the second half."""
    sql = """
INSERT INTO translations (key, lang, value) VALUES
  ('kitchen light', 'hr', 'kuhinjsko svjetlo');
UPDATE automations SET enabled = false WHERE id = 66;
"""
    assert _offences(sql)


def test_schema_statements_are_never_flagged():
    """The overwhelming majority of migrations are exactly this, and a guard that
    nags about them gets switched off."""
    sql = """
CREATE TABLE IF NOT EXISTS media_tracks (id BIGSERIAL PRIMARY KEY, path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS media_tracks_path_idx ON media_tracks (path);
ALTER TABLE entities ADD COLUMN IF NOT EXISTS native_key TEXT;
"""
    assert _offences(sql) == []
