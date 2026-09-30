"""The alarm's last mile.

This adapter is the only thing standing between "DIDA decided you should know"
and a phone actually buzzing. Everything upstream of it is observable — the
command is in the audit trail, the rule firing is in the journal — so when it
fails, every surface still reads healthy and the only symptom is a notification
that never came. That is the worst shape a failure can take, and until now this
was 361 lines with no test at all.

What is pinned here is routing and degradation, not delivery: whether the right
targets are selected, whether an unroutable notification is loud, and whether a
losable extra (the camera frame) can take the message down with it.
"""
from __future__ import annotations

import pytest
from conftest import FakeBroker
from dida_adapter_notify.adapter import (
    ALL_ENTITY,
    NotifyAdapter,
    _app_build,
    _parse_targets,
)
from dida_core import Command, CommandRejected


def cmd(entity_id, command="notify", capability="notify", **args):
    return Command(entity_id=entity_id, capability=capability, command=command,
                   ts_ns=1, args=args)


def _adapter(topics=None, webpush=None, fcm=None):
    a = NotifyAdapter()
    a._topics = topics if topics is not None else {}
    a._webpush = webpush if webpush is not None else {}
    a._fcm = fcm if fcm is not None else {}
    if fcm:
        a._fcm_sender = object()  # non-None so routing counts FCM as reachable
    a.sent_ntfy, a.sent_push, a.sent_fcm = [], [], []

    async def fake_ntfy(topic, message, title, image):
        a.sent_ntfy.append((topic, message, title, image))
        return True

    async def fake_push(sub, message, title, image):
        a.sent_push.append((sub, message, title, image))
        return True

    async def fake_fcm(token, message, title, image):
        a.sent_fcm.append((token, message, title, image))
        return True

    async def no_snapshot(camera):
        return None

    a._send_ntfy, a._send_webpush, a._snapshot_url = fake_ntfy, fake_push, no_snapshot
    a._send_fcm = fake_fcm
    return a


# --- target parsing -----------------------------------------------------------

def test_targets_parse_into_slugged_entities():
    assert _parse_targets("alex:dida-alex,sam:dida-sam") == {
        "notify:alex": "dida-alex", "notify:sam": "dida-sam"}


def test_a_malformed_target_is_skipped_not_crashed():
    # One typo in Settings -> Adapters must not take the other targets with it.
    assert _parse_targets("alex:dida-alex,,garbage,:x,y:,sam:dida-sam") == {
        "notify:alex": "dida-alex", "notify:sam": "dida-sam"}


def test_no_targets_configured_is_an_empty_map_not_an_error():
    assert _parse_targets("") == {}
    assert _parse_targets(None) == {}


# --- routing ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_notification_reaches_exactly_its_own_target():
    a = _adapter(topics={"notify:alex": "t-alex", "notify:sam": "t-sam"})
    await a.handle_command(cmd("notify:alex", message="door open"))
    assert [t for t, *_ in a.sent_ntfy] == ["t-alex"], "only the addressed target"


@pytest.mark.asyncio
async def test_notify_all_reaches_every_target_once():
    # Two people can share a topic (a household tablet); a broadcast must not
    # deliver twice to it.
    a = _adapter(topics={"notify:alex": "shared", "notify:sam": "shared", "notify:kid": "t-kid"},
                 webpush={"notify:alex": [{"endpoint": "e1"}], "notify:sam": [{"endpoint": "e2"}]})
    await a.handle_command(cmd(ALL_ENTITY, message="alarm"))
    assert sorted(t for t, *_ in a.sent_ntfy) == ["shared", "t-kid"], "topics de-duplicated"
    assert len(a.sent_push) == 2, "every push subscription still gets its own copy"


@pytest.mark.asyncio
async def test_both_channels_fire_for_the_same_entity():
    """An automation does not know or care which channel reaches the phone — that
    is the whole point of converging them on one entity."""
    a = _adapter(topics={"notify:alex": "t-alex"}, webpush={"notify:alex": [{"endpoint": "e1"}]})
    await a.handle_command(cmd("notify:alex", message="hi"))
    assert len(a.sent_ntfy) == 1
    assert len(a.sent_push) == 1


@pytest.mark.asyncio
async def test_fcm_reaches_its_target_and_broadcast_dedupes():
    """FCM is the third transport for the same notify entities; a broadcast must
    hit each device token once, even when a token is shared across two users."""
    a = _adapter(fcm={"notify:alex": ["tok-a"], "notify:sam": ["tok-a", "tok-s"]})
    await a.handle_command(cmd("notify:sam", message="door"))
    assert sorted(t for t, *_ in a.sent_fcm) == ["tok-a", "tok-s"]

    a2 = _adapter(fcm={"notify:alex": ["tok-a"], "notify:sam": ["tok-a", "tok-s"]})
    await a2.handle_command(cmd(ALL_ENTITY, message="alarm"))
    assert sorted(t for t, *_ in a2.sent_fcm) == ["tok-a", "tok-s"], "device tokens de-duplicated"


@pytest.mark.asyncio
async def test_the_app_supersedes_web_push_for_its_user():
    """A phone with the app installed gets the native notification — the same
    alert to that user's browser subscription would just buzz them twice. ntfy
    is its own channel and is not suppressed."""
    a = _adapter(topics={"notify:alex": "t"}, webpush={"notify:alex": [{"endpoint": "e"}]},
                 fcm={"notify:alex": ["tok"]})
    await a.handle_command(cmd("notify:alex", message="hi"))
    assert len(a.sent_fcm) == 1 and len(a.sent_ntfy) == 1
    assert a.sent_push == [], "the app owns the phone; web push stays quiet"


@pytest.mark.asyncio
async def test_a_broadcast_suppresses_web_push_only_for_app_users():
    # A mixed household: alex has the app, sam is web-only (iPhone PWA).
    a = _adapter(webpush={"notify:alex": [{"endpoint": "e-alex"}],
                          "notify:sam": [{"endpoint": "e-sam"}]},
                 fcm={"notify:alex": ["tok-a"]})
    await a.handle_command(cmd(ALL_ENTITY, message="alarm"))
    assert [s["endpoint"] for s, *_ in a.sent_push] == ["e-sam"]
    assert [t for t, *_ in a.sent_fcm] == ["tok-a"]


@pytest.mark.asyncio
async def test_web_push_resumes_when_fcm_cannot_send():
    """Suppression is only justified while the app can actually be reached: with
    the service-account key unusable the FCM tokens are dead letters, so the
    browser subscription is the delivery route again."""
    a = _adapter(webpush={"notify:alex": [{"endpoint": "e"}]},
                 fcm={"notify:alex": ["tok"]})
    a._fcm_sender = None
    await a.handle_command(cmd("notify:alex", message="hi"))
    assert len(a.sent_push) == 1


@pytest.mark.asyncio
async def test_the_camera_frame_reaches_every_channel():
    """The channels must not drift apart on enrichments: a frame that web push
    carries while the FCM message goes out bare leaves the app notification
    blind to what the camera saw."""
    a = _adapter(topics={"notify:alex": "t"}, webpush={"notify:sam": [{"endpoint": "e"}]},
                 fcm={"notify:alex": ["tok"]})

    async def snapshot(camera):
        return "/api/snap/frame.jpg"

    a._snapshot_url = snapshot
    await a.handle_command(cmd(ALL_ENTITY, message="gate", camera="cam:gate"))
    assert a.sent_ntfy[0][3] == "/api/snap/frame.jpg"
    assert a.sent_push[0][3] == "/api/snap/frame.jpg"
    assert a.sent_fcm[0][3] == "/api/snap/frame.jpg"


@pytest.mark.asyncio
async def test_message_falls_back_to_value():
    # The automations builder's generic single-value action form collects `value`.
    a = _adapter(topics={"notify:alex": "t"})
    await a.handle_command(cmd("notify:alex", value="from the generic form"))
    assert a.sent_ntfy[0][1] == "from the generic form"


# --- the failure that must be LOUD --------------------------------------------

@pytest.mark.asyncio
async def test_an_unroutable_notification_is_rejected_not_swallowed():
    """A renamed target or a typo in an automation leaves a notification with
    nowhere to go. A silently vanished NOTIFICATION is the worst artifact this
    adapter can produce — everything upstream still reads healthy."""
    a = _adapter(topics={"notify:alex": "t-alex"})
    with pytest.raises(CommandRejected, match="no target"):
        await a.handle_command(cmd("notify:ghost", message="nobody hears this"))
    assert a.sent_ntfy == []
    assert a.sent_push == []


@pytest.mark.asyncio
async def test_a_notification_no_route_delivered_is_rejected():
    a = _adapter(topics={"notify:alex": "t-alex"})

    async def refused(*_a):
        a._last_error = "ntfy HTTP 403"
        return False

    a._send_ntfy = refused
    with pytest.raises(CommandRejected, match="ntfy HTTP 403"):
        await a.handle_command(cmd("notify:alex", message="door open"))


# --- degradation --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_snapshot_helper_swallows_its_own_failure():
    """The picture is an enrichment: a notification that loses it still has to
    arrive. So the helper returns None rather than raising, and a dead camera
    costs the frame and nothing else."""
    a = NotifyAdapter()

    class Bus:
        class nc:
            @staticmethod
            async def request(*_a, **_k):
                raise TimeoutError("no reply")

    a._bus = Bus()
    assert await a._snapshot_url("cam:front") is None


@pytest.mark.asyncio
async def test_a_notification_still_goes_out_when_the_camera_is_dead():
    a = _adapter(topics={"notify:alex": "t-alex"})

    async def dead_camera(camera):
        return None

    a._snapshot_url = dead_camera
    await a.handle_command(cmd("notify:alex", message="alarm", camera="cam:front"))
    assert len(a.sent_ntfy) == 1, "the message arrives, without its picture"
    assert a.sent_ntfy[0][3] is None


@pytest.mark.asyncio
async def test_no_camera_named_means_no_snapshot_lookup():
    a = NotifyAdapter()
    a._bus = None
    assert await a._snapshot_url("") is None


# --- command discipline -------------------------------------------------------

@pytest.mark.asyncio
async def test_an_invalid_command_is_rejected_at_the_boundary():
    a = _adapter(topics={"notify:alex": "t"})
    with pytest.raises(CommandRejected):
        await a.handle_command(cmd("notify:alex", capability="on_off", command="turn_on"))
    assert a.sent_ntfy == [], "a command the capability model rejects never sends"


# --- waking a phone that went dark -------------------------------------------

def _wake_broker(fixes, tracked):
    """The api's side of the watchdog: the presence entities, and every current value
    under them — the GPS fixes AND the network adapters' always-fresh `location`, so
    asking for the wrong capability would read every phone as alive."""
    rows = [{"entity_id": e, "capability": c, "value": 0.0, "age": age}
            for e, age in fixes.items() for c in ("latitude", "longitude")]
    rows += [{"entity_id": e, "capability": "location", "value": "home", "age": 1.0} for e in tracked]

    def state(prefix="", capabilities=None, entity_ids=None):
        return [r for r in rows if r["entity_id"].startswith(prefix)
                and (not capabilities or r["capability"] in capabilities)]

    return FakeBroker(entities=lambda **_: [{"entity_id": e} for e in tracked], state=state)


class _Sender:
    def __init__(self, result="ok"):
        self.result, self.sent = result, []

    async def send_data(self, session, token, data):
        self.sent.append((token, data))
        return self.result


CURRENT_UA = "DIDA-App/v0.1.999 (Android)"


def _waker(tokens, fixes, result="ok", tracked=None):
    from dida_adapter_notify.adapter import NotifyAdapter

    if tracked is None:
        tracked = {f"presence:{u}" for u, _, _ in tokens}
    a = NotifyAdapter()
    a.broker = _wake_broker(fixes, tracked)
    a._fcm_rows = [{"username": u, "token": t, "user_agent": ua} for u, t, ua in tokens]
    a._session = object()
    a._fcm = {"notify:ema": ["tok-ema"]}  # non-empty: the roster gate
    a._fcm_sender = _Sender(result)
    return a


def _ago(hours):
    return hours * 3600.0


@pytest.mark.asyncio
async def test_a_phone_silent_for_hours_is_woken():
    # The engine only starts from the app's own onResume or a reboot, so nobody
    # can rescue a phone in another country. This push is the only way in.
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {"presence:ema": _ago(50)})
    await a._wake_dark_phones()
    assert a._fcm_sender.sent == [("tok-ema", {"type": "wake_location"})]


@pytest.mark.asyncio
async def test_a_phone_that_never_reported_is_woken_too():
    # No latitude row at all — a tracker that was provisioned and never armed.
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {})
    await a._wake_dark_phones()
    assert a._fcm_sender.sent


@pytest.mark.asyncio
async def test_a_reporting_phone_is_left_alone():
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {"presence:ema": _ago(0.1)})
    await a._wake_dark_phones()
    assert a._fcm_sender.sent == []


@pytest.mark.asyncio
async def test_freshness_comes_from_gps_not_the_network_verdict():
    # The whole trap: unifi keeps `location` current for a phone whose tracker
    # died weeks ago. Only latitude/longitude prove a GPS reporter is alive, so
    # a live `location` row must not call off the wake.
    a = _waker([("ema", "tok-ema", CURRENT_UA)],
               {"presence:ema": _ago(50), "presence:marko": _ago(0.1)})
    await a._wake_dark_phones()
    assert [t for t, _ in a._fcm_sender.sent] == ["tok-ema"]


@pytest.mark.asyncio
async def test_a_phone_that_cannot_answer_is_not_hammered():
    # Switched off, abroad without data, or force-stopped (FCM cannot reach a
    # force-stopped app at all). FCM keeps only the last message anyway.
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {"presence:ema": _ago(50)})
    await a._wake_dark_phones()
    await a._wake_dark_phones()
    await a._wake_dark_phones()
    assert len(a._fcm_sender.sent) == 1


@pytest.mark.asyncio
async def test_a_dead_token_is_pruned_while_waking():
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {"presence:ema": _ago(50)}, result="gone")
    await a._wake_dark_phones()
    assert a.broker.asked("push_prune") == [{"fcm_token": "tok-ema"}]


@pytest.mark.asyncio
async def test_without_a_service_account_no_wake_is_attempted():
    # Mirrors the send path: no key means every push is dropped, and pretending
    # otherwise would mark phones as woken when nothing left the building.
    a = _waker([("ema", "tok-ema", CURRENT_UA)], {"presence:ema": _ago(50)})
    sender, a._fcm_sender = a._fcm_sender, None
    await a._wake_dark_phones()
    assert sender.sent == []


def test_the_app_build_is_read_out_of_the_user_agent():
    assert _app_build("DIDA-App/v0.1.858 (Android)") == 858
    # Unreadable must read as OLD: guessing the other way buzzes a phone that
    # cannot act on the message.
    assert _app_build("Owntracks-Android/2.4") == 0
    assert _app_build(None) == 0


@pytest.mark.asyncio
async def test_an_app_too_old_to_understand_the_wake_is_not_sent_one():
    # The one that bit for real: an older FcmService renders ANY data message as
    # a notification, so a wake it cannot act on is just a blank buzz — hourly,
    # for a fault its owner cannot fix from the notification shade.
    a = _waker([("ema", "tok-ema", "DIDA-App/v0.1.775 (Android)")],
               {"presence:ema": _ago(50)})
    await a._wake_dark_phones()
    assert a._fcm_sender.sent == []


@pytest.mark.asyncio
async def test_a_phone_whose_location_is_not_tracked_is_left_alone():
    # Holding the app for notifications is not consent to be located. No
    # presence entity, no ping — the person was never on that map.
    a = _waker([("tomo", "tok-tomo", CURRENT_UA)], {}, tracked=set())
    await a._wake_dark_phones()
    assert a._fcm_sender.sent == []


# --- what the alert watchdog asks: who can actually be reached -----------------


def test_routes_are_the_targets_a_notification_would_leave_for():
    a = _adapter(topics={"notify:garage": "t1"}, fcm={"notify:marko": ["tok"], "notify:gone": []})
    a._webpush = {"notify:ana": [{"endpoint": "x"}]}
    a._vapid_priv = None
    assert a.routes() == ["notify:garage", "notify:marko"], "web push without its key, and a user with no token, reach nobody"
    a._vapid_priv = "key"
    assert a.routes() == ["notify:ana", "notify:garage", "notify:marko"]


def test_a_phone_token_without_the_sending_key_is_not_a_route():
    a = _adapter(fcm={"notify:marko": ["tok"]})
    a._fcm_sender = None
    assert a.routes() == []
