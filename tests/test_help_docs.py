"""Unit test — the assistant's explain-itself corpus (dida_api.help_docs).

PURE: dict lookups + string formatting, no Postgres, no bus, no LLM.

DIDA ships no help pages; `explain_dida` is the whole help system. So the corpus
itself is the thing to guard: a topic that renders empty, or a page nobody can ask
about, is a hole in the only documentation the app has.
"""
from dida_api.help_docs import HELP_TOPIC_NAMES, help_index, help_text

# Every named surface a user can stand on and press "Explain this page". The
# header button sends the page's NAME, so the corpus must have something to say
# about each one.
SURFACES = (
    "floor plan", "cameras", "media", "automations", "schedules", "scenes", "helpers",
    "history", "presence", "entry", "devices", "adapters", "users", "voice",
    "notifications", "backup", "retention", "alerts",
)


def test_every_topic_renders():
    for name in HELP_TOPIC_NAMES:
        text = help_text(name)
        assert len(text) > 100, f"{name} is empty or a stub"
        # The generated vocabulary legitimately contains braces (a colour pattern
        # is `[0-9a-fA-F]{6}`), so only OUR placeholders may not survive.
        assert "{vocabulary}" not in text and "{readable}" not in text, (
            f"{name} has an unsubstituted placeholder")


def test_capabilities_topic_is_generated_not_written():
    # The command vocabulary comes from CAPABILITIES, so a new capability documents
    # itself. `vacuum` arrived with the robot adapter and nobody edited this corpus.
    text = help_text("capabilities")
    assert "vacuum" in text, "the vocabulary is not being generated from the capability model"


def test_index_lists_every_topic_with_a_summary():
    index = help_index()
    for name in HELP_TOPIC_NAMES:
        assert f"- {name}:" in index, f"{name} is unreachable — absent from the index"
        summary = index.split(f"- {name}:", 1)[1].split("\n", 1)[0].strip()
        assert len(summary) > 10, f"{name} has no usable summary"


def test_unknown_topic_falls_back_to_the_index():
    assert "DIDA help topics" in help_text("no-such-topic")
    assert "DIDA help topics" in help_text(None)
    assert help_text("rules") == help_text("automations"), "a near-miss resolves, not dead-ends"


def test_every_page_a_user_can_ask_about_is_covered():
    # The index counts: the model reads it first and picks a topic from it.
    corpus = " ".join([help_index(), *(help_text(n) for n in HELP_TOPIC_NAMES)]).lower()
    missing = [s for s in SURFACES if s not in corpus]
    assert not missing, f"the assistant cannot explain: {missing}"
