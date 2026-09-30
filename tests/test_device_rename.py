"""A renamed device must move, not fork — with everything that names it.

`entity_id` is derived from the device's NAME, so renaming a device in
zigbee2mqtt or the Tuya app changes its id. Until now that produced a duplicate:
a new entity under the new slug, the old one stale beside it, and every
automation, scene, area and schedule still naming the id that stopped updating.
Nothing fails. The rules load, evaluate, and never fire again. Measured against
this installation's production data: renaming one commonly-referenced entity would
silently kill ten rules.

The adapter now reports the device's native, immutable key (zigbee2mqtt's
`ieee_address` — verified present on all 30 devices here), so the engine can tell
a rename from a new device and migrate instead of forking.

Against a REAL database, because the whole claim is about a transaction across
seven tables — entities, current_state (by cascade), devices, and four kinds of
rule. A stub would prove the code calls itself.
"""

from __future__ import annotations

import json

from dida_core import EntityInfo, apply_migrations, pg_pool
from dida_engine.__main__ import Engine


class StubBus:
    async def publish_raw(self, subject, payload):
        pass

    async def flush(self, timeout=5.0):
        pass

    async def publish_journal(self, event):
        pass


class StubHistory:
    def enqueue(self, *a):
        pass


async def _fresh_pool():
    # No jsonb codec: the engine's production pool has none, and its writes are
    # JSON text that Postgres parses. With a codec they would be encoded twice.
    pool = await pg_pool(min_size=1, max_size=4)
    await apply_migrations(pool, "db/migrations")
    for sql in (
        "DELETE FROM current_state WHERE entity_id LIKE 'ren:%'",
        "DELETE FROM entities WHERE entity_id LIKE 'ren:%'",
        "DELETE FROM devices WHERE device_key IN ('old_name', 'new_name', 'taken')",
        "DELETE FROM automations WHERE name LIKE 'rename-test%'",
        "DELETE FROM scenes WHERE name LIKE 'rename-test%'",
        "DELETE FROM areas WHERE name LIKE 'rename-test%'",
        "DELETE FROM app_settings WHERE key IN ('entry_controls', 'energy_config', 'radio_player')",
    ):
        await pool.execute(sql)
    return pool


async def _announce(engine, slug, native, *, field=None):
    eid = f"ren:{slug}" + (f":{field}" if field else "")
    await engine.on_entity_info(EntityInfo(
        entity_id=eid, adapter="ren", capabilities=["on_off"], name=slug,
        device=slug, device_name=slug, device_type="switch", native_key=native))
    return eid


async def test_the_same_device_under_a_new_name_moves_instead_of_forking():
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await _announce(engine, "old_name", "0xAABB", field="power")

        await _announce(engine, "new_name", "0xAABB")

        ids = [r["entity_id"] for r in await pool.fetch(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'ren:%' ORDER BY 1")]
        assert ids == ["ren:new_name", "ren:new_name:power"], \
            f"expected the device to MOVE, got {ids}"
        assert await pool.fetchval(
            "SELECT count(*) FROM devices WHERE device_key = 'old_name'") == 0
        assert await pool.fetchval(
            "SELECT native_key FROM devices WHERE device_key = 'new_name'") == "0xAABB"
    finally:
        await pool.close()


async def test_the_rules_that_named_it_are_rewritten():
    """The half that matters: moving the entity while leaving the rules behind
    would turn a stale duplicate into a reference that points at nothing."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await pool.execute(
            "INSERT INTO automations (name, definition, enabled) VALUES ($1, $2, true)",
            "rename-test-typed",
            json.dumps({"triggers": [{"entity_id": "ren:old_name"}],
                        "actions": [{"entity_id": "ren:old_name", "command": "turn_on"}]}))
        await pool.execute(
            "INSERT INTO automations (name, definition, enabled) VALUES ($1, $2, true)",
            "rename-test-script",
            json.dumps({"script": 'turn_on("ren:old_name")\nnotify("marko", "ren:old_name on")'}))
        await pool.execute(
            "INSERT INTO automations (name, definition, enabled) VALUES ($1, $2, true)",
            "rename-test-untouched",
            json.dumps({"actions": [{"entity_id": "ren:someone_else"}]}))

        await _announce(engine, "new_name", "0xAABB")

        rows = {r["name"]: r["definition"] for r in await pool.fetch(
            "SELECT name, definition FROM automations WHERE name LIKE 'rename-test%'")}
        typed = rows["rename-test-typed"]
        typed = json.loads(typed)
        assert typed["triggers"][0]["entity_id"] == "ren:new_name"
        assert typed["actions"][0]["entity_id"] == "ren:new_name"

        scripted = rows["rename-test-script"]
        scripted = json.loads(scripted)
        assert 'turn_on("ren:new_name")' in scripted["script"]
        assert '"ren:old_name on"' in scripted["script"], \
            "the notify TEXT was rewritten — only entity arguments may move"

        other = rows["rename-test-untouched"]
        other = json.loads(other)
        assert other["actions"][0]["entity_id"] == "ren:someone_else", \
            "an unrelated rule was rewritten"
    finally:
        await pool.close()


async def test_settings_and_room_configs_that_named_it_are_rewritten():
    """They name devices under their own field names, which the rule walk never
    looked at: a rename left the gate button and the room's player pointing at the
    old id."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await _announce(engine, "old_name", "0xAABB", field="energy")
        await pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ('entry_controls', $1), "
            "('energy_config', $2), ('radio_player', 'ren:old_name')",
            json.dumps({"car": "ren:old_name", "door": "ren:someone_else", "notify_on_open": True}),
            json.dumps({"ren:old_name:energy": "submeter"}))
        await pool.execute(
            "INSERT INTO areas (name, media_config, sensor_config) VALUES ($1, $2, $3)",
            "rename-test-area",
            json.dumps({"sources": [{"key": "music", "player": "ren:old_name"}]}),
            json.dumps({"excluded": ["ren:old_name:power"], "hidden": ["power"]}))
        await _announce(engine, "new_name", "0xAABB")

        settings = {r["key"]: r["value"] for r in await pool.fetch(
            "SELECT key, value FROM app_settings "
            "WHERE key IN ('entry_controls', 'energy_config', 'radio_player')")}
        assert json.loads(settings["entry_controls"]) == \
            {"car": "ren:new_name", "door": "ren:someone_else", "notify_on_open": True}
        assert json.loads(settings["energy_config"]) == {"ren:new_name:energy": "submeter"}
        assert settings["radio_player"] == "ren:new_name"

        area = await pool.fetchrow(
            "SELECT media_config, sensor_config FROM areas WHERE name = 'rename-test-area'")
        assert json.loads(area["media_config"])["sources"][0]["player"] == "ren:new_name"
        assert json.loads(area["sensor_config"]) == \
            {"excluded": ["ren:new_name:power"], "hidden": ["power"]}
    finally:
        await pool.close()


async def test_the_live_state_moves_with_the_entity():
    """`current_state` references `entities` — the rename relies on ON UPDATE
    CASCADE (migration 0070). Without it the update is rejected by the foreign key
    at the last step, after the rules have already been rewritten."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await pool.execute(
            "INSERT INTO current_state (entity_id, capability, value, ts_ns) "
            "VALUES ($1, 'on_off', $2, 1)", "ren:old_name", json.dumps(True))

        await _announce(engine, "new_name", "0xAABB")

        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = 'ren:new_name'") == 1
        assert await pool.fetchval(
            "SELECT count(*) FROM current_state WHERE entity_id = 'ren:old_name'") == 0
    finally:
        await pool.close()


async def test_a_device_with_no_native_key_behaves_exactly_as_before():
    """Most adapters have no stable id — SSDP and mDNS discover by friendly name.
    They must keep working, which means a rename there still forks. Stating it is
    the point: the guarantee is per-adapter, not global."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", None)
        await _announce(engine, "new_name", None)
        ids = sorted(r["entity_id"] for r in await pool.fetch(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'ren:%'"))
        assert ids == ["ren:new_name", "ren:old_name"]
    finally:
        await pool.close()


async def test_a_different_device_with_a_different_key_is_not_a_rename():
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await _announce(engine, "new_name", "0xCCDD")
        ids = sorted(r["entity_id"] for r in await pool.fetch(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'ren:%'"))
        assert ids == ["ren:new_name", "ren:old_name"], "two devices were merged"
    finally:
        await pool.close()


async def test_renaming_INTO_an_existing_id_is_refused_rather_than_merged():
    """Two devices whose names slug to the same thing. Proceeding would merge one
    device's state into another's — silent, and unrecoverable from the outside."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await _announce(engine, "taken", "0xEEFF")

        await engine.on_entity_info(EntityInfo(
            entity_id="ren:taken", adapter="ren", capabilities=["on_off"],
            name="taken", device="taken", device_name="taken", native_key="0xAABB"))

        assert await pool.fetchval(
            "SELECT count(*) FROM entities WHERE entity_id = 'ren:old_name'") == 1, \
            "the refused rename moved the entity anyway"
    finally:
        await pool.close()


async def test_re_announcing_the_same_name_is_not_a_rename():
    """Adapters re-announce on every reconnect. Treating that as a rename would
    rewrite every rule in the house on each restart."""
    pool = await _fresh_pool()
    engine = Engine(StubBus(), pool, StubHistory())
    try:
        await _announce(engine, "old_name", "0xAABB")
        await pool.execute(
            "INSERT INTO automations (name, definition, enabled) VALUES ($1, $2, true)",
            "rename-test-stable",
            json.dumps({"actions": [{"entity_id": "ren:old_name"}]}))
        for _ in range(3):
            await _announce(engine, "old_name", "0xAABB")
        row = await pool.fetchval(
            "SELECT definition FROM automations WHERE name = 'rename-test-stable'")
        row = json.loads(row)
        assert row["actions"][0]["entity_id"] == "ren:old_name"
    finally:
        await pool.close()
