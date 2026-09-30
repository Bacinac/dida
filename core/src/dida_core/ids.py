"""Subject-safe id slugging, shared by adapters.

A NATS subject can't contain a colon/space/uppercase, so device and component
ids get slugged before they become entity_ids. Every adapter had a byte-identical
copy of this; here it lives once.
"""

from __future__ import annotations

import re

_SLUG = re.compile(r"[^a-z0-9_-]+")


def slug(s: str, *, default: str = "x") -> str:
    """Lowercase, collapse any run of non ``[a-z0-9_-]`` to ``_``, trim edge
    underscores. Returns ``default`` when the result is empty."""
    return _SLUG.sub("_", s.strip().lower()).strip("_") or default
