import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import dida_api.app as appmod
import pytest
import pytest_asyncio
from dida_api.auth import SESSION_COOKIE, TOKEN_TTL, encode_session_token
from dida_api.hub import Hub
from dida_core import StateUpdate, apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

PUBLIC = "zzaudit:gate"
PRIVATE = "zzaudit:meter"
VALVE = "zzaudit:valve"
SECRET = "audit-live-access-secret-0123456789abcdef"


@pytest_asyncio.fixture
async def access():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM users WHERE username IN ('zzauditadmin', 'zzaudituser')")
    entity_ids = [PUBLIC, PRIVATE, VALVE, "dreame:vacuum"]
    device_keys = ["zzaudit:public-device", "zzaudit:private-device"]
    await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1::text[])", entity_ids)
    await pool.execute("DELETE FROM devices WHERE device_key = ANY($1::text[])", device_keys)
    await pool.execute("DELETE FROM areas WHERE name IN ('zzaudit_entry', 'zzaudit_private')")
    area = await pool.fetchval("INSERT INTO areas (name) VALUES ('zzaudit_entry') RETURNING id")
    other = await pool.fetchval("INSERT INTO areas (name, fp_poly) VALUES ('zzaudit_private', $1) RETURNING id",
                              [[0, 0], [10, 0], [10, 10]])
    for key in device_keys:
        await pool.execute("INSERT INTO devices (device_key, adapter, name) VALUES ($1, 'zzaudit', $1)", key)
    for entity, caps, room, key in ((PUBLIC, ["on_off"], area, device_keys[0]),
                                   (VALVE, ["target_temperature"], area, device_keys[0]),
                                   (PRIVATE, ["energy"], other, device_keys[1]),
                                   ("dreame:vacuum", ["vacuum"], other, device_keys[1])):
        await pool.execute("INSERT INTO entities (entity_id, adapter, capabilities, area_id, device_key) "
                           "VALUES ($1, 'zzaudit', $2, $3, $4)", entity, caps, room, key)
    await pool.execute("UPDATE areas SET sensor_config = $1, media_config = $2 WHERE id = $3",
                       {"included": [f"{PUBLIC}:on_off", f"{PRIVATE}:energy"]},
                       {"sources": [{"key": "private", "player": PRIVATE}, {"key": "public", "player": PUBLIC}]}, area)
    previous_entry = await pool.fetchval("SELECT value FROM app_settings WHERE key = 'entry_controls'")
    await pool.execute("INSERT INTO app_settings (key, value) VALUES ('entry_controls', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps({"car": PUBLIC}))
    ids = {}
    for username, role in (("zzauditadmin", "admin"), ("zzaudituser", "user")):
        ids[role] = await pool.fetchval(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3) RETURNING id",
            username, await hash_password("auditpassword123"), role)
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = SECRET
    appmod.app.state.hub = Hub()
    appmod.app.state.ch = None
    data = SimpleNamespace(pool=pool, ids=ids, area=area, other=other)
    data.token = lambda role="user": encode_session_token(ids[role], SECRET, ttl=TOKEN_TTL)
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as client:
        data.client = client
        try:
            yield data
        finally:
            await appmod.app.state.hub.disconnect_user(ids["user"], 4401)
            await pool.execute("DELETE FROM users WHERE id = ANY($1::bigint[])", list(ids.values()))
            await pool.execute("DELETE FROM entities WHERE entity_id = ANY($1::text[])", entity_ids)
            await pool.execute("DELETE FROM devices WHERE device_key = ANY($1::text[])", device_keys)
            await pool.execute("DELETE FROM areas WHERE id = ANY($1::bigint[])", [area, other])
            if previous_entry is None:
                await pool.execute("DELETE FROM app_settings WHERE key = 'entry_controls'")
            else:
                await pool.execute("UPDATE app_settings SET value = $1 WHERE key = 'entry_controls'", previous_entry)
            await pool.close()


@asynccontextmanager
async def socket(token):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "query_string": b"",
             "headers": [(b"host", b"itest"), (b"cookie", f"{SESSION_COOKIE.name}={token}".encode())],
             "client": ("127.0.0.1", 12000), "server": ("itest", 80), "subprotocols": []}
    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(appmod.app(scope, incoming.get, outgoing.put))

    async def message(text):
        await incoming.put({"type": "websocket.receive", "text": text})

    async def receive():
        return await asyncio.wait_for(outgoing.get(), 3)

    try:
        assert (await receive())["type"] == "websocket.accept"
        await message("ping")
        assert json.loads((await receive())["text"]) == {"type": "pong"}
        yield SimpleNamespace(send=message, receive=receive, task=task)
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 3)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def state(entity_id, capability, value):
    return StateUpdate(entity_id=entity_id, capability=capability, value=value, adapter="zzaudit", ts_ns=1)


async def test_logout_closes_only_the_signed_out_token(access):
    first, second = access.token(), access.token()
    async with socket(first) as a, socket(second) as b:
        response = await access.client.post("/auth/logout", headers=bearer(first))
        assert response.status_code == 204
        assert (await a.receive())["code"] == 4401
        await appmod.app.state.hub.on_event(state(PRIVATE, "energy", 1.0))
        assert json.loads((await b.receive())["text"])["entity_id"] == PRIVATE
        assert len(appmod.app.state.hub._clients) == 1
        assert (await access.client.get("/auth/me", headers=bearer(first))).status_code == 401
        assert (await access.client.get("/auth/me", headers=bearer(second))).status_code == 200


@pytest.mark.parametrize("patch,code", [({"password": "newpassword123"}, 4401),
                                       ({"allowed_pages": ["entry"]}, 1012),
                                       ({"view_hides": [{"scope": "entity", "ref": PRIVATE}]}, 1012),
                                       ({"can_control": True}, 1012), ({"role": "admin"}, 1012)])
async def test_user_mutations_disconnect_existing_streams(access, patch, code):
    async with socket(access.token()) as ws:
        response = await access.client.patch(f"/users/{access.ids['user']}", json=patch,
                                             headers=bearer(access.token("admin")))
        assert response.status_code == 204
        assert (await ws.receive())["code"] == code
        assert not appmod.app.state.hub._clients
        await appmod.app.state.hub.on_event(state(PRIVATE, "energy", 1.0))


async def test_delete_closes_existing_stream(access):
    async with socket(access.token()) as ws:
        response = await access.client.delete(f"/users/{access.ids['user']}", headers=bearer(access.token("admin")))
        assert response.status_code == 204
        assert (await ws.receive())["code"] == 4401


async def test_external_token_version_change_is_caught_on_keepalive(access):
    async with socket(access.token()) as ws:
        await access.pool.execute("UPDATE users SET token_version = token_version + 1 WHERE id = $1", access.ids["user"])
        await ws.send("ping")
        assert (await ws.receive())["code"] == 4401


async def test_self_password_change_reissues_cookie_and_closes_old_streams(access):
    token = access.token()
    async with socket(token) as ws:
        response = await access.client.post("/auth/change-password", headers=bearer(token),
            json={"old_password": "auditpassword123", "new_password": "newpassword123"})
        assert response.status_code == 204
        assert (await ws.receive())["code"] == 1012
        replacement = access.client.cookies.get(SESSION_COOKIE.name)
        assert replacement and replacement != token
        assert (await access.client.get("/auth/me")).status_code == 200
        access.client.cookies.clear()
        assert (await access.client.get("/auth/me", headers=bearer(token))).status_code == 401
        assert (await access.client.get("/auth/me", headers=bearer(replacement))).status_code == 200


async def test_invalid_patch_rolls_back_all_access_changes(access):
    async with socket(access.token()) as ws:
        response = await access.client.patch(f"/users/{access.ids['user']}",
            json={"role": "admin", "view_hides": [{"scope": "invalid", "ref": PRIVATE}]},
            headers=bearer(access.token("admin")))
        assert response.status_code == 400
        assert await access.pool.fetchval("SELECT role FROM users WHERE id = $1", access.ids["user"]) == "user"
        await ws.send("ping")
        assert json.loads((await ws.receive())["text"])["type"] == "pong"


async def test_entry_scope_covers_detail_stats_areas_and_floors(access):
    await access.pool.execute("UPDATE users SET allowed_pages = ARRAY['entry'] WHERE id = $1", access.ids["user"])
    headers = bearer(access.token())
    for suffix in ("detail", "stats"):
        assert (await access.client.get(f"/entities/{PRIVATE}/{suffix}", headers=headers)).status_code == 404
    assert (await access.client.get(f"/entities/{PUBLIC}/detail", headers=headers)).status_code == 200
    areas = (await access.client.get("/areas", headers=headers)).json()
    assert [row["id"] for row in areas] == [access.area]
    assert areas[0]["fp_poly"] is None and areas[0]["media_config"] is None
    assert (await access.client.get("/floors", headers=headers)).json() == []
    async with socket(access.token()) as ws:
        await appmod.app.state.hub.on_event(state(PRIVATE, "energy", 1.0))
        await appmod.app.state.hub.on_event(state(PUBLIC, "on_off", True))
        assert json.loads((await ws.receive())["text"])["entity_id"] == PUBLIC


async def test_view_hides_are_removed_from_area_configuration(access):
    await access.pool.execute("INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'view', 'entity', $2)",
                             access.ids["user"], PRIVATE)
    areas = (await access.client.get("/areas", headers=bearer(access.token()))).json()
    room = next(row for row in areas if row["id"] == access.area)
    assert room["sensor_config"]["included"] == [f"{PUBLIC}:on_off"]
    assert [source["key"] for source in room["media_config"]["sources"]] == ["public"]
    assert PRIVATE not in json.dumps(areas)


@pytest.mark.parametrize("path", ["/history/energy", "/history/energy/hourly?frm=1&to=2",
                                  "/history/energy/config", "/vacuum/map", "/vacuum/live"])
async def test_narrow_guest_is_rejected_before_upstream_reads(access, path):
    await access.pool.execute("UPDATE users SET allowed_pages = ARRAY['entry'] WHERE id = $1", access.ids["user"])
    appmod.app.state.bus = None
    assert (await access.client.get(path, headers=bearer(access.token()))).status_code == 403


@pytest.mark.parametrize("path", ["/vacuum/map", "/vacuum/live"])
async def test_hidden_vacuum_geometry_is_not_read(access, path):
    await access.pool.execute("INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'view', 'entity', 'dreame:vacuum')",
                             access.ids["user"])
    appmod.app.state.bus = None
    assert (await access.client.get(path, headers=bearer(access.token()))).status_code == 404


async def test_house_balance_does_not_reveal_hidden_meters(access):
    await access.pool.execute("INSERT INTO user_access_rules (user_id, kind, scope, ref) VALUES ($1, 'view', 'entity', $2)",
                             access.ids["user"], PRIVATE)
    assert (await access.client.get("/history/energy", headers=bearer(access.token()))).status_code == 403


@pytest.mark.parametrize("page", ["entry", "floorplan", "devices", "cameras", "media", "heating",
                                  "assistant", "history", "adapters"])
async def test_every_selectable_page_loads_a_scoped_snapshot_and_live_stream(access, page):
    await access.pool.execute("UPDATE users SET allowed_pages = $2 WHERE id = $1", access.ids["user"], [page])
    headers = bearer(access.token())
    responses = [await access.client.get(path, headers=headers) for path in ("/entities", "/state", "/areas", "/devices")]
    assert all(response.status_code == 200 for response in responses)
    ids = {row["entity_id"] for row in responses[0].json()}
    if page == "entry":
        assert ids == {PUBLIC}
    elif page == "heating":
        assert ids == {VALVE}
        assert (await access.client.get("/heating", headers=headers)).json()["rooms"][0]["valves"] == [VALVE]
    else:
        assert {PUBLIC, PRIVATE, VALVE, "dreame:vacuum"} <= ids
    keys = {row["device_key"] for row in responses[3].json()}
    assert "zzaudit:public-device" in keys
    assert ("zzaudit:private-device" in keys) == (page not in ("entry", "heating"))
    async with socket(access.token()) as ws:
        if page in ("entry", "heating"):
            await appmod.app.state.hub.on_event(state("zzaudit:new-private", "on_off", True))
        entity = VALVE if page == "heating" else PUBLIC
        await appmod.app.state.hub.on_event(state(entity, "on_off", True))
        assert json.loads((await ws.receive())["text"])["entity_id"] == entity


async def test_scope_change_reconnects_with_fresh_visibility(access):
    token = access.token()
    async with socket(token) as ws:
        response = await access.client.patch(f"/users/{access.ids['user']}", json={"allowed_pages": ["entry"]},
                                             headers=bearer(access.token("admin")))
        assert response.status_code == 204
        assert (await ws.receive())["code"] == 1012
    async with socket(token) as ws:
        await appmod.app.state.hub.on_event(state(PRIVATE, "energy", 1.0))
        await appmod.app.state.hub.on_event(state(PUBLIC, "on_off", True))
        assert json.loads((await ws.receive())["text"])["entity_id"] == PUBLIC
