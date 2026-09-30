"""Integration — the household address book (dida_api.contacts + core.store_book)
against a real Postgres.

What the tests hold in place is the SHARE: OPUS Library stopped reading Google and
now asks DIDA, so `/contacts/person` is the only place a birth year comes from. A
wrong answer there puts a birth year on the wrong face, silently — hence a refusal
where the book is ambiguous rather than a plausible guess.

Runs in the api image; the runner supplies the ephemeral Postgres (tests/run.sh).
Bypasses the app lifespan by wiring app.state directly.
"""
import datetime
from zoneinfo import ZoneInfo

import dida_api.app as appmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from dida_core.people import store_book
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


async def _fresh_pool():
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM people")
    await pool.execute("DELETE FROM users WHERE username IN ('ctadmin', 'ctlib')")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('ctadmin', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    await pool.execute(
        "INSERT INTO users (username, password_hash, role, can_control) "
        "VALUES ('ctlib', $1, 'user', false)",
        await hash_password("libpw1234"),
    )
    # The house's clock. Every service container runs UTC, so without this the
    # endpoints would answer on the container's date — see the tz test below.
    await pool.execute(
        "INSERT INTO adapter_config (adapter, key, value) VALUES ('astro', 'tz', 'UTC') "
        "ON CONFLICT (adapter, key) DO UPDATE SET value = EXCLUDED.value"
    )
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "contacts-test-secret-0123456789ab"
    appmod.app.state.bus = StubBus()
    return pool


def _entry(name, born, contact_id="", raw=""):
    return {"name": name, "born": born, "id": contact_id, "raw": raw}


def _born_on(year, day):
    """A birth date falling on `day`'s month-and-day. 29 February moves to the
    28th in a common birth year — otherwise this suite breaks one day in four."""
    try:
        return day.replace(year=year)
    except ValueError:
        return day.replace(year=year, day=28)


async def test_a_rename_in_the_book_is_a_rename_here():
    pool = await _fresh_pool()
    book = [_entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1")]
    report = await store_book(pool, book, source="google", prune=True)
    assert (report["added"], report["updated"]) == (1, 0)

    # Same contact, new surname. Matched on the contact it came from, so it is the
    # same person — not a stranger plus an orphan nobody's rule points at any more.
    book = [_entry("Ana Marić", datetime.date(1992, 6, 15), "people/c1")]
    report = await store_book(pool, book, source="google", prune=True)
    assert (report["added"], report["updated"], report["removed"]) == (0, 1, 0)
    assert await pool.fetchval("SELECT count(*) FROM people") == 1
    assert await pool.fetchval("SELECT name FROM people") == "Ana Marić"


async def test_a_birthday_with_no_year_is_reported_and_never_stored():
    pool = await _fresh_pool()
    report = await store_book(pool, [
        _entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1"),
        _entry("Marko Marić", None, "people/c2", raw="--06-16"),
    ], source="google", prune=True)
    assert report["added"] == 1
    assert report["no_year"] == [{"name": "Marko Marić", "says": "--06-16"}]
    assert await pool.fetchval("SELECT count(*) FROM people") == 1


async def test_only_a_complete_read_prunes():
    pool = await _fresh_pool()
    await store_book(pool, [
        _entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1"),
        _entry("Marko Marić", datetime.date(2014, 6, 16), "people/c2"),
    ], source="google", prune=True)

    # A file export is usually a subset; pruning against one would empty the table.
    report = await store_book(
        pool, [_entry("Ana Horvat", datetime.date(1992, 6, 15))], source="file", prune=False
    )
    assert report["removed"] == 0
    assert await pool.fetchval("SELECT count(*) FROM people") == 2

    # A complete Google read that no longer holds Marko means Marko is gone.
    report = await store_book(
        pool, [_entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1")],
        source="google", prune=True,
    )
    assert report["removed"] == 1
    assert await pool.fetchval("SELECT count(*) FROM people") == 1


async def test_an_ambiguous_name_is_refused_not_guessed():
    pool = await _fresh_pool()
    await store_book(pool, [
        _entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1"),
        _entry("Ana Horvat", datetime.date(2014, 3, 3), "people/c2"),
    ], source="google", prune=True)

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctlib", "password": "libpw1234"})).status_code == 200
        r = await c.get("/contacts/person", params={"name": "Ana Horvat"})
        assert r.status_code == 404, "two people of that name — a guess would date the wrong face"
        assert r.json()["detail"] == "ambiguous"

    # A third contact under that name must not become a birth date for the other two.
    report = await store_book(
        pool, [_entry("Ana Horvat", datetime.date(1970, 1, 1))], source="file", prune=False
    )
    assert report["added"] == 1 and report["updated"] == 0


async def test_the_library_looks_a_person_up_however_the_name_is_spelled():
    pool = await _fresh_pool()
    await store_book(
        pool, [_entry("Ana Marija Kovačić", datetime.date(1992, 6, 15), "people/c9")],
        source="google", prune=True,
    )
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctlib", "password": "libpw1234"})).status_code == 200
        for spelling in ("Ana Marija Kovačić", "Kovacic Ana Marija", "ana marija kovacic"):
            r = await c.get("/contacts/person", params={"name": spelling})
            assert r.status_code == 200, spelling
            assert r.json() == {"name": "Ana Marija Kovačić", "born_on": "1992-06-15",
                                "contact_id": "people/c9"}
        assert (await c.get("/contacts/person", params={"name": "Nitko Nikić"})).status_code == 404


async def test_birthdays_answers_the_whole_book_and_says_who_is_announced():
    pool = await _fresh_pool()
    today = datetime.date.today()
    await store_book(pool, [
        _entry("Danas Rodjendan", _born_on(1990, today), "people/c1"),
        _entry("Za Sto Dana", _born_on(1985, today + datetime.timedelta(days=100)), "people/c2"),
    ], source="google", prune=True)
    await pool.execute("UPDATE people SET announce = true WHERE contact_id = 'people/c1'")

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctlib", "password": "libpw1234"})).status_code == 200
        body = (await c.get("/contacts/birthdays", params={"within": 2})).json()
        assert [p["name"] for p in body] == ["Danas Rodjendan"]
        assert body[0]["in_days"] == 0 and body[0]["turns"] == today.year - 1990
        assert body[0]["announce"] is True


async def test_the_roster_and_the_google_handshake_are_admin_only():
    pool = await _fresh_pool()
    await store_book(
        pool, [_entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1")],
        source="google", prune=True,
    )
    person_id = await pool.fetchval("SELECT id FROM people")
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctlib", "password": "libpw1234"})).status_code == 200
        for path in ("/contacts/status", "/contacts/login", "/contacts/people"):
            assert (await c.get(path)).status_code == 403, path
        assert (await c.patch(f"/contacts/people/{person_id}", json={"announce": True})).status_code == 403

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctadmin", "password": "adminpw12"})).status_code == 200
        assert (await c.get("/contacts/status")).json()["connected"] is False
        # Not configured: the flow refuses rather than sending the browser at Google
        # with an empty client id.
        assert (await c.get("/contacts/login")).status_code == 503
        assert (await c.patch(f"/contacts/people/{person_id}", json={"announce": True})).status_code == 200
        assert (await c.get("/contacts/people")).json()[0]["announce"] is True


async def test_an_export_file_is_the_way_in_when_oauth_is_refused():
    pool = await _fresh_pool()
    vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:Ana Horvat\nBDAY:1992-06-15\nEND:VCARD\n"
    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctadmin", "password": "adminpw12"})).status_code == 200
        r = await c.post("/contacts/import", json={"text": vcard})
        assert r.status_code == 200 and r.json()["added"] == 1
        assert (await c.post("/contacts/import", json={"text": "nothing here"})).status_code == 400
    assert await pool.fetchval("SELECT source FROM people") == "file"


async def test_whose_birthday_it_is_follows_the_house_clock_not_the_container():
    """The container runs UTC; the household does not. Between local midnight and
    02:00 CEST a UTC "today" is yesterday — which would answer yesterday's
    birthdays on the one night of the year that matters.

    Proven against a zone whose date differs from UTC's RIGHT NOW: UTC+14 differs
    whenever the UTC hour is ≥ 10, UTC−12 whenever it is < 12, so one of them
    always does — the test can never quietly pass by comparing a date to itself.
    """
    pool = await _fresh_pool()
    utc_today = datetime.datetime.now(datetime.UTC).date()
    zone = next(z for z in ("Pacific/Kiritimati", "Etc/GMT+12")
                if datetime.datetime.now(ZoneInfo(z)).date() != utc_today)
    house_today = datetime.datetime.now(ZoneInfo(zone)).date()
    await pool.execute("UPDATE adapter_config SET value = $1 "
                       "WHERE adapter = 'astro' AND key = 'tz'", zone)
    await store_book(
        pool, [_entry("Rodjendan Danas", _born_on(1990, house_today), "people/c1")],
        source="google", prune=True,
    )

    async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
        assert (await c.post("/auth/login", json={"username": "ctlib", "password": "libpw1234"})).status_code == 200
        body = (await c.get("/contacts/birthdays", params={"within": 0})).json()
    assert [p["name"] for p in body] == ["Rodjendan Danas"], (
        f"UTC says {utc_today}, the house ({zone}) says {house_today}"
    )


async def test_an_empty_book_never_empties_the_table():
    """A complete read that came back with nobody in it is a Google or permission
    anomaly far more often than a household that has left the address book — and
    only one of the two readings is unrecoverable."""
    pool = await _fresh_pool()
    await store_book(
        pool, [_entry("Ana Horvat", datetime.date(1992, 6, 15), "people/c1")],
        source="google", prune=True,
    )
    report = await store_book(pool, [], source="google", prune=True)
    assert report["removed"] == 0
    assert await pool.fetchval("SELECT count(*) FROM people") == 1
