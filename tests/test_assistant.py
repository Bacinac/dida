"""Unit test — the assistant's generated prompt vocabulary, its help corpus, and
its rate limiter. No DB, no model: everything here is pure.

The point of the vocabulary tests is drift. The command list used to be written by
hand in the prompt and covered 5 of the ~24 writable capabilities, so the model
guessed names for media/climate/mower and the boundary rejected them. It is now
rendered from CAPABILITIES; these tests assert that the rendering really does cover
the whole registry, so adding a capability without teaching the assistant about it
is a test failure rather than a silent blind spot.

The DB-backed half (tool dispatch, permissions, option catalogue) is
tests/test_api_assistant.py.
"""
import pytest
from dida_core import CAPABILITIES, Access, command_vocabulary, readable_capabilities
from fastapi import HTTPException


def test_vocabulary_covers_every_writable_capability():
    """Every capability that declares commands must appear, with all of them."""
    text = command_vocabulary()
    for kind, spec in CAPABILITIES.items():
        if not spec.commands:
            continue
        assert f"  {kind.value} → " in text, f"{kind.value} missing from the vocabulary"
        for command in spec.commands:
            assert command in text, f"{kind.value}: command {command!r} not rendered"


def test_vocabulary_omits_read_only_capabilities():
    """A sensor must not appear as something to command — that is what made the
    model invent commands for read-only capabilities in the first place."""
    text = command_vocabulary()
    for kind, spec in CAPABILITIES.items():
        if spec.access is Access.READ:
            assert f"  {kind.value} → " not in text, f"{kind.value} is read-only"


def test_vocabulary_carries_ranges_and_choices():
    """The value hint comes from the same spec validate_command_args enforces, so a
    model that follows the prompt cannot produce an out-of-range set_<x>."""
    text = command_vocabulary()
    assert "set_brightness" in text and "0..100" in text
    # A closed string set is spelled out rather than left to the model's imagination.
    assert "set_hvac_mode" in text
    for mode in CAPABILITIES[_kind("hvac_mode")].choices:
        assert mode in text, f"hvac_mode choice {mode!r} not offered to the model"


def test_readable_capabilities_are_exactly_the_read_only_ones():
    entries = readable_capabilities().split(", ")
    names = {e.split(" (")[0] for e in entries}
    expected = {k.value for k, s in CAPABILITIES.items() if s.access is Access.READ}
    assert names == expected


def test_readable_capabilities_carry_their_closed_value_sets():
    """A sensor is the commonest thing to trigger on, and the trigger must name the
    value. Told only that sun_state exists, a model invents one — which is exactly
    what happened on the live system before this."""
    text = readable_capabilities()
    assert "sun_state (night|dawn|day|dusk)" in text
    for kind, spec in CAPABILITIES.items():
        if spec.access is Access.READ and spec.choices:
            assert f"{kind.value} ({'|'.join(spec.choices)})" in text


def _kind(value):
    return next(k for k in CAPABILITIES if k.value == value)


# ── help corpus ──────────────────────────────────────────────────────────────


def test_help_index_lists_every_topic():
    from dida_api.help_docs import HELP_TOPIC_NAMES, help_index

    text = help_index()
    for name in HELP_TOPIC_NAMES:
        assert name in text


def test_help_capabilities_topic_is_rendered_not_templated():
    """The capabilities topic interpolates the generated vocabulary. A stray
    placeholder would ship the literal '{vocabulary}' to the model."""
    from dida_api.help_docs import help_text

    text = help_text("capabilities")
    assert "{vocabulary}" not in text and "{readable}" not in text
    assert "set_brightness" in text, "the generated vocabulary did not land"


def test_help_every_topic_renders():
    """Guards the .format() on the one templated topic: a brace typo in ANY entry
    would raise here rather than at the user's first question."""
    from dida_api.help_docs import HELP_TOPIC_NAMES, help_text

    for name in HELP_TOPIC_NAMES:
        assert help_text(name).strip(), f"{name} rendered empty"


def test_help_unknown_topic_falls_back_to_the_index():
    from dida_api.help_docs import help_text

    assert "DIDA help topics" in help_text("nonsense-topic")
    assert "DIDA help topics" in help_text(None)


def test_help_resolves_a_near_miss():
    """'rules' is what a user calls automations; it must not dead-end."""
    from dida_api.help_docs import help_text

    assert "Trigger" in help_text("rules")


# ── prompts ──────────────────────────────────────────────────────────────────


def test_prompts_are_fully_interpolated():
    """All three prompts are f-strings carrying literal JSON braces. A missing
    escape turns into a KeyError at import or a stray placeholder in the prompt."""
    from dida_api.assistant import _AUTHOR_SYSTEM, _STARLARK_AUTHOR_SYSTEM, _SYSTEM

    for name, prompt in (
        ("_SYSTEM", _SYSTEM),
        ("_AUTHOR_SYSTEM", _AUTHOR_SYSTEM),
        ("_STARLARK_AUTHOR_SYSTEM", _STARLARK_AUTHOR_SYSTEM),
    ):
        assert "set_hvac_mode" in prompt, f"{name} lost the generated vocabulary"
        assert "{command_vocabulary" not in prompt, f"{name} has an unrendered field"
    # The announce convention is not derivable from the spec, so it is written by
    # hand in every prompt that can emit one.
    assert 'args {"value": "<text to speak>"}' in _SYSTEM


def test_system_prompt_is_static():
    """It must carry nothing per-user and nothing per-request: it is the cached
    prefix, and one interpolated locale or username would give every caller their
    own entry and every turn a cold write."""
    from dida_api.assistant import _SYSTEM

    for leak in ("hrvatskom", "engleskom", "RESPONSE LANGUAGE", "{", "}"):
        if leak in ("{", "}"):
            continue  # JSON examples legitimately contain braces
        assert leak not in _SYSTEM, f"{leak!r} makes the prefix vary"
    # The language rule is now a property of the message, not of the app.
    assert "LAST AND MOST VISIBLE RULE — LANGUAGE" in _SYSTEM


def test_control_model_calls_are_explicit_about_thinking():
    """On the control model an OMITTED thinking field means adaptive. The tool loop
    wants that; the one-shot jobs (explain, translate) must opt out or thinking eats
    their small max_tokens. Both settings are named constants so neither drifts."""
    from dida_api.assistant import CONTROL_THINKING, ONE_SHOT_THINKING

    assert CONTROL_THINKING == {"type": "adaptive"}
    assert ONE_SHOT_THINKING == {"type": "disabled"}


# ── rate limit ───────────────────────────────────────────────────────────────


def test_assistant_limiter_allows_a_burst_then_429s():
    from dida_api.rate_limit import ASSISTANT_LIMITER, enforce_assistant_limit

    for _ in range(int(ASSISTANT_LIMITER._capacity)):
        enforce_assistant_limit("burstuser")  # a real conversation stays under this
    with pytest.raises(HTTPException) as exc:
        enforce_assistant_limit("burstuser")
    assert exc.value.status_code == 429
    assert exc.value.headers.get("Retry-After")


def test_assistant_limiter_is_per_user():
    """One person exhausting their budget must not lock the household out."""
    from dida_api.rate_limit import ASSISTANT_LIMITER, enforce_assistant_limit

    for _ in range(int(ASSISTANT_LIMITER._capacity)):
        enforce_assistant_limit("noisy")
    with pytest.raises(HTTPException):
        enforce_assistant_limit("noisy")
    enforce_assistant_limit("quiet")  # untouched bucket
