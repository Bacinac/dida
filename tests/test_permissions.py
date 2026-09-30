"""Regression tests for the page-scope least-privilege boundary.

Run inside the api image (dida_api installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_permissions.py"

Exercises the read-surface scoping (page_allowed_ids / hidden_for /
can_view_entity) that keeps a narrow login (entry-only) from enumerating the whole
house, using a fake pool so no live Postgres is needed. Complements test_auth.py
(pure can_see_page) with the DB-shaped logic.
"""
import asyncio
import json

from dida_api.auth import AuthUser
from dida_api.visibility import can_view_entity, hidden_for, page_allowed_ids

ENTRY_CFG = {
    "car": "virtual:car_gate", "pedestrian": "virtual:ped_gate",
    "door": "tuya:lock", "state_door": "baba:contact", "notify_on_open": True,
}
ENTRY_IDS = {"virtual:car_gate", "virtual:ped_gate", "tuya:lock", "baba:contact"}
HOUSE = {"mqtt:light", "sensor:temp", "presence:alex"}
ALL_IDS = ENTRY_IDS | HOUSE


class FakePool:
    """Answers only the handful of queries visibility.py issues, by SQL substring."""

    def __init__(self, entry_cfg=ENTRY_CFG, all_ids=ALL_IDS, view_hidden=()):
        self.entry_cfg = entry_cfg
        self.all_ids = all_ids
        self.view_hidden = set(view_hidden)

    async def fetchval(self, sql, *args):
        if "entry_controls" in sql:
            return json.dumps(self.entry_cfg) if self.entry_cfg is not None else None
        if "EXISTS" in sql:  # is_hidden(user, entity_id)
            return args[1] in self.view_hidden
        return None

    async def fetch(self, sql, *args):
        if "user_access_rules" in sql:  # hidden_entity_ids (view rules)
            return [{"entity_id": e} for e in self.view_hidden]
        if "SELECT entity_id FROM entities" in sql:  # hidden_for all-ids scan
            return [{"entity_id": e} for e in self.all_ids]
        return []


def U(role="user", pages=None):
    return AuthUser(id=1, username="u", role=role, token_version=0, allowed_pages=pages, can_control=True)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


pool = FakePool()
# view-hide rules still apply on top of page scope
pool_hidden = FakePool(view_hidden={"mqtt:light", "tuya:lock"})


def test_page_allowed_ids():
    # page_allowed_ids: narrow vs broad vs admin
    assert run(page_allowed_ids(pool, U("user", ["entry"]))) == ENTRY_IDS, \
        "ulaz-only user is scoped to exactly the entry entities"
    assert run(page_allowed_ids(pool, U("user", ["media"]))) is None, \
        "a broad page (media) removes the entity restriction"
    assert run(page_allowed_ids(pool, U("user", ["entry", "media"]))) is None, \
        "any broad page present → unrestricted"
    assert run(page_allowed_ids(pool, U("admin", ["entry"]))) is None, "admin is never scoped"
    assert run(page_allowed_ids(pool, U("user", None))) is None, "unrestricted user (pages=None)"


def test_hidden_for():
    # hidden_for: folds page scope into the exclude-set
    assert run(hidden_for(pool, U("user", ["entry"]))) == HOUSE, \
        "ulaz-only hides everything outside the entry set (the whole house)"
    assert run(hidden_for(pool, U("user", ["media"]))) == set(), \
        "broad user with no view-hides hides nothing"
    assert run(hidden_for(pool, U("admin", None))) == set(), "admin hides nothing"

    # view-hide rules still apply on top of page scope
    assert "tuya:lock" in run(hidden_for(pool_hidden, U("user", ["entry"]))), \
        "a view-hide on an entry entity still hides it for the ulaz user"


def test_can_view_entity():
    # can_view_entity: single-entity read gate
    assert run(can_view_entity(pool, U("user", ["entry"]), "tuya:lock")) is True, \
        "ulaz user may read an entry entity's history"
    assert run(can_view_entity(pool, U("user", ["entry"]), "sensor:temp")) is False, \
        "ulaz user may NOT read a house entity's history"
    assert run(can_view_entity(pool, U("user", ["media"]), "sensor:temp")) is True, \
        "broad user may read any non-hidden entity"
    assert run(can_view_entity(pool_hidden, U("user", None), "mqtt:light")) is False, \
        "a view-hidden entity is unreadable even for an unrestricted user"
