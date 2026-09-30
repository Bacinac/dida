"""Integration test — the heating routes against a real Postgres: the config
round-trip, what the boundary refuses to store, and who may nudge a room.

Runs in the api image; the runner supplies the ephemeral Postgres (tests/run.sh).
"""
import time

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


BOILER = "zztest:heat:relay"
VALVE = "zztest:heat:valve"
SENSOR = "zztest:heat:sensor"
DEAD_VALVE = "zztest:heat:flatbattery"
ORPHAN_VALVE = "zztest:heat:homeless"


async def _setup(pool):
    await pool.execute("DELETE FROM areas WHERE name LIKE 'zztest_heat%'")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:heat:%'")
    await pool.execute("DELETE FROM users WHERE username IN ('heatadmin', 'heatuser')")
    await pool.execute("DELETE FROM app_settings WHERE key = 'heating'")
    for username, role in (("heatadmin", "admin"), ("heatuser", "user")):
        await pool.execute(
            "INSERT INTO users (username, password_hash, role, can_control) VALUES ($1, $2, $3, true)",
            username, await hash_password("heatpw1234"), role,
        )
    area_id = await pool.fetchval("INSERT INTO areas (name) VALUES ('zztest_heat') RETURNING id")
    # The room as the registry knows it: a thermometer and two heads assigned to it,
    # one of them flat-battery (temperature only, recognised by its settings
    # sub-entity), plus a relay and a head that belong to no room at all.
    for eid, caps, area in (
        (BOILER, ["on_off", "temperature"], None),
        (VALVE, ["target_temperature", "temperature"], area_id),
        (SENSOR, ["temperature", "humidity"], area_id),
        (DEAD_VALVE, ["temperature"], area_id),
        (f"{DEAD_VALVE}:comfort_temperature", ["number"], area_id),
        (ORPHAN_VALVE, ["target_temperature", "temperature"], None),
    ):
        await pool.execute(
            "INSERT INTO entities (entity_id, adapter, capabilities, area_id) "
            "VALUES ($1, 'zztest', $2::jsonb, $3)",
            eid, caps, area,
        )
    return area_id


async def _login(c, username):
    r = await c.post("/auth/login", json={"username": username, "password": "heatpw1234"})
    assert r.status_code == 200, f"{username} login"


async def test_heating_config_round_trip():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    area_id = await _setup(pool)

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "heating-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        await _login(c, "heatadmin")

        # A house that was never configured reads as inert, not as broken. The room
        # is already listed — it holds a valve — with what it contains derived, and
        # the orphan valve named rather than silently missing.
        r = await c.get("/heating")
        assert r.status_code == 200
        body = r.json()
        assert body["settings"]["enabled"] is False
        here = next(x for x in body["rooms"] if x["area_id"] == area_id)
        assert here["valves"] == [DEAD_VALVE, VALVE], "both heads in the room, flat battery included"
        assert here["sensors"] == [SENSOR], "the relay's internal temperature is not the room's"
        assert here["config"]["enabled"] is True, "a room with a radiator is heated by default"
        assert ORPHAN_VALVE in body["orphan_valves"]

        # Enabling without a relay is refused: it would drive nothing, silently.
        r = await c.put("/heating/settings", json={"enabled": True, "boiler": ""})
        assert r.status_code == 400, "enabled with no boiler"

        # …and a relay that doesn't exist is refused too.
        r = await c.put("/heating/settings", json={"enabled": True, "boiler": "zztest:heat:nope"})
        assert r.status_code == 400, "unknown boiler entity"

        # A sensor is not a relay.
        r = await c.put("/heating/settings", json={"enabled": True, "boiler": SENSOR})
        assert r.status_code == 400, "boiler entity must expose on_off"

        # The house's own programme lives here, once, for every room.
        r = await c.put("/heating/settings", json={
            "enabled": True, "boiler": BOILER, "min_on_s": 300, "min_calling": 2,
            "targets": {"comfort": 21.5, "night": 18},
            "schedule": [{"days": [0, 1, 2, 3, 4], "at": "06:30", "profile": "comfort"}],
        })
        assert r.status_code == 200
        assert r.json()["boiler"] == BOILER
        assert r.json()["targets"]["comfort"] == 21.5
        assert r.json()["schedule"][0]["at"] == "06:30", "the house schedule round-trips"

        r = await c.put("/heating/settings", json={"enabled": True, "boiler": BOILER, "min_calling": 0})
        assert r.status_code == 400, "a boiler that starts on zero rooms is not a threshold"
        r = await c.put("/heating/settings", json={"enabled": True, "boiler": BOILER,
                                                   "targets": {"comfort": 99}})
        assert r.status_code == 400, "the house setpoints obey the same range as a room's"
        assert (await c.get("/heating")).json()["settings"]["enabled"] is True

        # --- room config
        good = {
            "enabled": True, "sensor": SENSOR, "valves": [VALVE], "offset": -1.5,
            "schedule": [{"days": [0, 1, 2, 3, 4], "at": "06:30", "profile": "comfort"}],
            "window_pause": True, "can_call_boiler": False,
        }
        r = await c.put(f"/heating/rooms/{area_id}", json=good)
        assert r.status_code == 200
        stored = r.json()["config"]
        assert stored["offset"] == -1.5
        assert stored["can_call_boiler"] is False
        assert stored["schedule"][0]["at"] == "06:30"
        assert stored["override_target"] is None, "stored config is the full, normalised shape"

        r = await c.get("/heating")
        here = next(x for x in r.json()["rooms"] if x["area_id"] == area_id)
        assert here["config"]["sensor"] == SENSOR, "an explicit override is stored and returned"

        # A TRV whose battery died reports only a temperature — it must still be
        # selectable, or the house can't be configured before the season starts.
        r = await c.put(f"/heating/rooms/{area_id}", json={**good, "valves": [VALVE, DEAD_VALVE]})
        assert r.status_code == 200, "a silent TRV is still a valve"

        # …but a relay is not a valve, and a typo is not an entity.
        r = await c.put(f"/heating/rooms/{area_id}", json={**good, "valves": [BOILER]})
        assert r.status_code == 400, "the boiler relay is not a thermostatic valve"
        r = await c.put(f"/heating/rooms/{area_id}", json={**good, "valves": ["zztest:heat:typo"]})
        assert r.status_code == 400, "unknown valve"
        r = await c.put(f"/heating/rooms/{area_id}", json={**good, "sensor": "zztest:heat:typo"})
        assert r.status_code == 400, "unknown sensor"
        r = await c.put(f"/heating/rooms/{area_id}", json={**good, "offset": 99})
        assert r.status_code == 400, "a room is a trim on the house, not its own house"
        r = await c.put(f"/heating/rooms/{area_id}", json={**good,
                        "schedule": [{"days": [0], "at": "6:30", "profile": "comfort"}]})
        assert r.status_code == 400, "malformed schedule time"

        r = await c.put(f"/heating/rooms/{area_id}", json=good)
        assert r.status_code == 200, "back to a valid config"

        # --- boost: a temporary hold that expires on its own
        r = await c.post(f"/heating/rooms/{area_id}/boost", json={"target": 23, "minutes": 60})
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["override_target"] == 23
        assert cfg["override_until"] > time.time(), "the hold has an end"

        r = await c.post(f"/heating/rooms/{area_id}/boost", json={"target": 23, "minutes": 0})
        assert r.status_code == 422, "a zero-length hold is not a hold"

        r = await c.delete(f"/heating/rooms/{area_id}/boost")
        assert r.status_code == 200
        assert r.json()["config"]["override_target"] is None

        r = await c.post("/heating/rooms/999999/boost", json={"target": 23, "minutes": 60})
        assert r.status_code == 404, "a room with no heating config"

    # --- a non-admin may boost a room but may not reconfigure the house
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        await _login(c, "heatuser")
        assert (await c.get("/heating")).status_code == 200
        r = await c.post(f"/heating/rooms/{area_id}/boost", json={"target": 22, "minutes": 30})
        assert r.status_code == 200, "a household member may nudge a room"
        r = await c.put("/heating/settings", json={"enabled": False})
        assert r.status_code == 403, "…but not rewire the house"
        r = await c.put(f"/heating/rooms/{area_id}", json={"valves": []})
        assert r.status_code == 403
        assert (await c.delete(f"/heating/rooms/{area_id}")).status_code == 403

    # a view-only member cannot even nudge
    await pool.execute("UPDATE users SET can_control = false WHERE username = 'heatuser'")
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        await _login(c, "heatuser")
        r = await c.post(f"/heating/rooms/{area_id}/boost", json={"target": 22, "minutes": 30})
        assert r.status_code == 403, "view-only members do not drive the heating"

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        await _login(c, "heatadmin")
        # Clearing the config resets it — it does NOT unheat the room, which is
        # heated because it holds a radiator valve, not because of a saved row.
        assert (await c.delete(f"/heating/rooms/{area_id}")).status_code == 204
        here = next(x for x in (await c.get("/heating")).json()["rooms"] if x["area_id"] == area_id)
        assert here["config"]["sensor"] == "" and here["config"]["offset"] == 0
        assert here["valves"] == [DEAD_VALVE, VALVE]
        assert (await c.delete(f"/heating/rooms/{area_id}")).status_code == 404, "nothing left to clear"

    await pool.execute("UPDATE entities SET area_id = NULL WHERE entity_id LIKE 'zztest:heat:%'")
    await pool.execute("DELETE FROM areas WHERE id = $1", area_id)
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'zztest:heat:%'")
    await pool.execute("DELETE FROM app_settings WHERE key = 'heating'")
    await pool.close()
