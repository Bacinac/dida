"""The page that opens the gate — the one action software cannot undo.

`/entry` exists for a limited-trust login: a cleaner, a neighbour with the keys
for a week. The page is the face; the boundary is `is_hidden` + `require_control`
on the resolved entity, exactly as on `POST /command`. That matters more here than
anywhere else in DIDA, because a wrong `turn_off` can be turned back on and a
wrong `unlock` cannot — the gate is open, and whoever was outside is inside.

Two properties follow from that. The slot name may only ever resolve through the
three configured access points, never through an arbitrary key of the settings
store. And opening the front door names who did it, because for an irreversible
physical act the audit trail IS the safety mechanism.
"""

from __future__ import annotations

import json

import pytest
from dida_api import entry as mod
from dida_api.auth import AuthUser
from fastapi import HTTPException

ADMIN = AuthUser(id=1, username="marko", role="admin")
CLEANER = AuthUser(id=3, username="spremacica", role="user")

CONFIG = {
    "car": "gate:auto",
    "pedestrian": "gate:pjesacka",
    "door": "lock:ulazna",
    "notify_on_open": True,
}

CAPS = {
    "gate:auto": ["open_close"],
    "gate:pjesacka": ["press"],
    "lock:ulazna": ["lock", "on_off"],
    "secret:gate": ["open_close"],
    "look:gate": ["open_close"],
    "sensor:only": ["temperature"],
}


class _Pool:
    async def fetchrow(self, sql, entity_id):
        caps = CAPS.get(entity_id)
        if caps is None:
            return None
        return {"entity_id": entity_id, "name": entity_id, "label": None,
                "capabilities": caps}


class _Bus:
    def __init__(self) -> None:
        self.commands: list = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)


class _Request:
    def __init__(self, bus=None) -> None:
        self.bus = bus or _Bus()
        self.app = type("A", (), {"state": type("S", (), {
            "pool": _Pool(), "bus": self.bus})()})()


@pytest.fixture
def configured(monkeypatch):
    """`secret:*` is hidden, `look:*` is view-only. A hidden access point is also
    ungranted — modelled that way so the ORDER of the two checks is pinned; with a
    hidden-but-controllable fixture both orders answer 404 and the leaky one passes."""
    state = {"cfg": dict(CONFIG)}

    async def _get_setting(pool, key):
        return json.dumps(state["cfg"]) if key == mod.SETTING_KEY else None

    async def _is_hidden(pool, user, eid):
        return eid.startswith("secret:")

    async def _require_control(pool, user, eid, cap):
        if eid.startswith(("look:", "secret:")):
            raise HTTPException(403, "not granted")
    monkeypatch.setattr(mod, "get_setting", _get_setting)
    monkeypatch.setattr(mod, "is_hidden", _is_hidden)
    monkeypatch.setattr(mod, "require_control", _require_control)
    return state


# --- which slot, and only which slot -------------------------------------------


@pytest.mark.parametrize("slot", ["gate", "window", "notify_on_open", "state_door",
                                  "", "..", "CAR", "car "])
async def test_only_the_three_access_points_can_be_named(slot, configured):
    """`slot` indexes the settings blob. Unconstrained, `notify_on_open` resolves to
    `True` and `state_door` to a sensor — the caller choosing which stored value
    becomes an entity id."""
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot=slot), _Request(), user=ADMIN)
    assert e.value.status_code in (400, 404)


async def test_an_unconfigured_slot_says_so_rather_than_failing_obscurely(configured):
    configured["cfg"]["car"] = ""
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), _Request(), user=ADMIN)
    assert e.value.status_code == 404


async def test_a_slot_bound_to_a_device_that_is_gone_is_a_404(configured):
    configured["cfg"]["car"] = "gate:removed"
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), _Request(), user=ADMIN)
    assert e.value.status_code == 404


async def test_a_device_that_cannot_open_is_refused_not_silently_ignored(configured):
    """A 204 the tile renders as success, with the gate still shut, is the worst
    outcome on this page: the person outside waits."""
    configured["cfg"]["car"] = "sensor:only"
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), _Request(), user=ADMIN)
    assert e.value.status_code == 409


async def test_a_corrupt_config_does_not_500_the_page(configured, monkeypatch):
    async def _bad(pool, key):
        return "{not json"
    monkeypatch.setattr(mod, "get_setting", _bad)
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), _Request(), user=ADMIN)
    assert e.value.status_code == 404


# --- the boundary ---------------------------------------------------------------


async def test_a_hidden_access_point_does_not_exist(configured):
    """403 would tell a limited-trust account that a second gate is there."""
    configured["cfg"]["car"] = "secret:gate"
    req = _Request()
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), req, user=CLEANER)
    assert e.value.status_code == 404
    assert req.bus.commands == []


async def test_an_ungranted_access_point_is_refused(configured):
    configured["cfg"]["car"] = "look:gate"
    req = _Request()
    with pytest.raises(HTTPException) as e:
        await mod.entry_action(mod.EntryActionIn(slot="car"), req, user=CLEANER)
    assert e.value.status_code == 403
    assert req.bus.commands == []


async def test_a_granted_user_opens_the_gate(configured):
    """The other half: a boundary that refuses the cleaner her one job is an
    outage that ends with a person locked outside."""
    req = _Request()
    assert await mod.entry_action(
        mod.EntryActionIn(slot="car"), req, user=CLEANER) == {"ok": True}
    assert [c.entity_id for c in req.bus.commands] == ["gate:auto"]


# --- how each kind of access point opens ----------------------------------------


@pytest.mark.parametrize("caps,expect", [
    (["lock"], ("lock", "unlock")),
    (["open_close"], ("open_close", "open")),
    (["press"], ("press", "press")),
    (["on_off"], ("on_off", "turn_on")),
    (["temperature"], None),
    ([], None),
])
def test_the_opening_action_is_derived_from_the_capability(caps, expect):
    assert mod._open_action(caps) == expect


def test_a_lock_that_also_exposes_a_relay_is_unlocked_not_energised():
    """Order matters, and this is the case it exists for: the front door is a lock
    with an `on_off` facet. Picking `turn_on` would energise the strike without
    releasing the bolt — the door buzzes and stays shut."""
    assert mod._open_action(["on_off", "lock"]) == ("lock", "unlock")
    assert mod._open_action(["on_off", "open_close"]) == ("open_close", "open")


async def test_the_door_is_unlocked_through_its_lock_capability(configured):
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=ADMIN)
    cmd = req.bus.commands[0]
    assert (cmd.capability, cmd.command) == ("lock", "unlock")


# --- the audit trail ------------------------------------------------------------


async def test_opening_the_door_says_who_did_it(configured):
    """An irreversible physical act. The notification is not a courtesy — it is the
    only record the household gets while it is happening."""
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=CLEANER)
    notify = [c for c in req.bus.commands if c.entity_id == "notify:all"]
    assert len(notify) == 1
    assert "spremacica" in notify[0].args["message"]


async def test_the_command_records_the_person_and_the_surface(configured):
    """`user:x:entry` rather than `user:x` — the command log has to distinguish a
    tap on the entry page from a tap on the dashboard."""
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=CLEANER)
    assert req.bus.commands[0].source == "user:spremacica:entry"


async def test_only_the_door_broadcasts(configured):
    """The car gate opens several times a day. Announcing each one trains everyone
    to ignore the notification that matters."""
    for slot in ("car", "pedestrian"):
        req = _Request()
        await mod.entry_action(mod.EntryActionIn(slot=slot), req, user=ADMIN)
        assert [c.entity_id for c in req.bus.commands] == [CONFIG[slot]]


async def test_the_broadcast_is_on_unless_it_was_turned_off(configured):
    """An absent key must not read as "off" — that is how the trail disappears
    without anyone deciding to remove it."""
    configured["cfg"].pop("notify_on_open")
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=ADMIN)
    assert any(c.entity_id == "notify:all" for c in req.bus.commands)


async def test_an_admin_can_turn_the_broadcast_off(configured):
    configured["cfg"]["notify_on_open"] = False
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=ADMIN)
    assert [c.entity_id for c in req.bus.commands] == ["lock:ulazna"]


async def test_the_open_is_published_before_the_broadcast(configured):
    """If the notification were first, a failure to publish it would announce a
    door that never opened."""
    req = _Request()
    await mod.entry_action(mod.EntryActionIn(slot="door"), req, user=ADMIN)
    assert [c.entity_id for c in req.bus.commands] == ["lock:ulazna", "notify:all"]
