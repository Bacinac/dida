"""Web push: whose phone buzzes, and who is allowed to stop it.

A push subscription is a per-browser capability — an endpoint URL at Google's or
Mozilla's push service that anyone holding it can send an encrypted payload to.
DIDA stores one row per browser and fans notifications out over them, which makes
two boundaries matter. Unsubscribing is scoped to the caller's own rows, or one
household member could silence another's phone and the alarm that mattered would
arrive nowhere. And the test notification fires at the CALLER's notify entity,
never at a name they supply, or the button becomes a way to make someone else's
phone go off.

The keypair has a different failure mode: it is generated once and never
replaced, because every stored subscription is bound to the public key. Rotating
it does not break loudly — it strands every phone silently, and the first thing
anyone notices is that a notification did not arrive months later.
"""

from __future__ import annotations

import pytest
from dida_api import push as mod
from dida_api.auth import AuthUser
from fastapi import HTTPException
from pydantic import ValidationError

MARKO = AuthUser(id=1, username="marko", role="admin")
GOST = AuthUser(id=2, username="gost", role="user")
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abcdef123456"


class _Pool:
    def __init__(self, count=1) -> None:
        self.count = count
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, *a):
        self.calls.append((sql, a))

    async def fetchval(self, sql, *a):
        self.calls.append((sql, a))
        return self.count


class _Bus:
    def __init__(self) -> None:
        self.commands: list = []

    async def publish_command(self, cmd):
        self.commands.append(cmd)


class _Request:
    def __init__(self, pool=None, bus=None, agent="Firefox/141") -> None:
        self.pool = pool or _Pool()
        self.bus = bus or _Bus()
        self.headers = {"user-agent": agent}
        self.app = type("A", (), {"state": type("S", (), {
            "pool": self.pool, "bus": self.bus})()})()


def _sub(endpoint=ENDPOINT):
    return mod.PushSubscribeIn(endpoint=endpoint,
                               keys={"p256dh": "BPk...", "auth": "tok"})


# --- what may be stored as an endpoint ------------------------------------------


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abc",
    "javascript:alert(1)//aaaaaaaaaa",
    "file:///etc/passwd",
    "//fcm.googleapis.com/fcm/send/abc",
    "ftp://example.com/aaaaaaaa",
    "",
    "https://",
    "x" * 2049,
])
def test_an_endpoint_that_is_not_an_https_url_is_refused(endpoint):
    """The endpoint is a URL the notify adapter will POST encrypted payloads to.
    Anything the model accepts here, the adapter later dials."""
    with pytest.raises(ValidationError):
        mod.PushSubscribeIn(endpoint=endpoint, keys={"p256dh": "k", "auth": "a"})


@pytest.mark.parametrize("endpoint", [
    "https://evil.example/fcm/send/abc",
    "https://fcm.googleapis.com.evil.example/fcm/send/abc",
    "https://fcm.googleapis.com@evil.example/fcm/send/abc",
    "https://192.168.1.100/fcm/send/abc",
    "https://fcm.googleapis.com:8443/fcm/send/abc",
    "https://notify.windows.com.example/abc",
])
def test_an_endpoint_outside_the_browsers_push_services_is_refused(endpoint):
    """The notify adapter dials what is stored; anything else here would make it
    request a LAN address or a stranger's server."""
    with pytest.raises(ValidationError):
        mod.PushSubscribeIn(endpoint=endpoint, keys={"p256dh": "k", "auth": "a"})


@pytest.mark.parametrize("endpoint", [
    ENDPOINT,
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABk",
    "https://web.push.apple.com/QGuQyavXutnMH7tH",
    "https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB",
])
def test_every_browser_push_service_is_accepted(endpoint):
    assert mod.PushSubscribeIn(endpoint=endpoint, keys={"p256dh": "k", "auth": "a"}).endpoint == endpoint


def test_the_subscription_keys_are_required_and_bounded():
    """Without them the payload cannot be encrypted; unbounded they are a row the
    caller sizes."""
    for keys in ({"p256dh": "", "auth": "a"}, {"p256dh": "k", "auth": ""},
                 {"p256dh": "x" * 513, "auth": "a"}, {"p256dh": "k", "auth": "x" * 257}):
        with pytest.raises(ValidationError):
            mod.PushSubscribeIn(endpoint=ENDPOINT, keys=keys)


async def test_a_subscription_is_stored_against_the_logged_in_user():
    req = _Request()
    await mod.push_subscribe(_sub(), req, user=GOST)
    sql, args = req.pool.calls[0]
    assert "INSERT INTO push_subscriptions" in sql
    assert args[0] == GOST.id and args[1] == ENDPOINT


async def test_an_endpoint_another_user_holds_is_not_taken_over():
    """Re-posting someone's endpoint used to move it to the poster: their phone went
    quiet, and what the poster's keys encrypted it could not read. A shared tablet
    still changes hands — notifications off and on subscribes it afresh."""
    req = _Request(pool=_Pool(count=None))
    with pytest.raises(HTTPException) as e:
        await mod.push_subscribe(_sub(), req, user=GOST)
    assert e.value.status_code == 409
    sql, _ = req.pool.calls[0]
    assert "WHERE push_subscriptions.user_id = EXCLUDED.user_id" in sql
    assert "SET user_id" not in sql


async def test_the_user_agent_is_bounded_before_it_is_stored():
    """It is an attacker-supplied header, recorded only so a person can tell their
    phone from their laptop in the list."""
    req = _Request(agent="M" * 5000)
    await mod.push_subscribe(_sub(), req, user=GOST)
    assert len(req.pool.calls[0][1][4]) <= 200


# --- who can stop a phone buzzing -----------------------------------------------


async def test_unsubscribing_is_scoped_to_the_callers_own_rows():
    """The endpoint alone is the identifier. Without the user_id predicate, anyone
    who learns another household member's endpoint can silence their phone — and
    the alarm that mattered then arrives nowhere, with nothing logged."""
    req = _Request()
    await mod.push_unsubscribe(mod.PushEndpointIn(endpoint=ENDPOINT), req, user=GOST)
    sql, args = req.pool.calls[0]
    assert "DELETE FROM push_subscriptions" in sql
    assert "user_id = $2" in sql
    assert args == (ENDPOINT, GOST.id)


async def test_the_owner_of_this_browsers_subscription_is_told_apart():
    """The card says "on" only when notifications reach the logged-in user. A
    subscription left behind by someone else on a shared browser is theirs, and a
    browser subscription the server never stored reaches nobody."""
    endpoint = mod.PushEndpointIn(endpoint=ENDPOINT)
    for holder, owner in ((GOST.id, "self"), (MARKO.id, "other"), (None, "none")):
        req = _Request(pool=_Pool(count=holder))
        assert await mod.push_owner(endpoint, req, user=GOST) == {"owner": owner}
        sql, args = req.pool.calls[0]
        assert "SELECT user_id FROM push_subscriptions" in sql and args == (ENDPOINT,)


# --- the test button ------------------------------------------------------------


async def test_the_test_notification_goes_to_the_caller_not_a_supplied_name():
    """There is no target parameter, and there must not be one: the button would
    become a way to make another person's phone go off."""
    req = _Request()
    await mod.push_test(mod.PushTestIn(message="bok"), req, user=GOST)
    cmd = req.bus.commands[0]
    assert cmd.entity_id == "notify:gost"
    assert cmd.source == "user:gost"


def test_the_test_route_takes_no_target_parameter():
    """Structural, so adding one is a visible decision rather than a convenience."""
    assert set(mod.PushTestIn.model_fields) == {"message"}


async def test_a_user_with_no_subscription_is_told_so():
    """Publishing anyway succeeds on the bus and delivers nothing — the person
    presses Test, sees success, and waits for a notification that cannot arrive."""
    req = _Request(pool=_Pool(count=0))
    with pytest.raises(HTTPException) as e:
        await mod.push_test(mod.PushTestIn(), req, user=GOST)
    assert e.value.status_code == 400
    assert req.bus.commands == []


async def test_an_empty_message_still_sends_something_readable():
    req = _Request()
    await mod.push_test(mod.PushTestIn(message="   "), req, user=MARKO)
    assert req.bus.commands[0].args["message"].strip()


async def test_the_test_goes_through_the_same_path_an_automation_uses():
    """A test that took a shortcut would prove the shortcut works. This one has to
    traverse bus → notify adapter → push service → service worker, so a green
    result means the chain is whole."""
    req = _Request()
    await mod.push_test(mod.PushTestIn(), req, user=MARKO)
    cmd = req.bus.commands[0]
    assert (cmd.capability, cmd.command) == ("notify", "notify")


# --- the keypair ----------------------------------------------------------------


async def test_an_existing_keypair_is_never_regenerated(monkeypatch):
    """Every stored subscription is bound to the public key. Replacing it strands
    all of them SILENTLY — no error anywhere, and the first symptom is a
    notification that did not arrive, months later."""
    written: list = []

    class _P:
        async def fetchval(self, sql, *a):
            return "existing-public-key"

        def acquire(self):
            raise AssertionError("keygen ran with a key already present")
    monkeypatch.setattr(mod, "ensure_app_setting",
                        lambda *a: written.append(a))
    await mod.ensure_vapid(_P())
    assert written == []


async def test_the_public_key_is_served_to_any_signed_in_user():
    """The browser needs it to subscribe at all; it is public by design."""
    req = _Request()
    req.pool.count = "BPublicKey"
    assert (await mod.vapid_key(req, _user=GOST)) == {"key": "BPublicKey"}


async def test_a_missing_keypair_is_reported_rather_than_served_empty():
    """`{"key": ""}` makes the browser fail deep inside the subscribe call with a
    message nobody can act on."""
    req = _Request()
    req.pool.count = None
    with pytest.raises(HTTPException) as e:
        await mod.vapid_key(req, _user=GOST)
    assert e.value.status_code == 503


async def test_the_notify_target_directory_is_admin_only():
    """It enumerates every user holding a subscription — the household roster,
    handed to a login that was given one page."""
    import inspect
    sig = inspect.signature(mod.notify_targets)
    dep = sig.parameters["_admin"].default
    assert getattr(dep, "dependency", None) is mod.require_admin
