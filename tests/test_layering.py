"""The dependency direction everything else rests on.

DIDA's whole fault-isolation claim — a crashing adapter is a non-event for the
engine — depends on a graph that only ever points DOWN: adapters and services
depend on core, core depends on nothing above it, and no adapter knows another
exists. That property is invisible in any single file and is lost one innocent
import at a time, so it is asserted here rather than trusted.

The junk-drawer guard is the second half. `core` is "everything two or more
services must agree on"; the way a shared layer decays is a feature model that
only ONE service uses being parked there because it had nowhere else to go.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sources(pkg_glob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in ROOT.glob(pkg_glob):
        if d.is_dir():
            out[d.name] = "".join(
                f.read_text(errors="ignore") for f in d.rglob("*.py") if "__pycache__" not in str(f))
    return out


def _exported_names(mod: str) -> set[str]:
    """What `dida_core/__init__.py` re-exports from `mod`.

    A consumer almost never writes `dida_core.heating`; it writes
    `from dida_core import AutomationDef, trigger_matches, …`. Matching only the
    module path made the automation service look like it did not use
    core.automations at all — it does, through five re-exported names.
    """
    init = (ROOT / "core/src/dida_core/__init__.py").read_text()
    m = re.search(rf'^from dida_core\.{mod} import \(([^)]*)\)', init, re.M | re.S)
    if not m:
        m = re.search(rf'^from dida_core\.{mod} import (.+)$', init, re.M)
    names = set(re.findall(r'\b([A-Za-z_]\w*)\b', m.group(1))) if m else set()
    return {n for n in names if len(n) > 3}


def _uses(src: str, mod: str) -> bool:
    if re.search(rf'dida_core\.{mod}\b', src):
        return True
    return any(re.search(rf'\b{re.escape(n)}\b', src) for n in _exported_names(mod))


def test_core_imports_nothing_above_it():
    """The load-bearing one. A single `from dida_api import …` in core would make
    the engine's image need FastAPI, and the isolation argument collapses."""
    core = "".join(f.read_text(errors="ignore")
                   for f in (ROOT / "core/src/dida_core").rglob("*.py"))
    offenders = sorted(set(re.findall(
        r'^\s*(?:from|import)\s+(dida_(?:api|engine|automation|journal|netmgr|planvision|runner|adapter_\w+))',
        core, re.M)))
    assert not offenders, f"core imports from above: {offenders}"


def test_no_adapter_imports_another_adapter():
    """31 protocol packages, zero cross-coupling — this is what lets one be
    deleted, replaced or crash without touching the others."""
    bad = {}
    for name, src in _sources("adapters/*/src/dida_adapter_*").items():
        others = set(re.findall(r'^\s*(?:from|import)\s+(dida_adapter_\w+)', src, re.M)) - {name}
        if others:
            bad[name] = sorted(others)
    assert not bad, f"adapters coupled to each other: {bad}"


def test_no_adapter_imports_a_service():
    bad = {}
    for name, src in _sources("adapters/*/src/dida_adapter_*").items():
        svc = sorted(set(re.findall(
            r'^\s*(?:from|import)\s+(dida_(?:api|engine|automation|journal|netmgr|planvision|runner))',
            src, re.M)))
        if svc:
            bad[name] = svc
    assert not bad, f"adapters reaching into services: {bad}"


def test_no_service_imports_another_service():
    """Services talk over the BUS, never by import — that is what makes them
    separately deployable and separately killable."""
    bad = {}
    for name, src in _sources("services/*/src/dida_*").items():
        others = sorted(set(re.findall(
            r'^\s*(?:from|import)\s+(dida_(?:api|engine|automation|journal|netmgr|planvision|runner))',
            src, re.M)) - {name})
        if others:
            bad[name] = others
    assert not bad, f"services coupled by import: {bad}"


def test_core_carries_no_single_consumer_FEATURE_model():
    """The junk-drawer guard.

    Infrastructure with one consumer is fine (ch_migrations: the engine is the
    sole ClickHouse DDL writer, by design). A FEATURE model with one consumer is
    not — it belongs to that service. `heating` and `automations` pass because
    each is shared by exactly the two that must agree: the api validates on
    write, the automation service executes on load.
    """
    FEATURE_MODELS = ("heating", "automations")
    consumers = _sources("services/*/src/dida_*")
    for mod in FEATURE_MODELS:
        users = {name for name, src in consumers.items() if _uses(src, mod)}
        assert len(users) >= 2, (
            f"core.{mod} is a feature model used by {sorted(users) or 'nobody'} — "
            "a shared layer is for what TWO services must agree on; move it into its service"
        )


# --- the inventory the document claims vs the one on disk ------------------------
#
# CLAUDE.md is loaded into every session working on this repository, which makes a
# wrong sentence in it more expensive than a wrong comment anywhere else: it
# misinforms the next person before they have read a line of code. Found stale on
# 2026-08-08 — it claimed 31 adapters and 8 services against 33 and 9, omitted
# panasonic/peer/unifi and the runner service, and still named `opnsense`, an
# adapter that had been deleted.
#
# Corrected, and then not left as a corrected list. The same day showed twice what
# happens to a fact kept in two places by hand: the CI image list drifted under
# exactly that arrangement, having already drifted once before under a written rule
# telling people not to let it.

CLAUDE_MD = ROOT / "CLAUDE.md"
needs_document = pytest.mark.skipif(not CLAUDE_MD.exists(), reason="CLAUDE.md is not in this checkout")


def _documented(section_start: str, section_end: str) -> set[str]:
    md = CLAUDE_MD.read_text()
    block = md[md.index(section_start):md.index(section_end)]
    return set(re.findall(r"\b([a-z][a-z0-9_-]{2,})/", block))


def _dirs(where: str) -> set[str]:
    return {p.name for p in (ROOT / where).iterdir() if p.is_dir()}


@needs_document
def test_every_adapter_on_disk_is_named_in_the_document():
    missing = sorted(_dirs("adapters") - _documented("├── adapters/", "├── ui/"))
    assert not missing, f"CLAUDE.md does not mention: {missing}"


@needs_document
def test_the_document_does_not_name_an_adapter_that_was_deleted():
    """The failure mode that matters more than a wrong count: `opnsense` outlived
    its own removal here, so every session was told to expect it."""
    # Words that are prose in that block, not directory names.
    prose = {"adapters", "bridge", "cloud", "helpers", "proces", "sensors", "devices",
             "media", "presence-vision", "notify-helpers"}
    named = _documented("├── adapters/", "├── ui/") - prose
    gone = sorted(named - _dirs("adapters"))
    assert not gone, f"CLAUDE.md names adapters that no longer exist: {gone}"


@needs_document
def test_every_service_on_disk_is_named_in_the_document():
    missing = sorted(_dirs("services") - _documented("├── services/", "├── adapters/"))
    assert not missing, f"CLAUDE.md does not mention services: {missing}"


@needs_document
def test_the_stated_counts_match_what_is_there():
    """A number is the part a reader trusts without checking."""
    md = CLAUDE_MD.read_text()
    adapters = int(re.search(r"adapters/\s+#\s*(\d+)\s+adaptera?", md).group(1))
    services = int(re.search(r"services/\s+#\s*(\d+)\s+servisa", md).group(1))
    assert adapters == len(_dirs("adapters")), f"document says {adapters}"
    assert services == len(_dirs("services")), f"document says {services}"
