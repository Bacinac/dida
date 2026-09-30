"""Integration test — the engine's DEVICE-LIFECYCLE entry points.

`test_engine_projection.py` covers the hot path (validate → project → persist).
This covers the five entry points that had no test at all, which between them
decide whether a device's updates are accepted, what it IS, and whether a removed
one can come back:

    load_removed        the tombstone set, rebuilt from the DB at boot
    on_forget           user deleted a device → block its key AND delete its rows
    on_restore          user un-deleted it   → unblock, or it stays deaf forever
    on_entity_info      the announce path — metadata with no state
    seed_missing_types  give every type-less entity a concrete canonical type

The restore path is the one worth having: `_removed` is an in-memory set, so a bug
there produces a device that the UI shows as restored while the engine silently
drops everything it says — which looks exactly like a broken adapter and would be
debugged for hours in the wrong place.

Runs against the throwaway Postgres the runner spins up for the engine suite.
"""
import msgspec
from dida_core import EntityInfo, StateUpdate, apply_migrations, jsonb_init, pg_pool
from dida_engine.__main__ import Engine

PREFIX = "zzlife:"


class StubBus:
    """Records journal events so the lifecycle's LOUDNESS is asserted too — a
    removal that leaves no trace is how a device silently disappears."""

    def __init__(self):
        self.journal = []

    encode_event = staticmethod(lambda ev: msgspec.msgpack.encode(ev))

    async def publish_journal(self, event):
        self.journal.append(event)

    async def publish_raw(self, subject, payload):
        pass

    async def flush(self, timeout=5.0):
        pass


class StubHistory:
    def enqueue(self, *a):
        pass


async def _fresh():
    pool = await pg_pool(min_size=1, max_size=2, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _clean(pool)
    bus = StubBus()
    return pool, bus, Engine(bus, pool, StubHistory())


async def _clean(pool):
    await pool.execute("DELETE FROM current_state WHERE entity_id LIKE $1", PREFIX + "%")
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE $1", PREFIX + "%")
    await pool.execute("DELETE FROM devices WHERE device_key LIKE $1", PREFIX + "%")
    await pool.execute("DELETE FROM removed_devices WHERE key LIKE $1", PREFIX + "%")


def _state(entity_id, cap="on_off", value=True, ts=1000, device=None):
    return StateUpdate(entity_id=entity_id, capability=cap, value=value,
                       adapter="zzlife", ts_ns=ts, device=device)


# --- load_removed -------------------------------------------------------------

async def test_load_removed_rebuilds_the_tombstone_set_from_the_database():
    """The set is in MEMORY; the DB is the truth. A restart must not resurrect
    every device the user ever deleted."""
    pool, _bus, eng = await _fresh()
    await pool.execute("INSERT INTO removed_devices (key) VALUES ($1), ($2)",
                       PREFIX + "gone_a", PREFIX + "gone_b")

    assert eng._removed == set(), "starts empty before load"
    await eng.load_removed()
    assert eng._removed == {PREFIX + "gone_a", PREFIX + "gone_b"}

    # …and a fresh Engine over the same DB sees the same set — this is what makes
    # the periodic reload safe rather than a source of drift.
    eng2 = Engine(_bus, pool, StubHistory())
    await eng2.load_removed()
    assert eng2._removed == eng._removed

    await _clean(pool)
    await pool.close()


# --- on_forget ----------------------------------------------------------------

async def test_forget_blocks_the_key_and_deletes_the_rows():
    pool, bus, eng = await _fresh()
    await eng.on_state(_state(PREFIX + "lamp"))
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id=$1", PREFIX + "lamp") == 1

    await eng.on_forget(PREFIX + "lamp")

    assert PREFIX + "lamp" in eng._removed, "the key is blocked in memory"
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id=$1", PREFIX + "lamp") == 0, \
        "and its rows are gone — a state message already past the check can otherwise re-insert an orphan"
    assert [e.kind for e in bus.journal] == ["removed"], "the removal is journalled, not silent"

    await _clean(pool)
    await pool.close()


async def test_a_forgotten_device_cannot_re_insert_itself():
    """THE point of the tombstone: the adapter has not been told anything and will
    keep publishing. Every one of those must be dropped."""
    pool, _bus, eng = await _fresh()
    await eng.on_state(_state(PREFIX + "lamp"))
    await eng.on_forget(PREFIX + "lamp")

    before = eng._accepted
    await eng.on_state(_state(PREFIX + "lamp", ts=2000))
    await eng.on_state(_state(PREFIX + "lamp", ts=3000))

    assert eng._accepted == before, "re-publishes after removal are dropped"
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id=$1", PREFIX + "lamp") == 0

    await _clean(pool)
    await pool.close()


async def test_forgetting_a_DEVICE_key_blocks_every_entity_grouped_under_it():
    # A device is keyed by its grouping key; deleting "the TRV" must not leave its
    # nine entities streaming in.
    pool, _bus, eng = await _fresh()
    await eng.on_state(_state(PREFIX + "trv:temp", cap="temperature", value=21.0, device=PREFIX + "trv"))
    await eng.on_forget(PREFIX + "trv")

    before = eng._accepted
    await eng.on_state(_state(PREFIX + "trv:temp", cap="temperature", value=22.0, ts=2000, device=PREFIX + "trv"))
    assert eng._accepted == before, "an entity whose DEVICE key is blocked is blocked too"

    await _clean(pool)
    await pool.close()


# --- on_restore ---------------------------------------------------------------

async def test_restore_unblocks_the_key_so_the_device_can_come_back():
    """Dropping the tombstone ROW is not enough: the block is this in-memory set,
    so without on_restore the key stays deaf until an unrelated restart — which
    looks exactly like a restore that silently did nothing."""
    pool, bus, eng = await _fresh()
    await eng.on_forget(PREFIX + "lamp")
    assert PREFIX + "lamp" in eng._removed

    await eng.on_restore(PREFIX + "lamp")
    assert PREFIX + "lamp" not in eng._removed

    await eng.on_state(_state(PREFIX + "lamp", ts=5000))
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id=$1", PREFIX + "lamp") == 1, \
        "the device is heard again the moment it publishes"
    assert [e.kind for e in bus.journal] == ["removed", "restored"]

    await _clean(pool)
    await pool.close()


async def test_restoring_a_key_that_was_never_removed_is_a_no_op():
    pool, _bus, eng = await _fresh()
    await eng.on_restore(PREFIX + "never")  # must not raise
    assert eng._removed == set()
    await pool.close()


# --- on_entity_info -----------------------------------------------------------

async def test_announce_registers_an_entity_that_has_no_state_yet():
    """A write-only button has no value to report, so only this path can put it in
    the registry. Without it the device's field list is missing exactly the fields
    you cannot see any other way."""
    pool, _bus, eng = await _fresh()
    await eng.on_entity_info(EntityInfo(
        entity_id=PREFIX + "button", name="Laser", adapter="zzlife",
        capabilities=["press"], device=PREFIX + "panel", device_name="Panel"))

    row = await pool.fetchrow(
        "SELECT name, adapter, device_key, device_type, capabilities FROM entities WHERE entity_id=$1",
        PREFIX + "button")
    assert row is not None and row["name"] == "Laser"
    assert row["device_key"] == PREFIX + "panel"
    assert row["device_type"] is not None, "every entity gets a concrete canonical type"
    assert await pool.fetchval("SELECT name FROM devices WHERE device_key=$1", PREFIX + "panel") == "Panel", \
        "the friendly name lives once, on the device"

    await _clean(pool)
    await pool.close()


async def test_a_re_announce_never_overwrites_the_canonical_type():
    """device_type is SEEDED from the adapter then OWNED by DIDA. An adapter may
    change what it thinks a device is; DIDA's type does not follow it — otherwise
    a user's explicit choice is undone by the next reconnect."""
    pool, _bus, eng = await _fresh()
    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "x", adapter="zzlife",
                                        capabilities=["on_off"], device_type="light"))
    await pool.execute("UPDATE entities SET device_type='fan' WHERE entity_id=$1", PREFIX + "x")

    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "x", adapter="zzlife",
                                        capabilities=["on_off"], device_type="light"))

    assert await pool.fetchval("SELECT device_type FROM entities WHERE entity_id=$1", PREFIX + "x") == "fan", \
        "the stored type survives a re-announce"

    await _clean(pool)
    await pool.close()


async def test_announce_capabilities_are_AUTHORITATIVE_unlike_the_state_path():
    # The state path UNIONS capabilities (it only ever sees one at a time); an
    # announce carries the device's whole field set, so it replaces.
    pool, _bus, eng = await _fresh()
    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "m", adapter="zzlife",
                                        capabilities=["on_off", "brightness"]))
    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "m", adapter="zzlife",
                                        capabilities=["on_off"]))

    caps = await pool.fetchval("SELECT capabilities FROM entities WHERE entity_id=$1", PREFIX + "m")
    caps = msgspec.json.decode(caps) if isinstance(caps, str | bytes) else caps
    assert set(caps) == {"on_off"}, "a shrunken field set is reflected, not accumulated"

    await _clean(pool)
    await pool.close()


async def test_announce_is_dropped_for_a_forgotten_device():
    pool, _bus, eng = await _fresh()
    await eng.on_forget(PREFIX + "dead")
    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "dead", adapter="zzlife", capabilities=["on_off"]))
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id=$1", PREFIX + "dead") == 0, \
        "removal blocks the announce path too, not only state"
    await _clean(pool)
    await pool.close()


async def test_announce_with_oversized_metadata_is_rejected_at_the_boundary():
    pool, _bus, eng = await _fresh()
    await eng.on_entity_info(EntityInfo(entity_id=PREFIX + "big", adapter="zzlife",
                                        name="x" * 5000, capabilities=["on_off"]))
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE entity_id LIKE $1", PREFIX + "big%") == 0, \
        "a multi-MB metadata string never reaches the unbounded TEXT column"
    await _clean(pool)
    await pool.close()


# --- seed_missing_types -------------------------------------------------------

async def test_seed_missing_types_fills_nulls_and_leaves_everything_else_alone():
    pool, _bus, eng = await _fresh()
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, capabilities, device_type) "
        "VALUES ($1, 'test', '[\"on_off\",\"brightness\"]'::jsonb, NULL), "
        "       ($2, 'test', '[\"on_off\"]'::jsonb, 'fan')",
        PREFIX + "untyped", PREFIX + "typed")

    await eng.seed_missing_types()

    assert await pool.fetchval("SELECT device_type FROM entities WHERE entity_id=$1", PREFIX + "untyped") is not None, \
        "a type-less entity is classified from its capabilities"
    assert await pool.fetchval("SELECT device_type FROM entities WHERE entity_id=$1", PREFIX + "typed") == "fan", \
        "an entity that already has a type — seeded or user-chosen — is untouched"

    await _clean(pool)
    await pool.close()


async def test_seed_missing_types_converges_to_a_no_op():
    """It runs on every boot, so a second pass must find nothing left to do —
    otherwise it would rewrite (and could re-classify) rows forever."""
    pool, _bus, eng = await _fresh()
    await pool.execute(
        "INSERT INTO entities (entity_id, adapter, capabilities, device_type) "
        "VALUES ($1, 'test', '[\"on_off\"]'::jsonb, NULL)", PREFIX + "u")

    await eng.seed_missing_types()
    first = await pool.fetchval("SELECT device_type FROM entities WHERE entity_id=$1", PREFIX + "u")
    await eng.seed_missing_types()
    second = await pool.fetchval("SELECT device_type FROM entities WHERE entity_id=$1", PREFIX + "u")

    assert first == second
    assert await pool.fetchval("SELECT count(*) FROM entities WHERE device_type IS NULL AND entity_id LIKE $1",
                               PREFIX + "%") == 0

    await _clean(pool)
    await pool.close()
