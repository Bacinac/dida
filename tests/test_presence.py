"""Regression tests for network-presence reconciliation (dida_core.presence).

This is the arbitration that keeps the network-presence adapters (router DHCP
leases, ARP) from fighting the GPS sources (OwnTracks /
the web app) that write the SAME `presence:<user>` entities. The rules under test:

  * a FRESH foreign (GPS) claim that disagrees owns the value -> we stay quiet;
  * unless this is the arrival edge (away -> present), where the live network
    verdict publishes regardless;
  * a STALE foreign claim (older than the grace window) is GPS drift the network
    verdict repairs;
  * a stored value that is OUR OWN last publish is not "foreign" -> never yielded to;
  * an unchanged-and-still-fresh verdict is deduped; past the refresh window it
    is re-published so the UI staleness window never flags a confirmed phone.

All decision tests pass an explicit `now` so timing is deterministic. The async
fetch helper is driven with an in-memory fake pool (no DB).

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_presence.py"
"""

from conftest import FakeBroker
from dida_core.presence import PresenceReconciler, fetch_presence_locations

EID = "presence:alex"


def _reconciler(grace=300.0):
    return PresenceReconciler(grace_seconds=grace)


# --- PresenceReconciler.resolve ---------------------------------------------

def test_first_verdict_publishes_the_value():
    r = _reconciler()
    out = r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60, now=1000.0)
    assert out == "Home", "first-ever verdict with nothing stored publishes the site"


def test_away_verdict_publishes_away():
    r = _reconciler()
    out = r.resolve(EID, present=False, site="Home", current={}, refresh_seconds=60, now=1000.0)
    assert out == "away", "not present -> the literal 'away', not the site"


def test_unchanged_fresh_verdict_is_deduped():
    r = _reconciler()
    r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60, now=1000.0)
    out = r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60, now=1030.0)
    assert out is None, "same verdict re-asserted inside the refresh window is not re-published"


def test_unchanged_verdict_republishes_past_refresh_window():
    r = _reconciler()
    r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60, now=1000.0)
    out = r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60, now=1070.0)
    assert out == "Home", "past the refresh window the same verdict is re-published (anti-staleness)"


def test_fresh_foreign_claim_owns_the_value():
    # Network says 'away', but a GPS source wrote 'Home' 5s ago -> yield to GPS.
    r = _reconciler(grace=300.0)
    out = r.resolve(
        EID, present=False, site="Home",
        current={EID: ("Home", 5.0)}, refresh_seconds=60, now=1000.0,
    )
    assert out is None, "a fresh disagreeing GPS claim owns the value; network stays quiet"


def test_stale_foreign_claim_is_repaired_by_network():
    # Same disagreement, but the GPS claim is 400s old (> 300s grace) -> drift the
    # live network verdict repairs.
    r = _reconciler(grace=300.0)
    out = r.resolve(
        EID, present=False, site="Home",
        current={EID: ("Home", 400.0)}, refresh_seconds=60, now=1000.0,
    )
    assert out == "away", "a foreign claim older than the grace window is overridden by the network"


def test_present_beats_fresh_foreign_claim():
    # A phone the router actively sees IS home — a fresh disagreeing GPS claim
    # (coarse WiFi/cell fix hundreds of metres out) cannot argue with that.
    r = _reconciler(grace=300.0)
    out = r.resolve(
        EID, present=True, site="Home",
        current={EID: ("Elsewhere", 5.0)}, refresh_seconds=60, now=1000.0,
    )
    assert out == "Home", "an actively-seen phone publishes the site over any fresh foreign claim"


def test_present_repairs_fresh_gps_away():
    # Steady-state on-wifi: a coarse GPS fix wrote 'away' seconds ago — the live
    # network verdict repairs it on the next scan (the 2026-07-13 'locira me
    # daleko dok sam na wifiju' regression).
    r = _reconciler(grace=300.0)
    assert r.resolve(EID, present=True, site="Home", current={},
                     refresh_seconds=60, now=1000.0) == "Home"
    out = r.resolve(
        EID, present=True, site="Home",
        current={EID: ("away", 5.0)}, refresh_seconds=60, now=1010.0,
    )
    assert out == "Home", "while present the network never yields — fresh GPS 'away' is repaired"


def test_own_previous_claim_is_not_treated_as_foreign():
    # We published 'Home'; the stored value is still 'Home' (ours). Now the network
    # flips to 'away'. The stored 'Home' is OUR claim, not a foreign one, so we do
    # NOT yield to it — we overwrite our own stale claim with 'away'.
    r = _reconciler(grace=300.0)
    assert r.resolve(EID, present=True, site="Home", current={},
                     refresh_seconds=60, now=1000.0) == "Home"
    out = r.resolve(
        EID, present=False, site="Home",
        current={EID: ("Home", 5.0)}, refresh_seconds=60, now=1010.0,
    )
    assert out == "away", "a stored value equal to our own last publish is never yielded to"


def test_resolve_defaults_now_to_wall_clock():
    # now=None path: the first verdict does not depend on timing, so it stays
    # deterministic while still exercising the time.time() default branch.
    r = _reconciler()
    out = r.resolve(EID, present=True, site="Home", current={}, refresh_seconds=60)
    assert out == "Home", "resolve() falls back to wall-clock time when now is omitted"


# --- fetch_presence_locations -----------------------------------------------

def _row(entity_id, value, age_seconds):
    return {"entity_id": entity_id, "capability": "location", "value": value, "age": age_seconds}


async def test_fetch_asks_for_presence_locations_only():
    broker = FakeBroker(state=[_row(EID, "Home", 100.0)])
    out = await fetch_presence_locations(broker)
    assert broker.asked("state") == [{"prefix": "presence:", "capabilities": ["location"]}], \
        "one state read, scoped to presence locations"
    assert out[EID] == ("Home", 100.0), "value and age pass through as the api reports them"


async def test_fetch_skips_non_string_values():
    broker = FakeBroker(state=[
        _row("presence:a", "Home", 5.0),
        _row("presence:b", 42, 5.0),      # int -> not a location, skipped
        _row("presence:c", None, 5.0),    # NULL -> skipped
    ])
    out = await fetch_presence_locations(broker)
    assert set(out) == {"presence:a"}, "only string-valued rows become locations"


async def test_fetch_nothing_reported_returns_empty_map():
    out = await fetch_presence_locations(FakeBroker(state=[]))
    assert out == {}, "no rows -> empty mapping"
