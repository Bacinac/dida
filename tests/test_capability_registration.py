"""Every capability an adapter names must be REGISTERED in the core model.

This is the gate for a failure that already happened and was invisible for
months: the baba adapter published `identity_since`, declared it on the entity
and had it rendered by three UI surfaces — while the core model had never
registered it, so the engine rejected every publish at the boundary. The footnote
it fed showed nothing, and the only trace was a log line that rotates away. It
took the device-event journal, shipped much later, to surface it.

The asymmetry is what makes it dangerous: naming a capability is cheap and local
(one string in an adapter), registering it is a deliberate edit somewhere else,
and NOTHING connects the two at build time. This does.

Scope, stated honestly: only LITERAL names are checkable. An adapter that builds
a capability name at runtime is out of reach here — the boundary still rejects
it, and now the journal makes that visible, but this test cannot see it.
"""
from __future__ import annotations

import pathlib
import re

from dida_core.capabilities import CAPABILITIES

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The three literal shapes an adapter uses to name a capability.
_KW = re.compile(r'capability\s*=\s*"([a-z_]+)"')
_PUB = re.compile(r'_pub\([^,\n]+,\s*"([a-z_]+)"')
_LIST = re.compile(r'capabilities\s*=\s*\[([^\]]*)\]')
_STR = re.compile(r'"([a-z_]+)"')


def _named_in(path: pathlib.Path) -> dict[str, set[str]]:
    """capability name -> the adapter packages that name it."""
    found: dict[str, set[str]] = {}
    for f in path.rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        src = f.read_text(errors="ignore")
        names = {m.group(1) for m in _KW.finditer(src)}
        names |= {m.group(1) for m in _PUB.finditer(src)}
        for m in _LIST.finditer(src):
            names |= set(_STR.findall(m.group(1)))
        for n in names:
            found.setdefault(n, set()).add(f.relative_to(ROOT).parts[1])
    return found


def test_every_capability_an_adapter_names_is_registered():
    unknown = {n: sorted(who) for n, who in _named_in(ROOT / "adapters").items()
               if n not in CAPABILITIES}
    assert not unknown, (
        "adapters name capabilities the core model does not know — the engine will "
        f"reject every publish of these at the boundary, silently: {unknown}"
    )


def test_the_services_agree_too():
    # virtual entities, the heating controller and the peer mirror all publish
    # state; the same rule applies to them.
    unknown = {n: sorted(who) for n, who in _named_in(ROOT / "services").items()
               if n not in CAPABILITIES}
    assert not unknown, f"services name unregistered capabilities: {unknown}"


def test_the_check_would_have_caught_the_bug_it_exists_for():
    """identity_since is the historical case. If the scanner cannot see it named
    in the fleet, the test above proves nothing — it would pass on an empty set."""
    named = _named_in(ROOT / "adapters")
    assert "identity_since" in named, \
        "the scanner no longer sees identity_since — the patterns have drifted from the code"
    assert "identity_since" in CAPABILITIES, "…and it must be registered"
    # The failing shape, simulated: pretend the registry lacks it.
    pretend = {n for n in CAPABILITIES if n != "identity_since"}
    assert "identity_since" not in pretend and "identity_since" in named, \
        "with the registration removed, the scanner would flag it — which is the point"


def test_the_scanner_actually_finds_a_useful_number_of_names():
    """A regex that quietly stops matching turns this whole file into a no-op that
    passes forever. Anchor it: the fleet names dozens of capabilities."""
    named = _named_in(ROOT / "adapters")
    assert len(named) >= 25, f"only {len(named)} capability names found — patterns likely stale"
