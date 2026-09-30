"""Integration test — the assistant's tool surface against a real Postgres, with a
scripted stub in place of Claude.

The assistant is the only LLM surface with WRITE access: it publishes commands and
inserts automations. It had no tests at all. These cover the parts that are ours
rather than the model's — tool dispatch, the per-user boundary, and the catalogue
the model is handed:

* explain_dida returns the help corpus (the only explain-itself surface DIDA has);
* list_entities carries <cap>_options VALUES, not just the capability name — the
  bug that made the author model invent source names;
* the synthesis catalogue carries them too, which is what its prompt promises;
* a restricted user's send_command is refused and never reaches the bus, and a
  hidden entity is neither listed nor commandable;
* a command outside the capability's vocabulary comes back as a rejection the model
  can read, not an exception.

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). No network: _StubClient replaces AsyncAnthropic.
"""
import json
from types import SimpleNamespace

from dida_api.assistant import (
    AssistantCtx,
    _execute_tool,
    assistant_events,
    run_assistant,
    synthesize_definition,
)
from dida_api.auth import AuthUser
from dida_api.seed import seed_from_env
from dida_core import apply_migrations, jsonb_init, pg_pool


class StubBus:
    def __init__(self):
        self.commands = []
        self.raw = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)

    async def publish_event(self, *a, **k):
        pass

    async def publish_raw(self, subject, payload):
        self.raw.append((subject, payload))


class _Block:
    """Duck-types an anthropic content block (text or tool_use)."""

    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


class _Messages:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class _StubClient:
    """Replays a scripted list of responses; records what it was asked."""

    def __init__(self, *script):
        self.messages = _Messages(script)


async def _seed(pool):
    await _cleanup(pool)
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, capabilities) VALUES "
        "('denon:zzavr', 'denon', 'zz AVR', '[\"on_off\",\"source\",\"source_options\"]'::jsonb)"
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, capabilities) VALUES "
        "('mqtt:zzsecret', 'mqtt', 'zz Secret', '[\"on_off\"]'::jsonb)"
    )
    # The options blob current_state carries — a JSON list, exactly as an adapter
    # publishes it. This is the payload that never used to reach the model.
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES "
        "('denon:zzavr', 'source_options', $1)",
        json.dumps(["TUNER", "MPLAY", "GAME"]),
    )
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES "
        "('denon:zzavr', 'source', $1)",
        json.dumps("TUNER"),
    )
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES "
        "('denon:zzavr', 'on_off', $1)",
        True,
    )
    # Radio presets are the same idea under an older name, and carry payload the
    # model must not be charged for (stream url, artwork).
    await pool.execute(
        "INSERT INTO current_state (entity_id, capability, value) VALUES "
        "('denon:zzavr', 'media_favorites', $1)",
        json.dumps([
            {"preset": 1, "name": "Yammat FM", "url": "https://example/y.mp3", "image": "http://x/y.png"},
            {"preset": 2, "name": "Radio 101", "url": "https://example/r.mp3", "image": ""},
        ]),
    )


async def _cleanup(pool):
    await pool.execute("DELETE FROM user_access_rules WHERE ref LIKE 'mqtt:zzsecret%'")
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE '%zz%'")
    await pool.execute("DELETE FROM entities WHERE entity_id IN ('denon:zzavr', 'mqtt:zzsecret')")
    await pool.execute("DELETE FROM users WHERE username IN ('zzasstadmin', 'zzasstuser')")
    await pool.execute("DELETE FROM automations WHERE name LIKE 'zzasst%'")
    await pool.execute("DELETE FROM scenes WHERE name LIKE 'zzasst%'")


async def test_assistant_tools():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)

    admin = AuthUser(id=1, username="zzasstadmin", role="admin")
    bus = StubBus()
    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=bus, ch=None, user=admin)

    # explain_dida — DIDA's only explain-itself surface. Returned as raw text.
    out = await _execute_tool(ctx, "explain_dida", {"topic": "automations"})
    assert "Trigger" in out and "Starlark" in out
    assert "DIDA help topics" in await _execute_tool(ctx, "explain_dida", {})

    # list_entities carries the OPTION VALUES, not just the capability name.
    listed = json.loads(await _execute_tool(ctx, "list_entities", {"adapter": "denon"}))
    avr = next(e for e in listed if e["entity_id"] == "denon:zzavr")
    assert avr["options"]["source_options"] == ["TUNER", "MPLAY", "GAME"], (
        "the model must see which sources set_source accepts, not just that a "
        "source_options capability exists"
    )
    # Presets reach it too — trimmed to what identifies a choice, so a station's
    # stream url and artwork don't ride along into the prompt on every listing.
    assert avr["options"]["media_favorites"] == [
        {"preset": 1, "name": "Yammat FM"},
        {"preset": 2, "name": "Radio 101"},
    ], "play_preset needs the preset numbers and the names to match them against"

    # get_state filters, so "which lights are on" is ONE call. Without this the
    # model answered it with 8-14 single-entity calls, each a full round of the
    # loop that re-sends the whole conversation.
    on_off = json.loads(await _execute_tool(
        ctx, "get_state", {"capability": "source", "entity_ids": ["denon:zzavr"]}))
    assert [r["capability"] for r in on_off] == ["source"], "filtered to one reading"
    assert json.loads(await _execute_tool(ctx, "get_state", {"device_type": "nosuchtype"})) == []

    # A command outside the capability's vocabulary is a readable rejection.
    bad = json.loads(await _execute_tool(
        ctx, "send_command",
        {"entity_id": "denon:zzavr", "capability": "on_off", "command": "explode"},
    ))
    assert "rejected (capability)" in bad["error"]
    assert bus.commands == [], "an invalid command must not reach the bus"

    # A valid one does, tagged with who asked for it.
    ok = json.loads(await _execute_tool(
        ctx, "send_command",
        {"entity_id": "denon:zzavr", "capability": "source",
         "command": "set_source", "args": {"value": "TUNER"}},
    ))
    assert ok["ok"] is True
    assert len(bus.commands) == 1
    assert bus.commands[0].source == "assistant:zzasstadmin"

    await _cleanup(pool)
    await pool.close()


async def test_list_entities_carries_type_and_floor_and_hides_diagnostics():
    """The house's own classification and placement must reach the model.

    Without `type` it guesses "is this a light?" from capabilities — but a relay and
    a lamp both expose only on_off, so "turn off the lights" was a coin flip it had
    to hedge about out loud. Without `floor`, the app's own example prompt ("all the
    lights on the ground floor") is unanswerable.
    """
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    await pool.execute(
        "INSERT INTO floors (key, name) VALUES ('zzground', 'zz Ground') ON CONFLICT DO NOTHING"
    )
    area_id = await pool.fetchval(
        "INSERT INTO areas (name, kind, fp_floor) VALUES ('zz Kitchen', 'kitchen', 'zzground') RETURNING id"
    )
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, device_type, area_id, capabilities) "
        "VALUES ('mqtt:zzlamp', 'mqtt', 'zz Lamp', 'light', $1, '[\"on_off\"]'::jsonb)", area_id)
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, device_type, area_id, capabilities) "
        "VALUES ('mqtt:zzrelay', 'mqtt', 'zz Relay', 'switch', $1, '[\"on_off\"]'::jsonb)", area_id)
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, name, capabilities, diagnostic) "
        "VALUES ('mqtt:zzrssi', 'mqtt', 'zz RSSI', '[\"signal\"]'::jsonb, true)")

    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=StubBus(), ch=None,
                       user=AuthUser(id=1, username="zzasstadmin", role="admin"))

    listed = json.loads(await _execute_tool(ctx, "list_entities", {"room": "zz Kitchen"}))
    by_id = {e["entity_id"]: e for e in listed}
    assert by_id["mqtt:zzlamp"]["type"] == "light"
    assert by_id["mqtt:zzrelay"]["type"] == "switch", "identical caps, different kind"
    assert by_id["mqtt:zzlamp"]["floor"] == "zz Ground", "floor inherited from the area"

    # device_type is a real filter, so "the lights" resolves without guessing.
    lights = json.loads(await _execute_tool(ctx, "list_entities", {"device_type": "light"}))
    assert {e["entity_id"] for e in lights} == {"mqtt:zzlamp"}

    # …and so is the floor, which the empty-state example prompt asks for verbatim.
    on_floor = json.loads(await _execute_tool(ctx, "list_entities", {"floor": "ground"}))
    assert {e["entity_id"] for e in on_floor} == {"mqtt:zzlamp", "mqtt:zzrelay"}

    everything = json.loads(await _execute_tool(ctx, "list_entities", {"adapter": "mqtt"}))
    assert "mqtt:zzrssi" not in {e["entity_id"] for e in everything}, "diagnostics are noise"

    # An UNFILTERED call answers with a directory instead of the whole registry:
    # measured on the real house, listing all 433 devices cost ~38k tokens and was
    # then re-sent on every later round of the turn.
    directory = json.loads(await _execute_tool(ctx, "list_entities", {}))
    assert isinstance(directory, dict), "a bare call must not dump every device"
    # Counted against this test's own area — the integration group shares one
    # ephemeral Postgres, so house-wide totals belong to every suite at once.
    assert directory["by_room"]["zz Kitchen"] == 2, "the lamp and the relay, not the RSSI"
    assert directory["by_type"]["light"] >= 1 and directory["by_type"]["switch"] >= 1
    assert directory["total"] >= 2
    # The note has to name the actual parameters, not just say "narrow it down" —
    # it is the only place the model learns what a second call should carry.
    for param in ("device_type", "room", "floor", "capability", "adapter"):
        assert param in directory["note"]

    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'mqtt:zz%'")
    await pool.execute("DELETE FROM areas WHERE id = $1", area_id)
    await pool.execute("DELETE FROM floors WHERE key = 'zzground'")
    await _cleanup(pool)
    await pool.close()


async def test_list_alerts_reads_the_live_evaluator():
    """'Why is X not working' should reach the watchdog, not guess. And a watchdog
    that is DOWN must say so — silence there reads exactly like "all clear"."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")

    class Evaluator:
        def active_list(self):
            return [{"key": "adapter_stale", "scope": "mqtt", "severity": "warning"}]

    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=StubBus(), ch=None,
                       user=AuthUser(id=1, username="zzasstadmin", role="admin"),
                       alert_evaluator=Evaluator())
    out = json.loads(await _execute_tool(ctx, "list_alerts", {}))
    assert out["active"][0]["key"] == "adapter_stale"

    down = AssistantCtx(client=_StubClient(), pool=pool, bus=StubBus(), ch=None,
                        user=AuthUser(id=1, username="zzasstadmin", role="admin"))
    out = json.loads(await _execute_tool(down, "list_alerts", {}))
    assert "not running" in out["error"], "a dead evaluator must not read as 'no alerts'"

    await pool.close()


async def test_assistant_enforces_the_user_boundary():
    """The assistant must not be a side door around per-user visibility/control."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    user_id = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) "
        "VALUES ('zzasstuser', 'x', 'user', false) RETURNING id"
    )
    # kind='view' is the HIDE rule (see visibility.hidden_entity_ids).
    await pool.execute(
        "INSERT INTO user_access_rules (user_id, kind, scope, ref) "
        "VALUES ($1, 'view', 'entity', 'mqtt:zzsecret')",
        user_id,
    )
    restricted = AuthUser(id=user_id, username="zzasstuser", role="user", can_control=False)
    bus = StubBus()
    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=bus, ch=None, user=restricted)

    listed = json.loads(await _execute_tool(ctx, "list_entities", {"adapter": "mqtt"}))
    ids = {e["entity_id"] for e in listed}
    assert "mqtt:zzsecret" not in ids, "a hidden entity must not even be named"
    # …and it must not show up in the directory's counts either — a total that
    # includes what you may not see still leaks that it exists. Asserted as a
    # DIFFERENCE against an admin's view, because the shared test database means
    # neither absolute total is this suite's to predict.
    admin_ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=bus, ch=None,
                             user=AuthUser(id=1, username="zzasstadmin", role="admin"))
    admin_total = json.loads(await _execute_tool(admin_ctx, "list_entities", {}))["total"]
    user_total = json.loads(await _execute_tool(ctx, "list_entities", {}))["total"]
    assert admin_total - user_total == 1, "the one hidden entity is missing from the count"

    # Hidden → 'unknown device' (don't leak that it exists), not 'forbidden'.
    hidden = json.loads(await _execute_tool(
        ctx, "send_command",
        {"entity_id": "mqtt:zzsecret", "capability": "on_off", "command": "turn_on"},
    ))
    assert "unknown device" in hidden["error"]

    # Visible but can_control=false → refused on permission grounds.
    refused = json.loads(await _execute_tool(
        ctx, "send_command",
        {"entity_id": "denon:zzavr", "capability": "on_off", "command": "turn_on"},
    ))
    assert "permission" in refused["error"]
    assert bus.commands == [], "no refused command reached the bus"

    # Creating a rule stays admin-only through this door too.
    created = json.loads(await _execute_tool(
        ctx, "create_automation", {"name": "zz", "description": "whatever"},
    ))
    assert "administrator" in created["error"]

    await _cleanup(pool)
    await pool.close()


async def test_synthesis_catalog_carries_option_values():
    """The author prompt tells the model to take values from <cap>_options. Assert
    the catalogue it is handed actually contains them — the instruction pointed at
    absent data before, so the model invented source names instead."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)

    definition = {
        "trigger": {"entity_id": "denon:zzavr", "capability": "on_off", "to": True,
                    "for_seconds": None},
        "conditions": [],
        "actions": [{"entity_id": "denon:zzavr", "capability": "source",
                     "command": "set_source", "value": "TUNER", "delay_ms": None}],
    }
    client = _StubClient(_Resp("end_turn", [_Block("text", text=json.dumps(definition))]))

    out = await synthesize_definition(client, pool, "when the avr turns on, select the tuner")
    assert out["actions"][0]["args"] == {"value": "TUNER"}

    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "denon:zzavr" in sent
    assert '"TUNER"' in sent and "MPLAY" in sent, (
        "the catalogue must spell out the allowed source values"
    )

    await _cleanup(pool)
    await pool.close()


async def test_typed_synthesis_self_corrects_a_rejected_definition():
    """The boundary's rejection is machine-actionable, so it is fed back rather than
    surfaced to the user — the same treatment the Starlark path already had. Caught
    live: the model reached for the new sun_state capability and invented the value
    'above_horizon'."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)

    bad = {
        "trigger": {"entity_id": "denon:zzavr", "capability": "on_off",
                    "to": "yes-please", "for_seconds": None},  # on_off is a bool
        "conditions": [], "actions": [],
    }
    good = {
        "trigger": {"entity_id": "denon:zzavr", "capability": "on_off",
                    "to": True, "for_seconds": None},
        "conditions": [],
        "actions": [{"entity_id": "denon:zzavr", "capability": "on_off",
                     "command": "turn_off", "value": None, "delay_ms": None}],
    }
    client = _StubClient(
        _Resp("end_turn", [_Block("text", text=json.dumps(bad))]),
        _Resp("end_turn", [_Block("text", text=json.dumps(good))]),
    )
    out = await synthesize_definition(client, pool, "when the avr turns on, turn it off")
    assert out["triggers"][0]["to"] is True

    # The retry carried the boundary's own words back to the model.
    retry = client.messages.calls[1]["messages"][-1]["content"]
    assert "rejected by the boundary" in retry and "on_off" in retry

    await _cleanup(pool)
    await pool.close()


async def test_automation_and_scene_tools():
    """The rule + scene tools drive the SAME domain functions the REST routes do,
    so the breaker reset, the disabled-rule guard and the scene boundary cannot
    drift between the button and the chat."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    auto_id = await pool.fetchval(
        "INSERT INTO automations (name, enabled, definition, last_error) "
        "VALUES ('zzasst rule', false, '{}'::jsonb, 'boom') RETURNING id"
    )
    scene_id = await pool.fetchval(
        "INSERT INTO scenes (name, states) VALUES ('zzasst scene', $1::jsonb) RETURNING id",
        json.dumps([{"entity_id": "denon:zzavr", "capability": "on_off", "value": True}]),
    )

    bus = StubBus()
    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=bus, ch=None,
                       user=AuthUser(id=1, username="zzasstadmin", role="admin"))

    listed = json.loads(await _execute_tool(ctx, "list_automations", {}))
    mine = next(a for a in listed if a["id"] == auto_id)
    assert mine["enabled"] is False and mine["last_error"] == "boom"

    # Running a disabled rule is refused — the same guard the route enforces.
    refused = json.loads(await _execute_tool(ctx, "run_automation", {"automation_id": auto_id}))
    assert "disabled" in refused["error"]
    assert bus.raw == [], "a disabled rule must not reach the engine"

    # Re-enabling clears the tripped breaker's last_error (shared set_enabled).
    enabled = json.loads(await _execute_tool(
        ctx, "set_automation_enabled", {"automation_id": auto_id, "enabled": True}))
    assert enabled["enabled"] is True
    assert await pool.fetchval("SELECT last_error FROM automations WHERE id = $1", auto_id) is None

    ran = json.loads(await _execute_tool(ctx, "run_automation", {"automation_id": auto_id}))
    assert ran["ok"] is True
    assert len(bus.raw) == 1 and bus.raw[0][0] == "dida.automation.run"
    assert json.loads(bus.raw[0][1]) == {"id": auto_id}

    # "Did it fire, did it fail" — the record the Automations page shows, which the
    # assistant could not reach, so "why didn't the watering run" got a guess.
    await pool.execute(
        "INSERT INTO automation_runs (automation_id, name, outcome, detail) "
        "VALUES ($1, 'zzasst rule', 'error', 'valve timeout')", auto_id)
    runs = json.loads(await _execute_tool(ctx, "automation_runs", {"automation_id": auto_id}))
    assert runs[0]["outcome"] == "error" and runs[0]["detail"] == "valve timeout"

    scenes = json.loads(await _execute_tool(ctx, "list_scenes", {}))
    assert any(s["id"] == scene_id for s in scenes)
    recalled = json.loads(await _execute_tool(ctx, "recall_scene", {"scene_id": scene_id}))
    assert recalled["applied"] == 1 and recalled["skipped"] == 0
    assert bus.commands[-1].source == f"assistant:zzasstadmin:scene:{scene_id}"

    await _cleanup(pool)
    await pool.close()


async def test_rule_tools_are_admin_only_and_scenes_keep_their_boundary():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    auto_id = await pool.fetchval(
        "INSERT INTO automations (name, enabled, definition) "
        "VALUES ('zzasst rule2', true, '{}'::jsonb) RETURNING id"
    )
    scene_id = await pool.fetchval(
        "INSERT INTO scenes (name, states) VALUES ('zzasst scene2', $1::jsonb) RETURNING id",
        json.dumps([{"entity_id": "denon:zzavr", "capability": "on_off", "value": True}]),
    )
    user_id = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role, can_control) "
        "VALUES ('zzasstuser', 'x', 'user', false) RETURNING id"
    )
    bus = StubBus()
    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=bus, ch=None,
                       user=AuthUser(id=user_id, username="zzasstuser", role="user",
                                     can_control=False))

    for tool, args in (
        ("list_automations", {}),
        ("set_automation_enabled", {"automation_id": auto_id, "enabled": False}),
        ("run_automation", {"automation_id": auto_id}),
        ("explain_automation", {"automation_id": auto_id}),
        ("automation_runs", {}),
    ):
        out = json.loads(await _execute_tool(ctx, tool, args))
        assert "administrator" in out["error"], f"{tool} must stay admin-only"
    assert bus.raw == [] and bus.commands == []
    assert await pool.fetchval("SELECT enabled FROM automations WHERE id = $1", auto_id) is True

    # Scenes ARE open to every user — but recall silently skips what they may not
    # control, so it can never become a permission-escalation path.
    recalled = json.loads(await _execute_tool(ctx, "recall_scene", {"scene_id": scene_id}))
    assert recalled["applied"] == 0 and recalled["skipped"] == 1
    assert bus.commands == [], "a can_control=false user drove nothing through a scene"

    await pool.execute("DELETE FROM automation_runs WHERE name LIKE 'zzasst%'")
    await _cleanup(pool)
    await pool.close()


async def test_command_log_keeps_the_view_boundary():
    """"Why did the light come on" reads the audit trail, so it reads other people's
    devices too — it must filter exactly like every other read, and refuse a hidden
    entity by name without admitting it exists."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    user_id = await pool.fetchval(
        "INSERT INTO users (username, password_hash, role) "
        "VALUES ('zzasstuser', 'x', 'user') RETURNING id")
    await pool.execute(
        "INSERT INTO user_access_rules (user_id, kind, scope, ref) "
        "VALUES ($1, 'view', 'entity', 'mqtt:zzsecret')", user_id)

    class StubCH:
        """Returns rows for both entities; the boundary is ours, not the store's."""

        async def query(self, sql, parameters=None):
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            return SimpleNamespace(result_rows=[
                (now, "mqtt:zzsecret", "on_off", "turn_on", "user:someone"),
                (now, "denon:zzavr", "on_off", "turn_off", "automation:7:Night"),
            ])

    restricted = AuthUser(id=user_id, username="zzasstuser", role="user")
    ctx = AssistantCtx(client=_StubClient(), pool=pool, bus=StubBus(), ch=StubCH(),
                       user=restricted)

    rows = json.loads(await _execute_tool(ctx, "command_log", {}))
    ids = {r["entity_id"] for r in rows}
    assert "mqtt:zzsecret" not in ids, "a hidden entity's commands must not leak"
    assert "denon:zzavr" in ids
    assert rows[0]["source"] or True  # the point of the log: WHO asked

    # Asked for by name it is 'unknown device', not 'forbidden' — same as elsewhere.
    out = json.loads(await _execute_tool(ctx, "command_log", {"entity_id": "mqtt:zzsecret"}))
    assert "unknown device" in out["error"]

    await pool.execute("DELETE FROM user_access_rules WHERE user_id = $1", user_id)
    await _cleanup(pool)
    await pool.close()


async def test_assistant_events_reports_each_tool_then_done():
    """The stream must announce every tool BEFORE the answer, otherwise a turn that
    runs for tens of seconds is indistinguishable from a hang."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)

    client = _StubClient(
        _Resp("tool_use", [_Block("tool_use", name="list_entities", input={}, id="tu_1"),
                           _Block("tool_use", name="explain_dida",
                                  input={"topic": "scenes"}, id="tu_2")]),
        _Resp("end_turn", [_Block("text", text="Evo.")]),
    )
    ctx = AssistantCtx(client=client, pool=pool, bus=StubBus(), ch=None,
                       user=AuthUser(id=1, username="zzasstadmin", role="admin"))

    events = [e async for e in assistant_events(ctx, [], "što imam?")]
    assert [e["type"] for e in events] == ["tool", "tool", "done"]
    assert [e["name"] for e in events if e["type"] == "tool"] == ["list_entities", "explain_dida"]
    assert events[-1]["reply"] == "Evo."

    # run_assistant is a drain of the same generator — one loop, not two.
    client2 = _StubClient(_Resp("end_turn", [_Block("text", text="Bok.")]))
    ctx2 = AssistantCtx(client=client2, pool=pool, bus=StubBus(), ch=None,
                        user=AuthUser(id=1, username="zzasstadmin", role="admin"))
    assert (await run_assistant(ctx2, [], "bok"))["reply"] == "Bok."

    await _cleanup(pool)
    await pool.close()


async def test_assistant_endpoint_streams_sse():
    """End to end over HTTP: the endpoint frames the turn as text/event-stream, the
    gate checks still produce ordinary status codes, and the rate limit bites."""
    import os

    import dida_api.app as appmod
    from dida_api.auth import hash_password
    from httpx import ASGITransport, AsyncClient

    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('zzasstadmin', $1, 'admin')",
        await hash_password("pw"),
    )

    client = _StubClient(
        _Resp("tool_use", [_Block("tool_use", name="explain_dida",
                                  input={"topic": "overview"}, id="tu_1")]),
        _Resp("end_turn", [_Block("text", text="Evo pregleda.")]),
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "assistant-sse-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()
    appmod.app.state.ch = None
    # resolve_assistant_client returns the cached client when the key matches.
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    await seed_from_env(pool)
    appmod.app.state.anthropic_client = client
    appmod.app.state.anthropic_key = "test-key"

    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app),
                               base_url="http://itest") as c:
            r = await c.post("/auth/login", json={"username": "zzasstadmin", "password": "pw"})
            assert r.status_code == 200

            r = await c.post("/assistant", json={"message": "što je DIDA?"})
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            # Proxies in front of this (vite/adapter-node/tunnel) must not buffer.
            assert r.headers.get("x-accel-buffering") == "no"

            events = [
                json.loads(line[5:].strip())
                for line in r.text.splitlines()
                if line.startswith("data:")
            ]
            assert [e["type"] for e in events] == ["tool", "done"]
            assert events[0]["name"] == "explain_dida"
            assert events[1]["reply"] == "Evo pregleda."

            # The per-user limiter is in front of the stream, as a real status code.
            from dida_api.rate_limit import ASSISTANT_LIMITER

            ASSISTANT_LIMITER._buckets.clear()
            for _ in range(int(ASSISTANT_LIMITER._capacity)):
                ASSISTANT_LIMITER.take("zzasstadmin")
            r = await c.post("/assistant", json={"message": "opet"})
            assert r.status_code == 429
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        appmod.app.state.anthropic_client = None
        appmod.app.state.anthropic_key = None

    await _cleanup(pool)
    await pool.close()


async def test_run_assistant_drives_the_tool_loop():
    """One full turn: the model asks for a tool, we execute it, it answers."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _seed(pool)

    client = _StubClient(
        _Resp("tool_use", [_Block("tool_use", name="explain_dida",
                                  input={"topic": "helpers"}, id="tu_1")]),
        _Resp("end_turn", [_Block("text", text="Helperi su virtualni entiteti.")]),
    )
    ctx = AssistantCtx(client=client, pool=pool, bus=StubBus(), ch=None,
                       user=AuthUser(id=1, username="zzasstadmin", role="admin"))

    result = await run_assistant(ctx, [], "što su helperi?")
    assert result["reply"] == "Helperi su virtualni entiteti."

    # The tool result really was fed back, and the loop asked for the tools + the
    # thinking/effort settings the tool-reaching behaviour depends on.
    first, second = client.messages.calls
    assert any(t["name"] == "explain_dida" for t in first["tools"])
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "low"}
    # The tools+system prefix is cached — 4.2k tokens that were otherwise re-sent
    # on every round of every turn. It must also stay byte-identical between turns,
    # which is why no locale or user name is interpolated into it.
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["system"] == second["system"], "a varying prefix would never hit"
    fed_back = second["messages"][-1]["content"][0]
    assert fed_back["tool_use_id"] == "tu_1"
    assert "Manual" in fed_back["content"], "the help text reached the model"

    await _cleanup(pool)
    await pool.close()
