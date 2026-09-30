"""Integration test — what is (and is not) offered for translation.

The catalog derives itself from live display names, which is what makes a new
adapter need no frontend work. It is also how it fills with junk: the tunnel's
ingress list publishes one entity per public hostname, so ~50 addresses landed in
the translator's queue, where translating one would produce a name resolving to
nothing while burying the strings a human actually has to translate.

So both surfaces — the management list and the autofill's work queue — must skip
link names and keep everything else, INCLUDING labels that merely contain a dot.

Runs in the api image against the runner's ephemeral Postgres (see tests/run.sh).
"""
import dida_api.app as appmod
from dida_api.translations import _KEYS_CTE
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

LINKS = ["dida.example.com", "backup.example.com", "nvr.example.com",
         "https://example.org/path", "ws://bus.example:4223"]
PROSE = ["Door Downstairs", "Contact last changed", "Day / night", "Car",
         "44.1kHz", "Temp. outside", "Upstairs"]


async def _clean(pool):
    await pool.execute("DELETE FROM entities WHERE entity_id LIKE 'trtest:%'")
    await pool.execute("DELETE FROM translations WHERE key = ANY($1::text[])", LINKS + PROSE)
    await pool.execute("DELETE FROM users WHERE username = 'tradmin'")


async def test_link_names_are_never_offered_for_translation():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await _clean(pool)
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('tradmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    for i, name in enumerate(LINKS + PROSE):
        await pool.execute(
            "INSERT INTO entities (entity_id, adapter, name) VALUES ($1, 'trtest', $2)",
            f"trtest:e{i}", name,
        )

    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "translations-test-secret-0123456789abcdef"

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        r = await c.post("/auth/login", json={"username": "tradmin", "password": "adminpw12"})
        assert r.status_code == 200
        offered = {row["key"] for row in (await c.get("/translations/keys")).json()}

    for link in LINKS:
        assert link not in offered, f"{link} is an address — translating it is meaningless"
    for phrase in PROSE:
        assert phrase in offered, f"{phrase} is prose and must stay translatable"

    # The autofill reads the same universe. Asserting on the shared CTE keeps the
    # two from drifting apart — the queue quietly regrowing link names would waste
    # LLM calls and refill the list the endpoint above just cleaned up.
    queued = {r["key"] for r in await pool.fetch(
        _KEYS_CTE + "SELECT k.key FROM translatable k LEFT JOIN translations t "  # noqa: S608
        "ON t.key = k.key AND t.lang = $1 WHERE t.value IS NULL OR t.value = ''", "hr")}
    assert not (queued & set(LINKS)), "autofill must not queue link names either"
    assert set(PROSE) <= queued, "…while still queueing every untranslated phrase"

    await _clean(pool)
    await pool.close()
