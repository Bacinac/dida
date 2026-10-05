"""Integration test — what the broker's ops do to a real database.

The adapter side and the seal are pinned in tests/test_broker.py; this is the api
side against Postgres: a secret an adapter stores lands sealed and reads back
plain, another adapter's config comes without its secrets, a sealed house setting
opens only for its reader, and a prune spares what the operator placed while taking
the live values with it. Runs in the api image; DIDA_SECRET_KEY comes from the gate.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dida_api import broker as api
from dida_core import apply_migrations, decrypt_secret, encrypt_secret, jsonb_init, pg_pool

SECRET = os.environ["DIDA_SECRET_KEY"]


@pytest.fixture
async def ctx():
    pool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _cleanup(pool)
    yield api.Ctx(pool, SECRET)
    await _cleanup(pool)
    await pool.close()


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'baba:zzb%' OR entity_id LIKE 'tuya:zzb%'")
    await pool.execute("DELETE FROM devices WHERE device_key LIKE 'baba:zzb%'")
    await pool.execute("DELETE FROM areas WHERE name = 'zzb-room'")
    await pool.execute("DELETE FROM adapter_config WHERE adapter IN ('smartthings', 'samsungtv', 'tuya')")
    await pool.execute("DELETE FROM app_settings WHERE key IN ('opus_token', 'contacts_sync_requested')")
    await pool.execute("DELETE FROM entities WHERE adapter = 'frigate' AND entity_id LIKE '%zzbfront%'")
    await pool.execute("DELETE FROM devices WHERE adapter = 'frigate' AND device_key LIKE '%zzbfront%'")
    await pool.execute("DELETE FROM adapter_config WHERE adapter = 'frigate'")
    await pool.execute("DELETE FROM automations WHERE name = 'zzb-frigate-rule'")
    await pool.execute("DELETE FROM users WHERE username = 'zzb-frigate-user'")


async def _ask(ctx, adapter: str, op: str, **args):
    reply = await api.answer(ctx, adapter, {"op": op, "args": args})
    assert "error" not in reply, reply
    return reply["result"]


async def test_an_adapters_secret_is_sealed_at_rest_and_plain_to_it(ctx):
    await _ask(ctx, "smartthings", "store", key="_oauth", value='{"refresh_token": "r1"}')
    raw = await ctx.pool.fetchval("SELECT value FROM adapter_config WHERE adapter = 'smartthings' AND key = '_oauth'")
    assert "\"refresh_token\"" not in raw and decrypt_secret(SECRET, raw) == '{"refresh_token": "r1"}'
    assert await _ask(ctx, "smartthings", "stored", key="_oauth") == '{"refresh_token": "r1"}'

    await _ask(ctx, "samsungtv", "store", key="_device", value='{"mac": "aa"}')
    raw = await ctx.pool.fetchval("SELECT value FROM adapter_config WHERE adapter = 'samsungtv' AND key = '_device'")
    assert raw == '{"mac": "aa"}', "what is not a secret stays readable in the database"


async def test_token_refresh_cannot_restore_a_deleted_or_replaced_grant(ctx):
    old, rolled, replacement = '{"refresh_token":"old"}', '{"refresh_token":"rolled"}', '{"refresh_token":"new-account"}'
    await _ask(ctx, "smartthings", "store", key="_oauth", value=old)
    assert await _ask(ctx, "smartthings", "store_if_current", key="_oauth", current=old, value=rolled) is True
    await ctx.pool.execute("DELETE FROM adapter_config WHERE adapter = 'smartthings' AND key = '_oauth'")
    assert await _ask(ctx, "smartthings", "store_if_current", key="_oauth", current=rolled, value=old) is False
    assert await _ask(ctx, "smartthings", "stored", key="_oauth") is None
    await _ask(ctx, "smartthings", "store", key="_oauth", value=replacement)
    assert await _ask(ctx, "smartthings", "store_if_current", key="_oauth", current=rolled, value=old) is False
    assert await _ask(ctx, "smartthings", "stored", key="_oauth") == replacement
    assert "error" in await api.answer(ctx, "presence", {"op": "store_if_current", "args": {}})


async def test_concurrent_refreshes_have_only_one_winner(ctx):
    await _ask(ctx, "smartthings", "store", key="_oauth", value="old")
    results = await asyncio.gather(*(_ask(ctx, "smartthings", "store_if_current", key="_oauth", current="old", value=value)
                                    for value in ("rolled-a", "rolled-b")))
    assert sorted(results) == [False, True]
    assert await _ask(ctx, "smartthings", "stored", key="_oauth") in ("rolled-a", "rolled-b")


async def test_frigate_site_migration_moves_live_state_references_and_permissions(ctx):
    old, new = "frigate:zzbfront", "frigate:home:zzbfront"
    await _ask(ctx, "frigate", "store", key="sites", value=json.dumps([
        {"name": "Home", "url": "http://home.test"}, {"name": "Cabin", "url": "http://cabin.test"}]))
    await ctx.pool.execute("INSERT INTO devices (device_key, adapter, site) VALUES ($1, 'frigate', 'Home')", old)
    for entity, caps in ((old, ["camera"]), (old + ":detect", ["on_off"]), (old + ":zone:door", ["occupancy"])):
        await ctx.pool.execute("INSERT INTO entities (entity_id, adapter, device_key, capabilities) VALUES ($1, 'frigate', $2, $3)",
                               entity, old, caps)
    await ctx.pool.execute("INSERT INTO current_state (entity_id, capability, value, ts_ns) VALUES ($1, 'on_off', 'true', 1)", old + ":detect")
    await ctx.pool.execute("INSERT INTO automations (name, definition) VALUES ('zzb-frigate-rule', $1)",
                           {"triggers": [{"entity_id": old + ":zone:door"}], "actions": [{"entity_id": old + ":detect"}]})
    uid = await ctx.pool.fetchval("INSERT INTO users (username, password_hash) VALUES ('zzb-frigate-user', 'unused') RETURNING id")
    await ctx.pool.execute("INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'view', 'entity', $2)", uid, old)
    result = await _ask(ctx, "frigate", "frigate_migrate")
    assert result["entities"] == 3 and result["references"] >= 2
    assert await ctx.pool.fetchval("SELECT value FROM current_state WHERE entity_id = $1", new + ":detect") is True
    definition = await ctx.pool.fetchval("SELECT definition FROM automations WHERE name = 'zzb-frigate-rule'")
    assert definition["triggers"][0]["entity_id"] == new + ":zone:door"
    assert definition["actions"][0]["entity_id"] == new + ":detect"
    assert await ctx.pool.fetchval("SELECT ref FROM user_access_rules WHERE user_id = $1", uid) == new
    assert await _ask(ctx, "frigate", "frigate_migrate") == {"entities": 0, "references": 0}
    assert "error" in await api.answer(ctx, "presence", {"op": "frigate_migrate", "args": {}})


@pytest.mark.parametrize("site", [None, "Removed site"])
async def test_ambiguous_frigate_migration_leaves_existing_identity_untouched(ctx, site):
    await _ask(ctx, "frigate", "store", key="sites", value=json.dumps([
        {"name": "Home", "url": "http://home.test"}, {"name": "Cabin", "url": "http://cabin.test"}]))
    await ctx.pool.execute("INSERT INTO devices (device_key, adapter, site) VALUES ('frigate:zzbfront', 'frigate', $1)", site)
    await ctx.pool.execute("INSERT INTO entities (entity_id, adapter, device_key, capabilities) VALUES ('frigate:zzbfront', 'frigate', 'frigate:zzbfront', '[\"camera\"]')")
    result = await api.answer(ctx, "frigate", {"op": "frigate_migrate", "args": {}})
    assert "unambiguous site" in result["error"]
    assert await ctx.pool.fetchval("SELECT entity_id FROM entities WHERE entity_id = 'frigate:zzbfront'")


async def test_another_adapters_config_comes_without_its_secrets(ctx):
    await _ask(ctx, "presence", "store", key="username", value="house")
    await _ask(ctx, "presence", "store", key="password", value="s3cr3t")
    own = await _ask(ctx, "presence", "config")
    assert own["username"] == "house" and own["password"] == "s3cr3t"
    other = await _ask(ctx, "unifi", "config", of="presence")
    assert other["username"] == "house" and "password" not in other


async def test_a_sealed_setting_opens_for_its_reader_only(ctx):
    await ctx.pool.execute(
        "INSERT INTO app_settings (key, value) VALUES ('opus_token', $1)", encrypt_secret(SECRET, "tok"))
    assert await _ask(ctx, "opus", "setting", key="opus_token") == "tok"
    reply = await api.answer(ctx, "cast", {"op": "setting", "args": {"key": "opus_token"}})
    assert "error" in reply


async def test_a_request_is_taken_once(ctx):
    await ctx.pool.execute("INSERT INTO app_settings (key, value) VALUES ('contacts_sync_requested', '1')")
    assert await _ask(ctx, "contacts", "take_setting", key="contacts_sync_requested") == "1"
    assert await _ask(ctx, "contacts", "take_setting", key="contacts_sync_requested") is None


async def test_the_prune_spares_a_placement_but_not_its_value(ctx):
    area = await ctx.pool.fetchval("INSERT INTO areas (name) VALUES ('zzb-room') RETURNING id")
    cam, placed, loose = "baba:zzb-cam", "baba:zzb-cam:scene:p1", "baba:zzb-cam:scene:p2"
    await ctx.pool.execute("INSERT INTO devices (device_key, adapter) VALUES ($1, 'baba')", cam)
    for eid in (cam, placed, loose):
        await ctx.pool.execute(
            "INSERT INTO entities (entity_id, adapter, device_key, area_id) VALUES ($1, 'baba', $2, $3)",
            eid, cam, area if eid == placed else None)
        await ctx.pool.execute(
            "INSERT INTO current_state (entity_id, capability, value) VALUES ($1, 'motion', 'true'::jsonb)", eid)
    # Same device key under another adapter: not baba's to take.
    await ctx.pool.execute(
        "INSERT INTO entities (entity_id, adapter, device_key) VALUES ('tuya:zzb-x', 'tuya', $1)", cam)

    assert await _ask(ctx, "baba", "forget", device_keys=[cam], keep_placed=True) == {"kept": 1}
    left = {r["entity_id"] for r in await ctx.pool.fetch(
        "SELECT entity_id FROM entities WHERE device_key = $1", cam)}
    assert left == {placed, "tuya:zzb-x"}
    assert await ctx.pool.fetchval(
        "SELECT count(*) FROM current_state WHERE starts_with(entity_id, 'baba:zzb')") == 0, \
        "a kept row does not keep a value its source no longer stands behind"
    assert await ctx.pool.fetchval("SELECT count(*) FROM devices WHERE device_key = $1", cam) == 1

    assert await _ask(ctx, "baba", "forget", entity_ids=[placed]) == {"kept": 0}
    await ctx.pool.execute("DELETE FROM entities WHERE entity_id = 'tuya:zzb-x'")
    await _ask(ctx, "baba", "forget", device_keys=[cam])
    assert await ctx.pool.fetchval("SELECT count(*) FROM devices WHERE device_key = $1", cam) == 0, \
        "the device row goes with its last entity"


async def test_state_comes_decoded_with_its_age(ctx):
    await ctx.pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ('baba:zzb-s', 'baba')")
    await ctx.pool.execute(
        "INSERT INTO current_state (entity_id, capability, value, updated_at) "
        "VALUES ('baba:zzb-s', 'location', '\"Home\"'::jsonb, now() - interval '90 seconds')")
    [row] = await _ask(ctx, "baba", "state", entity_ids=["baba:zzb-s"])
    assert row["value"] == "Home"
    assert 85 < row["age"] < 120


async def test_the_matter_bridge_hears_what_the_house_voices(ctx):
    rows = [("tuya:zzb-lamp", "light", None, True, {"on_off": True, "brightness": 40}),
            ("tuya:zzb-plug", "switch", None, True, {"on_off": False}),
            ("tuya:zzb-ac", "climate", None, True, {"on_off": True, "hvac_mode": "cool"}),
            ("tuya:zzb-gate", "cover", "Kapija", True, {"on_off": False}),
            ("tuya:zzb-mute", "light", None, False, {"on_off": True}),
            ("tuya:zzb-hub", "remote", None, False, {"source": "PS5", "source_options": ["PS5", "TV"]})]
    for eid, device_type, label, voiced, states in rows:
        await ctx.pool.execute(
            "INSERT INTO entities (entity_id, adapter, name, label, device_type, voice_exposed) "
            "VALUES ($1, 'tuya', $2, $3, $4, $5)", eid, eid.split(":")[1], label, device_type, voiced)
        for cap, value in states.items():
            await ctx.pool.execute(
                "INSERT INTO current_state (entity_id, capability, value) VALUES ($1, $2, $3)", eid, cap, value)

    voice = await _ask(ctx, "matter-bridge", "voice", activities=["tuya:zzb-hub"])

    def ours(kind: str) -> dict[str, str]:
        return {e["entity_id"]: e["name"] for e in voice[kind]["entities"] if "zzb" in e["entity_id"]}

    assert ours("controllables") == {"tuya:zzb-lamp": "zzb-lamp", "tuya:zzb-plug": "zzb-plug"}, \
        "an AC is a thermostat, a gate a covering"
    kinds = {e["entity_id"]: e["device_type"] for e in voice["controllables"]["entities"] if "zzb" in e["entity_id"]}
    assert kinds == {"tuya:zzb-lamp": "light", "tuya:zzb-plug": "switch"}, "only a light may answer to 'the lights'"
    assert ours("climates") == {"tuya:zzb-ac": "zzb-ac"}
    assert ours("covers") == {"tuya:zzb-gate": "Kapija"}, "a gate is named by its label"
    assert ours("activities") == {"tuya:zzb-hub": "zzb-hub"}
    lamp = {s["capability"]: s["value"] for s in voice["controllables"]["states"] if s["entity_id"] == "tuya:zzb-lamp"}
    assert lamp == {"on_off": True, "brightness": 40}
    assert {s["capability"] for s in voice["activities"]["states"]} == {"source", "source_options"}
    assert not [s for k in voice.values() for s in k["states"] if s["entity_id"] == "tuya:zzb-mute"]
