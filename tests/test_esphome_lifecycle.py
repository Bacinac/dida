"""The ESPHome node set — 410 of this house's entities, one connection each.

The mapping suite proves a node's reading becomes the right capability. This is
the layer under it: which nodes exist, when one is rebuilt, when one is torn
down, and what the badge says while any of that is happening.

The supervisor re-reads the node list every ten seconds and reconciles. Three of
its decisions are load-bearing and none of them was held by anything.

A BROKEN node list must not read as an EMPTY one. The list is a JSON textarea in
Settings and a Fernet blob at rest, so a typo — or an undecryptable value after a
key rotation — is a normal failure. Treating that as "no nodes configured" would
disconnect the entire estate over a missing bracket, which is the outage, not the
protection.

An UNCHANGED node must not be touched. The reconcile keys on a signature of the
node's own config; if that signature moved on its own, every tick would drop and
rebuild twenty connections, forever.

And the adapter badge is DERIVED from the per-node states rather than set at the
last thing that happened — otherwise it reads "ok, 20 nodes" while all twenty are
offline, which is worse than no badge because it answers the question wrongly.
"""

from __future__ import annotations

import json

import pytest
from dida_adapter_esphome.adapter import EsphomeAdapter


class _Status:
    """What `adapter_runner` attaches at startup; a bare adapter has none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def idle(self, detail=""):
        self.calls.append(("idle", detail))

    def connecting(self, detail=""):
        self.calls.append(("connecting", detail))

    def ok(self, detail=""):
        self.calls.append(("ok", detail))

    def error(self, detail=""):
        self.calls.append(("error", detail))

    def last(self):
        return self.calls[-1] if self.calls else (None, None)


def _adapter():
    a = EsphomeAdapter()
    a.status = _Status()
    return a


# --- a broken node list is not an empty one --------------------------------------


def test_an_empty_config_is_no_nodes():
    """A fresh installation has not been given any. That is not a fault."""
    assert EsphomeAdapter._parse_config("") == []


def test_a_textarea_left_with_whitespace_reads_as_empty_not_broken():
    """Where the trimming happens matters. `_parse_config` sees pre-trimmed input,
    so whitespace-only never reaches it as "broken" — the read path strips first.
    Asserted through the read path rather than the parser, because that is the
    only way the two are actually connected."""
    a = _adapter()
    a._cfg = type("C", (), {"get": staticmethod(lambda k, d="": "  \n  ")})()
    assert EsphomeAdapter._parse_config(a._read_config_raw()) == []


def test_the_read_path_is_what_trims():
    a = _adapter()
    a._cfg = type("C", (), {"get": staticmethod(lambda k, d="": '  [{"host": "192.0.2.1"}]  ')})()
    assert EsphomeAdapter._parse_config(a._read_config_raw())[0]["host"] == "192.0.2.1"


@pytest.mark.parametrize("raw", [
    "[{host: 1}]",
    '[{"host": "a"',
    "not json at all",
    '{"host": "a"}',
    '"a string"',
    "42",
    "gAAAAABm-undecryptable-fernet-blob",
])
def test_a_BROKEN_config_is_distinguishable_from_an_empty_one(raw):
    """The distinction is the whole guard. `[]` means "disconnect everything",
    `None` means "something is wrong, keep what you have". A textarea typo, or an
    undecryptable blob after a key rotation, must land in the second."""
    assert EsphomeAdapter._parse_config(raw) is None


def test_entries_without_a_host_are_dropped_not_fatal():
    """A half-filled row in the editor is one bad node, not a broken list."""
    raw = json.dumps([{"name": "a", "host": "192.0.2.1"}, {"name": "b"}, "junk"])
    parsed = EsphomeAdapter._parse_config(raw)
    assert [d["host"] for d in parsed] == ["192.0.2.1"]


def test_a_valid_list_survives_intact():
    raw = json.dumps([{"name": "kuhinja", "host": "192.0.2.35", "noise_psk": "k"}])
    assert EsphomeAdapter._parse_config(raw)[0]["name"] == "kuhinja"


# --- why a connect failed, in words the UI can localise --------------------------


@pytest.mark.parametrize("err,expect", [
    (RuntimeError("Noise handshake failed"), "encryption"),
    (RuntimeError("invalid psk"), "encryption"),
    (ValueError("encryption error"), "encryption"),
    (OSError("Could not resolve host"), "unresolved"),
    (TimeoutError("timed out"), "unreachable"),
    (OSError("socket error"), "unreachable"),
    (ConnectionRefusedError("connect call failed"), "unreachable"),
    (OSError("host unreachable"), "unreachable"),
    (RuntimeError("something else entirely"), "error"),
])
def test_a_connect_failure_is_classified(err, expect):
    """"Offline" and "wrong key" send a person to opposite ends of the house. A
    node with a mistyped noise_psk is powered on, on the network, and answering —
    telling the owner it is unreachable is telling them the wrong thing."""
    assert EsphomeAdapter._classify_error(err) == expect


def test_encryption_wins_over_a_generic_connection_word():
    """A handshake failure often arrives wrapped in a connection error. The
    specific reason has to survive the generic one."""
    assert EsphomeAdapter._classify_error(
        ConnectionError("connect failed: noise handshake rejected")) == "encryption"


# --- the badge is derived, never remembered --------------------------------------


def test_no_nodes_is_idle_not_ok():
    a = _adapter()
    a._update_badge()
    assert a.status.last()[0] == "idle"


def test_every_node_online_reads_as_ok():
    a = _adapter()
    a._status = {"a": {"state": "online"}, "b": {"state": "online"}}
    a._update_badge()
    state, detail = a.status.last()
    assert state == "ok" and "2 node(s)" in detail


def test_a_partial_outage_says_how_many():
    """"ok" with a count is the difference between "the kitchen node is down" and
    "ESPHome is fine" — and only one of those sends someone to look."""
    a = _adapter()
    a._status = {"a": {"state": "online"}, "b": {"state": "offline"}, "c": {"state": "error"}}
    a._update_badge()
    state, detail = a.status.last()
    assert state == "ok" and "1/3" in detail


def test_everything_offline_is_an_ERROR_not_a_quiet_ok():
    """The failure this derivation exists for: a badge set at the last successful
    connect keeps reading "ok, 20 nodes" while all twenty are gone."""
    a = _adapter()
    a._status = {"a": {"state": "offline"}, "b": {"state": "error"}}
    a._update_badge()
    state, detail = a.status.last()
    assert state == "error" and "0/2" in detail


def test_the_badge_follows_the_nodes_back_down_again():
    """Derived means derived in both directions — a node recovering must clear the
    error without anything else happening."""
    a = _adapter()
    a._status = {"a": {"state": "offline"}}
    a._update_badge()
    assert a.status.last()[0] == "error"
    a._status["a"]["state"] = "online"
    a._update_badge()
    assert a.status.last()[0] == "ok"


async def test_setting_a_node_status_updates_the_badge_and_stamps_the_time():
    a = _adapter()
    a._bus = _RecordingBus()  # _set_status now also emits a reachability verdict
    await a._set_status("kuhinja", state="online", entities=12)
    key = EsphomeAdapter._node_key("kuhinja")
    assert a._status[key]["state"] == "online"
    assert a._status[key]["since"] > 0
    assert a.status.last()[0] == "ok"


# --- dropping a node takes its leftovers with it ---------------------------------


class _Conn:
    def __init__(self, name) -> None:
        self.name = name
        self.reconnect = None
        self.client = None


async def test_dropping_a_node_removes_its_command_routes():
    """A stale route dispatches a command for a node that is gone — at best
    nothing happens, at worst it lands on whatever took its place."""
    a = _adapter()
    conn, other = _Conn("kuhinja"), _Conn("spavaca")
    a._conns = {"kuhinja": conn, "spavaca": other}
    a._routing = {"esphome:kuhinja:light": (conn, "k"), "esphome:spavaca:light": (other, "s")}
    await a._drop_device("kuhinja")
    assert list(a._routing) == ["esphome:spavaca:light"]


async def test_dropping_a_node_forgets_its_cached_state_and_units():
    """The reconcile drops and re-adds a node on every EDIT. Without this the two
    dicts grow by one node's entities each time somebody renames a light."""
    a = _adapter()
    conn = _Conn("kuhinja")
    a._conns = {"kuhinja": conn}
    a._state = {("esphome:kuhinja:light", "on_off"): True,
                ("esphome:spavaca:light", "on_off"): False}
    a._unit = {("esphome:kuhinja:temp", "temperature"): "°C",
               ("esphome:spavaca:temp", "temperature"): "°C"}
    await a._drop_device("kuhinja")
    assert list(a._state) == [("esphome:spavaca:light", "on_off")]
    assert list(a._unit) == [("esphome:spavaca:temp", "temperature")]


async def test_dropping_a_node_clears_its_signature_so_it_can_be_re_added():
    """The signature is what says "unchanged". Leaving it behind means a node
    removed and added back is never actually rebuilt."""
    a = _adapter()
    a._conns = {"kuhinja": _Conn("kuhinja")}
    a._sig = {"kuhinja": "whatever"}
    await a._drop_device("kuhinja")
    assert "kuhinja" not in a._sig


async def test_dropping_a_node_that_is_not_there_is_harmless():
    """The reconcile calls drop-then-add for an edited node, so the drop runs
    against a key that may not exist yet."""
    a = _adapter()
    await a._drop_device("nonexistent")


async def test_a_reconnect_that_refuses_to_stop_does_not_block_the_drop():
    """A hung teardown must not wedge the supervisor for every other node."""
    a = _adapter()
    conn = _Conn("kuhinja")

    class _R:
        async def stop(self):
            raise RuntimeError("stuck")
    conn.reconnect = _R()
    a._conns = {"kuhinja": conn}
    await a._drop_device("kuhinja")
    assert a._conns == {}


# --- the key a node is known by --------------------------------------------------


def test_the_node_key_matches_what_the_registry_uses():
    """It has to agree with the entity id's device slug and with the API's own
    lookup, or the UI shows a node whose status can never be found."""
    assert EsphomeAdapter._node_key("Kuhinja Gornja") == "kuhinja_gornja"
    assert EsphomeAdapter._node_key("192.0.2.35") == EsphomeAdapter._node_key("192.0.2.35")


def test_a_nameless_node_still_gets_a_key():
    assert EsphomeAdapter._node_key("") == "dev"


# --- reachability: the connection verdict flows out as a device signal -----------
#
# The Cabin incident's real gap was that nothing OUTSIDE the adapter knew a device
# was gone. _set_status is the single point where a node's connection state
# changes, so it is where the reachability signal is emitted — edge-triggered, so a
# node that stays up (and only bumps its entity count) does not reprint the verdict.


class _RecordingBus:
    def __init__(self) -> None:
        self.reach: list = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)


def _reach_adapter():
    a = EsphomeAdapter()
    a.status = _Status()
    a._bus = _RecordingBus()
    return a


async def test_going_online_publishes_reachable():
    a = _reach_adapter()
    await a._set_status("Kuhinja", state="online", entities=8)
    assert len(a._bus.reach) == 1
    ev = a._bus.reach[0]
    assert ev.device_key == "kuhinja" and ev.reachable is True and ev.adapter == "esphome"


async def test_going_offline_publishes_unreachable_with_no_detail_leak():
    a = _reach_adapter()
    await a._set_status("Kuhinja", state="online")
    await a._set_status("Kuhinja", state="offline")
    assert a._bus.reach[-1].reachable is False


async def test_a_connect_error_carries_the_reason():
    """"offline" and "wrong key" send a person to opposite ends of the house — the
    reason has to ride along so the timeline says which."""
    a = _reach_adapter()
    await a._set_status("Kuhinja", state="error", code="encryption", reason="noise handshake failed")
    ev = a._bus.reach[-1]
    assert ev.reachable is False
    assert "noise" in ev.detail or ev.detail == "encryption"


async def test_reachability_is_edge_triggered_not_a_heartbeat():
    """_set_status is also called just to update the entity count. The verdict must
    publish once per CHANGE, or the device row churns and the timeline fills."""
    a = _reach_adapter()
    await a._set_status("Kuhinja", state="online", entities=8)
    await a._set_status("Kuhinja", state="online", entities=9)   # count bump, same state
    await a._set_status("Kuhinja", state="online", entities=9)
    assert len(a._bus.reach) == 1, "reachable was re-published without a change"


async def test_flapping_publishes_each_transition():
    a = _reach_adapter()
    for st in ("online", "offline", "online"):
        await a._set_status("Kuhinja", state=st)
    assert [e.reachable for e in a._bus.reach] == [True, False, True]


async def test_dropping_a_node_clears_the_reachability_cache():
    """So a node removed and re-added re-announces its current verdict rather than
    being silenced by a stale 'already reachable' cache entry."""
    a = _reach_adapter()
    a._conns = {"kuhinja": _Conn("kuhinja")}
    await a._set_status("kuhinja", state="online")
    await a._drop_device("kuhinja")
    assert "kuhinja" not in a._reach
