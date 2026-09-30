"""The assistant's knowledge of DIDA itself.

The assistant could drive devices but knew nothing ABOUT the app: asked "how do I
make a rule" or "where do I see who turned the light on", it had no tool to answer
with and guessed. Its `explain_dida` tool answers from the same articles the /help
pages show — `ui/src/lib/help/` — so the page a person reads and the answer the
assistant gives cannot drift apart.

English on purpose: the backend emits English everywhere and the consumer
localises (see the i18n architecture). The assistant is told which language to
answer in and translates the English article as it answers.

Anything derivable from code is generated (see `capabilities`), never written into
an article, so it cannot go stale.
"""

from __future__ import annotations

import json
from pathlib import Path

from dida_core import command_vocabulary, readable_capabilities

# Installed in the image, the articles sit where its Dockerfile copies them. Run
# from a checkout (the test suites put services/api/src first on PYTHONPATH), they
# are read from the working tree they are written in, as the code is.
_TREE = Path(__file__).resolve().parents[4] / "ui" / "src" / "lib" / "help"
HELP_DIR = _TREE if _TREE.is_dir() else Path("/app/help")

_CAPABILITIES = (
    "The full command vocabulary, per capability",
    """The core knows only capabilities, never devices. A command is valid only if the
capability declares it, and a value is valid only inside the capability's own range or
choice set — anything else is rejected at the boundary rather than half-applied.

Writable capabilities and their exact commands:
{vocabulary}

Read-only (report values, accept no command):
{readable}

Conventions that are not visible in the list above:
- announce: the entity is `announce:<speaker>` and the command is `say`, whose value is
  the text to speak.
- media_transport: `play_media` carries a uri (plus optional title/art), not a value.
- Both `<x>` and `<x>_options` may be present: the plain one is the current value and
  the `_options` one carries the list of values `set_<x>` will accept.""",
)


def _load() -> dict[str, tuple[str, str]]:
    # Read once, at import: the tool's topic enum is built from it, and a missing
    # directory is a broken image that should not start.
    index = json.loads((HELP_DIR / "index.json").read_text(encoding="utf-8"))
    topics = {
        e["slug"]: (e["summary"]["en"], (HELP_DIR / f"{e['slug']}.en.md").read_text(encoding="utf-8").strip())
        for e in index
    }
    topics["capabilities"] = _CAPABILITIES
    return topics


# topic -> (one-line summary for the index, full text)
_TOPICS = _load()


def help_index() -> str:
    """Topic list, for when the model has not named one."""
    lines = [f"- {name}: {summary}" for name, (summary, _) in _TOPICS.items()]
    return "DIDA help topics (call explain_dida again with one of these):\n" + "\n".join(lines)


def help_text(topic: str | None = None) -> str:
    """One topic's text, or the index when the topic is missing or unknown.

    Unknown topics return the index rather than an error: the model picked a word,
    and showing it the real list is more useful than making it guess again.
    """
    if not topic:
        return help_index()
    key = topic.strip().lower().replace(" ", "_")
    entry = _TOPICS.get(key)
    if entry is None:
        # Cheap synonym pass before giving up — "rules" should not dead-end.
        for name, (summary, _) in _TOPICS.items():
            if key in name or key in summary.lower():
                entry = _TOPICS[name]
                break
    if entry is None:
        return f"No topic named {topic!r}.\n\n" + help_index()
    text = entry[1]
    if "{vocabulary}" in text:
        text = text.format(vocabulary=command_vocabulary(), readable=readable_capabilities())
    return text


HELP_TOPIC_NAMES: tuple[str, ...] = tuple(_TOPICS)
