"""Integration test — the per-device entity routes (dida_api.entities) end to end
against a real Postgres: `GET /entities` (any signed-in user, minus their hidden
set) lists the registry with its full column projection; `PATCH /entities/{id}`
(admin only) renames, retypes, toggles exposure, hides capabilities and places an
entity on the floor-plan — with its guards: an empty body is 400, an off-vocabulary
device_type is 400, and an unknown id is 404. `POST /areas/auto-assign` derives a
room (kind + optional proper name) from each unassigned entity's id and assigns it,
reusing rooms and never duplicating, leaving keyword-less entities untouched.

(The plain /areas CRUD lives in the same module but is covered by test_api_areas.py;
this file targets the entity-management + auto-assign surface.)

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. rate_limit.LOGIN is reset per-test by tests/conftest.py.
"""

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def test_entities_management():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%'")
    await pool.execute("DELETE FROM areas WHERE name LIKE 'zztest_ent_area%'")
    await pool.execute("DELETE FROM users WHERE username IN ('entadmin', 'entuser')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('entadmin', $1, 'admin')", await hash_password("adminpw12")
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('entuser', $1, 'user')", await hash_password("userpw12")
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, name, adapter, device_type, capabilities) "
        "VALUES ('zztest:ent_light', 'Raw Adapter Name', 'mqtt', 'light', '[\"on_off\", \"brightness\"]'::jsonb)"
    )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "entities-mgmt-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "entadmin", "password": "adminpw12"})
        assert r.status_code == 200, "admin login"

        # GET /entities → the test entity with the full column projection
        r = await c.get("/entities")
        assert r.status_code == 200
        ent = next(e for e in r.json() if e["entity_id"] == "zztest:ent_light")
        assert {"entity_id", "name", "label", "adapter", "device_type", "area_id", "exposed",
                "voice_exposed", "hidden_caps", "capabilities", "fp_floor", "last_seen"} <= set(ent), "documented columns present"
        assert ent["adapter"] == "mqtt" and ent["device_type"] == "light"
        assert ent["exposed"] is True and ent["label"] is None, "defaults: exposed, no user label"
        assert ent["capabilities"] == ["on_off", "brightness"], "jsonb caps decoded to a list"

        # PATCH with an empty body → 400
        r = await c.patch("/entities/zztest:ent_light", json={})
        assert r.status_code == 400, "empty patch body is rejected"

        # PATCH an off-vocabulary device_type → 400 (device_type is DIDA's canon)
        r = await c.patch("/entities/zztest:ent_light", json={"device_type": "banana"})
        assert r.status_code == 400, "an invalid device_type is rejected"

        # PATCH an unknown entity → 404
        r = await c.patch("/entities/zztest:ghost", json={"label": "nobody"})
        assert r.status_code == 404, "patching a non-existent entity is 404"

        # PATCH success: rename, retype, un-expose, hide a cap, place on the plan
        patch_body = {
            "label": "Kitchen Light",
            "device_type": "switch",
            "exposed": False,
            "voice_exposed": True,
            "hidden_caps": ["brightness"],
            "fp_floor": "ground",
            "fp_x": 10.5,
            "fp_y": 20.25,
        }
        r = await c.patch("/entities/zztest:ent_light", json=patch_body)
        assert r.status_code == 200
        patched = r.json()
        assert patched["label"] == "Kitchen Light" and patched["device_type"] == "switch"
        assert patched["exposed"] is False and patched["hidden_caps"] == ["brightness"]
        assert patched["voice_exposed"] is True, "voice_exposed round-trips (its own axis, distinct from exposed)"
        assert patched["fp_floor"] == "ground" and patched["fp_x"] == 10.5 and patched["fp_y"] == 20.25

        # reflected on a fresh GET
        r = await c.get("/entities")
        ent = next(e for e in r.json() if e["entity_id"] == "zztest:ent_light")
        assert ent["label"] == "Kitchen Light" and ent["device_type"] == "switch" and ent["exposed"] is False, \
            "the patch persisted"

        # assigning an area round-trips (area_id FK)
        r = await c.post("/areas", json={"name": "zztest_ent_area1"})
        assert r.status_code == 201, "create an area to assign"
        aid = r.json()["id"]
        r = await c.patch("/entities/zztest:ent_light", json={"area_id": aid})
        assert r.status_code == 200 and r.json()["area_id"] == aid, "entity assigned to the area"

        # require_admin boundary: a non-admin may NOT patch entities
        c2 = AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest")
        r = await c2.post("/auth/login", json={"username": "entuser", "password": "userpw12"})
        assert r.status_code == 200, "non-admin login"
        r = await c2.patch("/entities/zztest:ent_light", json={"label": "hijack"})
        assert r.status_code == 403, "a non-admin cannot patch an entity"
        # but a non-admin CAN read the list (no view rules → sees everything)
        r = await c2.get("/entities")
        assert r.status_code == 200 and any(e["entity_id"] == "zztest:ent_light" for e in r.json()), \
            "an unrestricted non-admin still sees the entity"
        await c2.aclose()

    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%'")
    await pool.execute("DELETE FROM areas WHERE name LIKE 'zztest_ent_area%'")
    await pool.execute("DELETE FROM users WHERE username IN ('entadmin', 'entuser')")
    await pool.close()


async def test_auto_assign_areas():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%'")
    await pool.execute("DELETE FROM users WHERE username = 'assignadmin'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('assignadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    # Two keyword-bearing entities (a generic 'kitchen', a proper-named 'Ada'
    # bedroom) and one with no room keyword — the last must stay unassigned.
    for eid in ("zztest:kitchen_sensor", "zztest:ada_lamp", "zztest:nomatch_widget"):
        await pool.execute("INSERT INTO entities (entity_id, adapter) VALUES ($1, 'mqtt')", eid)

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "entities-assign-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    # Areas that already exist, so we delete only the ones auto-assign creates.
    before = {r["id"] for r in await pool.fetch("SELECT id FROM areas")}
    created: set[int] = set()
    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            r = await c.post("/auth/login", json={"username": "assignadmin", "password": "adminpw12"})
            assert r.status_code == 200, "admin login"

            r = await c.post("/areas/auto-assign")
            assert r.status_code == 200
            body = r.json()
            assert body["assigned"] == 2, "the two keyword entities were assigned; the no-match one skipped"
            assert body["rooms"] >= 2, "at least the two derived rooms are in the cache"

            # each keyword entity now points at a room of the derived kind/name
            kitchen = await pool.fetchrow(
                "SELECT a.kind, a.name FROM entities e JOIN areas a ON a.id = e.area_id "
                "WHERE e.entity_id = 'zztest:kitchen_sensor'"
            )
            assert kitchen["kind"] == "kitchen", "the kitchen sensor was roomed by kind"
            ada = await pool.fetchrow(
                "SELECT a.kind, a.name FROM entities e JOIN areas a ON a.id = e.area_id "
                "WHERE e.entity_id = 'zztest:ada_lamp'"
            )
            assert ada["name"] == "Ada" and ada["kind"] == "bedroom", "the ada lamp got the named bedroom"

            # the keyword-less entity stayed unassigned
            unmatched = await pool.fetchval(
                "SELECT area_id FROM entities WHERE entity_id = 'zztest:nomatch_widget'"
            )
            assert unmatched is None, "an entity with no room keyword is left unassigned"

            created = {r["id"] for r in await pool.fetch("SELECT id FROM areas")} - before
    finally:
        await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:%'")
        if created:
            await pool.execute("DELETE FROM areas WHERE id = ANY($1::bigint[])", list(created))
        await pool.execute("DELETE FROM users WHERE username = 'assignadmin'")
        await pool.close()


async def test_voice_exposure_follows_the_rule_with_overrides():
    """Voice exposure is a RULE (a controllable light/switch/cover) plus optional
    per-entity overrides — not a checklist. The rule lives in one place, a generated
    column, so the Matter bridge and the UI cannot drift from each other.
    """
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'mqtt:zzvoice%'")

    async def add(eid, dtype, caps, category="control", diagnostic=False, override=None):
        # Native list, no json.dumps and no ::jsonb cast: the pool's codec
        # (jsonb_init) encodes it exactly once. Pre-dumping would store the JSON
        # *string* and `capabilities ? 'on_off'` would then be false.
        await pool.execute(
            "INSERT INTO entities (entity_id, adapter, device_type, capabilities, "
            "category, diagnostic, voice_exposed) VALUES ($1,'mqtt',$2,$3,$4,$5,$6)",
            eid, dtype, caps, category, diagnostic, override)

    await add("mqtt:zzvoicelamp", "light", ["on_off"])
    await add("mqtt:zzvoicecover", "cover", ["on_off"])
    await add("mqtt:zzvoicebutton", "button", ["press"])       # nothing to voice
    await add("mqtt:zzvoicesensor", "sensor", ["temperature"])  # nothing to voice
    await add("mqtt:zzvoicelock", "lock", ["lock"])             # deliberately outside
    await add("mqtt:zzvoicecfg", "switch", ["on_off"], category="config")
    await add("mqtt:zzvoicediag", "switch", ["on_off"], diagnostic=True)
    # Overrides in both directions.
    await add("mqtt:zzvoiceoff", "light", ["on_off"], override=False)
    await add("mqtt:zzvoiceac", "sensor", ["hvac_mode"], override=True)

    rows = {
        r["entity_id"]: r["voice_effective"]
        for r in await pool.fetch(
            "SELECT entity_id, voice_effective FROM entities WHERE entity_id LIKE 'mqtt:zzvoice%'"
        )
    }

    assert rows["mqtt:zzvoicelamp"] is True, "a controllable light is voiced by the rule"
    assert rows["mqtt:zzvoicecover"] is True
    assert rows["mqtt:zzvoicebutton"] is False, "a press-only button cannot be voiced"
    assert rows["mqtt:zzvoicesensor"] is False
    assert rows["mqtt:zzvoicelock"] is False, "locks are outside the rule on purpose"
    assert rows["mqtt:zzvoicecfg"] is False, "config is not a house control"
    assert rows["mqtt:zzvoicediag"] is False
    assert rows["mqtt:zzvoiceoff"] is False, "an explicit false beats the rule"
    assert rows["mqtt:zzvoiceac"] is True, "an explicit true beats the rule"

    # Clearing the override hands the entity back to the rule — which is what a
    # brand-new device gets, and the whole point of the change.
    await pool.execute(
        "UPDATE entities SET voice_exposed = NULL WHERE entity_id = 'mqtt:zzvoiceoff'")
    assert await pool.fetchval(
        "SELECT voice_effective FROM entities WHERE entity_id = 'mqtt:zzvoiceoff'") is True

    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'mqtt:zzvoice%'")
    await pool.close()


async def test_entity_detail_gathers_the_whole_device():
    """`GET /entities/{id}/detail` is the backend half of "click a device and see
    all of it": the registry row, live values with their age, the other entities on
    the same physical device, and which rules act on it — in one call."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM automations WHERE name LIKE 'zzdetail%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zzdet:%'")
    await pool.execute("DELETE FROM users WHERE username IN ('detadmin', 'detuser')")
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('detadmin', $1, 'admin')",
                       await hash_password("adminpw12"))
    await pool.execute("INSERT INTO users (username, password_hash, role) VALUES ('detuser', $1, 'user')",
                       await hash_password("userpw12"))
    # one physical device, two entities on it
    for eid, name in (("zzdet:trv", "Valve"), ("zzdet:trv:battery", "Valve battery")):
        await pool.execute(
            "INSERT INTO entities (entity_id, name, adapter, device_type, device_key, capabilities) "
            "VALUES ($1, $2, 'mqtt', 'thermostat', 'zzdet_dev', '[\"target_temperature\"]'::jsonb)", eid, name)
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value, unit, ts_ns) "
        "VALUES ('zzdet:trv', 'target_temperature', '21.5'::jsonb, '°C', 1)")
    await pool.execute(
        "INSERT INTO automations (name, enabled, definition) VALUES ('zzdetail rule', true, $1::jsonb)",
        '{"triggers":[{"entity_id":"zzdet:trv","capability":"target_temperature"}],"actions":[]}')

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "entities-detail-test-secret-0123456789abcd"
    appmod.app.state.bus = StubBus()

    # A stub ClickHouse, deliberately: without it `ch is None` short-circuits both
    # ClickHouse lookups and the non-admin assertion below would pass for the WRONG
    # reason (no store) instead of proving the admin gate. It answers the two
    # queries differently — a single canned row set would let the events assertion
    # pass on command data (or silently swallow an IndexError into an empty list).
    class _Res:
        def __init__(self, rows):
            self.result_rows = rows

    class _StubCH:
        def __init__(self):
            self.seen: list[dict] = []

        async def query(self, sql, parameters=None, **_k):
            self.seen.append(dict(parameters or {}))
            if "device_events" in sql:
                return _Res([[1700000001000, "", "adapter:mqtt", "offline", "error",
                              "broker refused auth", '{"adapter":"mqtt"}']])
            return _Res([[1700000000000, "on_off", "turn_on", "user:marko", "{}"]])

    stub_ch = _StubCH()
    appmod.app.state.ch = stub_ch

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "detadmin", "password": "adminpw12"})).status_code == 200
        r = await c.get("/entities/zzdet:trv/detail")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["entity_id"] == "zzdet:trv" and d["device_key"] == "zzdet_dev"
        assert [s_["capability"] for s_ in d["state"]] == ["target_temperature"], "live values are included"
        assert d["state"][0]["age_s"] is not None, "…with their age, so a stale reading can be greyed out"
        assert [x["entity_id"] for x in d["siblings"]] == ["zzdet:trv:battery"], \
            "the rest of the physical device, and not the entity itself"
        # A device is usually several entities; the panel shows the WHOLE group's
        # values, so each sibling carries its own state (empty list, not absent,
        # when it has never reported).
        assert d["siblings"][0]["state"] == [], "a sibling with no readings carries an empty list"
        assert [a["name"] for a in d["automations"]] == ["zzdetail rule"], "rules that reference it"
        assert d["commands"] and d["commands"][0]["source"] == "user:marko", \
            "an admin sees who commanded it"
        # The journal: WHAT HAPPENED to it, next to what was asked of it.
        assert d["events"] and d["events"][0]["kind"] == "offline", \
            "the device timeline carries journal events, not just commands"
        assert d["events"][0]["severity"] == "error"
        ev_params = next(p for p in stub_ch.seen if "src" in p)
        assert ev_params["ids"] == ["zzdet:trv", "zzdet:trv:battery"], \
            "an event about one entity of a device is an event about the device"
        assert ev_params["src"] == "adapter:mqtt", \
            "…and so is its adapter going offline, which carries no entity of its own"

        r = await c.get("/entities/zzdet:nonexistent/detail")
        assert r.status_code == 404, "an unknown entity is a 404, not an empty shell"

        # Cumulatives ride on the same registry row and the same 404 rule.
        r = await c.get("/entities/zzdet:trv/stats?days=7")
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["entity_id"] == "zzdet:trv" and st["days"] == 7
        # target_temperature is neither a counter nor a runtime capability, so both
        # lists are empty — an absence, not a zero.
        assert st["counters"] == [] and st["runtime"] == []
        assert (await c.get("/entities/zzdet:nonexistent/stats")).status_code == 404

    # a non-admin gets the device but NOT the command audit (source names people)
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c2:
        assert (await c2.post("/auth/login", json={"username": "detuser", "password": "userpw12"})).status_code == 200
        d = (await c2.get("/entities/zzdet:trv/detail")).json()
        assert d["entity_id"] == "zzdet:trv", "a normal user still sees the device"
        assert "commands" not in d, "…but never the audit trail, which names who did what"
        assert d["events"] and d["events"][0]["kind"] == "offline", \
            "events name services and rules, never people — everyone sees why a device went quiet"

    await pool.execute("DELETE FROM automations WHERE name LIKE 'zzdetail%'")
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE 'zzdet:%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zzdet:%'")
    await pool.execute("DELETE FROM users WHERE username IN ('detadmin', 'detuser')")
    await pool.close()
