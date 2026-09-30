"""The GPS ingress: the one endpoint a phone reaches with a password-shaped secret.

`/api/owntracks` is the only route in DIDA that accepts HTTP Basic credentials
from the open internet (Cloudflare tunnel → UI proxy → API), and what it feeds is
not cosmetic: presence drives `helper:house_empty`, which drives heating and the
away automations. Two properties therefore have to hold and stay holding.

It must not be a login oracle. The secret is `users.owntracks_token`, endpoint-
scoped and separate from the argon2 login password — so a phone config lifted off
a lost handset, or a photograph of the setup QR, cannot log into the UI. If this
route ever started comparing against the password it would become a brute-force
surface with none of the login's protections, and nothing else in the system
would notice.

And it must not be brute-forceable. The verify is cached by credential hash so a
chatty phone skips the database; the rate limit sits behind that cache, which is
correct only as long as the cache key covers the WHOLE credential — a cache keyed
on the username alone would hand every subsequent attempt a free pass.
"""

from __future__ import annotations

import base64

import pytest
from dida_api import owntracks as mod
from dida_api import rate_limit
from fastapi import HTTPException


class _Pool:
    """Records what was asked, answers with one configured row."""

    def __init__(self, row=None) -> None:
        self.row = row
        self.queries: list[str] = []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return self.row

    async def fetch(self, sql, *args):
        return []


class _Request:
    def __init__(self, *, creds: str | None = None, header: str | None = None,
                 pool: _Pool | None = None, ip: str = "203.0.113.9",
                 body=None, agent: str = "Owntracks/2.4") -> None:
        if header is None and creds is not None:
            header = "Basic " + base64.b64encode(creds.encode()).decode()
        self.headers = {"user-agent": agent}
        if header:
            self.headers["authorization"] = header
        self.client = type("C", (), {"host": ip})()
        self.app = type("A", (), {"state": type("S", (), {
            "pool": pool or _Pool(), "bus": None})()})()
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _user(name="Marko", token="tok-abc"):
    return {"username": name, "owntracks_token": token}


@pytest.fixture(autouse=True)
def clean_state():
    """The cache and the bucket are module-level and would leak between tests —
    which is exactly how a rate-limit test comes to pass without a limiter."""
    mod._auth_cache.clear()
    mod._LIMITER = rate_limit.TokenBucketLimiter(capacity=5, refill_per_s=5 / 60.0)
    yield
    mod._auth_cache.clear()


# --- it is not a login-password oracle ----------------------------------------


async def test_the_token_column_is_the_one_consulted():
    """If this ever reads password_hash, a leaked phone config becomes a login."""
    pool = _Pool(_user())
    await mod.basic_user(_Request(creds="Marko:tok-abc", pool=pool))
    sql = " ".join(pool.queries).lower()
    assert "owntracks_token" in sql
    assert "password" not in sql, "the GPS endpoint is querying the login password"


async def test_a_user_without_a_provisioned_token_cannot_authenticate():
    """NULL must refuse, not compare equal to an empty secret — every account that
    has never been handed a phone would otherwise be open."""
    with pytest.raises(HTTPException) as e:
        await mod.basic_user(_Request(creds="Marko:", pool=_Pool(_user(token=None))))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        await mod.basic_user(_Request(creds="Marko:anything", pool=_Pool(_user(token=None))))


async def test_the_wrong_token_for_a_real_user_is_refused():
    with pytest.raises(HTTPException) as e:
        await mod.basic_user(_Request(creds="Marko:wrong", pool=_Pool(_user())))
    assert e.value.status_code == 401


async def test_an_unknown_user_is_refused():
    with pytest.raises(HTTPException):
        await mod.basic_user(_Request(creds="nobody:tok-abc", pool=_Pool(None)))


# --- the shape of the credential ----------------------------------------------


async def test_a_missing_authorization_header_asks_for_basic():
    """Without the challenge header the app shows a blank failure instead of a
    credentials prompt."""
    with pytest.raises(HTTPException) as e:
        await mod.basic_user(_Request())
    assert e.value.status_code == 401
    assert e.value.headers["WWW-Authenticate"].startswith("Basic")


async def test_a_bearer_token_is_not_accepted_here():
    with pytest.raises(HTTPException):
        await mod.basic_user(_Request(header="Bearer abc"))


async def test_undecodable_credentials_are_refused_not_crashed():
    """A 500 here is a 500 on an internet-facing route driven by third-party apps."""
    with pytest.raises(HTTPException) as e:
        await mod.basic_user(_Request(header="Basic !!!not-base64!!!"))
    assert e.value.status_code == 401


async def test_a_credential_with_no_token_part_is_refused():
    with pytest.raises(HTTPException):
        await mod.basic_user(_Request(header="Basic " + base64.b64encode(b"Marko").decode()))


async def test_the_username_is_case_insensitive_but_resolves_to_the_stored_one():
    """Phone keyboards capitalize the first letter. The CANONICAL name has to come
    back regardless, or the fix lands on `presence:marko` while the house watches
    `presence:Marko`."""
    pool = _Pool(_user(name="Marko"))
    assert await mod.basic_user(_Request(creds="marko:tok-abc", pool=pool)) == "Marko"
    assert "lower(" in " ".join(pool.queries).lower()


# --- the cache, and what sits behind it ----------------------------------------


async def test_a_repeat_ping_skips_the_database():
    """A phone reporting every few seconds must not run a query per fix."""
    pool = _Pool(_user())
    await mod.basic_user(_Request(creds="Marko:tok-abc", pool=pool))
    await mod.basic_user(_Request(creds="Marko:tok-abc", pool=pool))
    assert len(pool.queries) == 1


async def test_the_cache_key_covers_the_TOKEN_not_just_the_user():
    """The hole this test exists for: cache the username alone and a valid session
    from one phone authenticates every wrong token that follows."""
    good = _Pool(_user())
    await mod.basic_user(_Request(creds="Marko:tok-abc", pool=good))
    with pytest.raises(HTTPException):
        await mod.basic_user(_Request(creds="Marko:guessed", pool=_Pool(_user())))


async def test_a_cached_phone_is_not_charged_against_the_rate_limit():
    """The limiter sits BEHIND the cache on purpose. In front of it, a phone
    reporting normally would exhaust its own bucket and lock the family's presence
    out of the house — a self-inflicted outage rather than a defence."""
    pool = _Pool(_user())
    for _ in range(50):
        assert await mod.basic_user(_Request(creds="Marko:tok-abc", pool=pool)) == "Marko"


async def test_repeated_guesses_from_one_address_are_cut_off():
    for _ in range(5):
        with pytest.raises(HTTPException) as e:
            await mod.basic_user(_Request(creds="Marko:guess", pool=_Pool(_user())))
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        await mod.basic_user(_Request(creds="Marko:guess", pool=_Pool(_user())))
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"]


async def test_the_bucket_is_not_shared_with_the_login_endpoint():
    """A misconfigured phone hammering bad credentials must not lock the family out
    of the UI."""
    assert mod._LIMITER not in (rate_limit.LOGIN.per_client, rate_limit.LOGIN.per_account)


async def test_the_cache_is_bounded():
    """It is keyed by credential hash — an attacker supplies the key. Unbounded,
    that is a memory exhaustion primitive on an internet-facing route."""
    pool = _Pool()
    for i in range(mod._CACHE_MAX + 20):
        pool.row = _user(name=f"u{i}")
        # A fresh address each time: the per-IP bucket is not what is under test
        # here, and letting it fire would prove nothing about the cache.
        await mod.basic_user(_Request(creds=f"u{i}:tok-abc", pool=pool,
                                       ip=f"198.51.100.{i % 250 + 1}"))
    assert len(mod._auth_cache) <= mod._CACHE_MAX


# --- the frames themselves -----------------------------------------------------


@pytest.fixture
def published(monkeypatch):
    seen: list[tuple] = []

    async def _report(state, username, lat, lon, accuracy=None, battery=None):
        seen.append(("report", username, lat, lon, accuracy, battery))
        return {"accepted": True, "zone": "Home"}

    async def _transition(state, username, event, desc):
        seen.append(("transition", username, event, desc))
        return {"accepted": True, "zone": desc}

    async def _sync(pool, username):
        return []
    monkeypatch.setattr(mod, "publish_report", _report)
    monkeypatch.setattr(mod, "publish_transition", _transition)
    monkeypatch.setattr(mod, "_waypoint_sync", _sync)
    return seen


def _authed(body, **kw):
    return _Request(creds="Marko:tok-abc", pool=_Pool(_user()), body=body, **kw)


async def test_a_location_frame_is_published(published):
    await mod.owntracks_report(_authed(
        {"_type": "location", "lat": 43.5, "lon": 16.4, "acc": 12, "batt": 80}))
    assert published == [("report", "Marko", 43.5, 16.4, 12.0, 80.0)]


async def test_coordinates_outside_the_globe_are_refused(published):
    """A corrupt fix must not be resolved against the zone list: the nearest-zone
    maths happily returns an answer for lat=999 and the house acts on it."""
    for lat, lon in ((91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)):
        with pytest.raises(HTTPException) as e:
            await mod.owntracks_report(_authed({"_type": "location", "lat": lat, "lon": lon}))
        assert e.value.status_code == 400
    assert published == []


async def test_a_frame_without_coordinates_is_refused(published):
    for body in ({"_type": "location"},
                 {"_type": "location", "lat": "here", "lon": "there"},
                 {"_type": "location", "lat": None, "lon": None}):
        with pytest.raises(HTTPException) as e:
            await mod.owntracks_report(_authed(body))
        assert e.value.status_code == 400
    assert published == []


async def test_an_impossible_battery_reading_is_dropped_not_published(published):
    await mod.owntracks_report(_authed(
        {"_type": "location", "lat": 43.5, "lon": 16.4, "batt": 8000, "acc": "n/a"}))
    assert published[0][4] is None and published[0][5] is None


async def test_the_other_frames_the_app_sends_are_acknowledged_and_ignored(published):
    """Waypoints, status, steps — a 4xx makes the app retry them forever."""
    for kind in ("waypoints", "status", "steps", "card", "cmd"):
        assert await mod.owntracks_report(_authed({"_type": kind})) == []
    assert published == []


async def test_a_non_object_body_does_not_crash_the_route(published):
    assert await mod.owntracks_report(_authed([1, 2, 3])) == []
    assert published == []


async def test_a_body_that_is_not_json_is_a_400(published):
    with pytest.raises(HTTPException) as e:
        await mod.owntracks_report(_authed(None))
    assert e.value.status_code == 400


async def test_the_ios_mode_suffix_is_stripped_from_the_zone_name(published):
    """iOS echoes the region name back with the mode-switch suffix DIDA encoded
    into the waypoint ("Home|1|2"). Published raw, it names a zone that does not
    exist and the arrival is silently lost."""
    await mod.owntracks_report(_authed(
        {"_type": "transition", "event": "enter", "desc": "Home|1|2"}))
    assert published == [("transition", "Marko", "enter", "Home")]


async def test_a_plain_zone_name_survives_the_strip(published):
    await mod.owntracks_report(_authed(
        {"_type": "transition", "event": "leave", "desc": "Island House"}))
    assert published == [("transition", "Marko", "leave", "Island House")]


async def test_an_encrypted_frame_with_no_key_is_dropped_rather_than_erroring(
        published, monkeypatch):
    """The phone cannot fix a 4xx — it would retry a frame that can never open."""
    async def _no_setting(pool, key):
        return None
    monkeypatch.setattr(mod, "get_setting", _no_setting)
    assert await mod.owntracks_report(_authed(
        {"_type": "encrypted", "data": "AAAA"})) == []
    assert published == []


async def test_an_unauthenticated_frame_never_reaches_the_publisher(published):
    with pytest.raises(HTTPException):
        await mod.owntracks_report(_Request(
            body={"_type": "location", "lat": 43.5, "lon": 16.4}))
    assert published == []
