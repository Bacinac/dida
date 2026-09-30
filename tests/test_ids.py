"""Regression tests for the subject-safe id slugger (dida_core.ids).

slug() is the byte-identical logic every adapter used to hand-roll before it
moved here once: lowercase, collapse any run of non-[a-z0-9_-] characters to a
single '_', trim edge underscores, and fall back to a caller-supplied default
when the result would be empty. Pure string transform, no I/O.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_ids.py"
"""
from dida_core.ids import slug


def test_slug_lowercases_and_replaces_spaces():
    assert slug("Kitchen Light") == "kitchen_light", "spaces collapse to underscore, lowercased"


def test_slug_collapses_runs_and_trims_colon():
    # a colon can't appear in a NATS subject — it must collapse like any other
    # disallowed character, and an adjacent disallowed run collapses to ONE underscore.
    assert slug("  Living Room:2  ") == "living_room_2", \
        "leading/trailing whitespace stripped, colon+space run collapses to one _"


def test_slug_keeps_already_valid_chars():
    assert slug("Already_slugged-123") == "already_slugged-123", \
        "underscore and hyphen are already valid slug chars, kept as-is (just lowercased)"


def test_slug_strips_leading_trailing_underscore_but_not_hyphen():
    assert slug("___") == "x", "all-underscore input trims to empty on both ends -> default"
    assert slug("---") == "---", "hyphens are a VALID slug char, never trimmed"


def test_slug_empty_and_default():
    assert slug("") == "x", "empty input -> default 'x'"
    assert slug("", default="unknown") == "unknown", "caller-supplied default is honoured"
    assert slug("!!!", default="unknown") == "unknown", \
        "an all-disallowed input collapses to empty -> default, not '_'"


def test_slug_non_ascii_is_stripped_not_transliterated():
    # non-ASCII letters aren't in [a-z0-9_-] even after .lower(), so they're
    # dropped like any other disallowed character (no transliteration).
    assert slug("Ø Special ñ Chars!") == "special_chars", \
        "non-ASCII letters collapse away along with the surrounding whitespace/punctuation"


def test_slug_single_char():
    assert slug("a") == "a", "a single already-valid character round-trips"
