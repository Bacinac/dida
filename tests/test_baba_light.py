"""The one thing DIDA writes to BABA: a camera's floodlight.

Everything else in this adapter is a mirror, and the parts of the lamp that break
quietly are the ones that look like a mirror and are not — a poll that fails, a
roster refresh that does not know the lamp exists, a toggle with nothing to toggle.

Run inside the baba adapter image (has aiohttp + dida_adapter_baba):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-baba:latest \
      -c "python -m pytest tests/test_baba_light.py"
"""
import asyncio
import json

import pytest
from conftest import FakeBroker
from dida_adapter_baba.adapter import BabaAdapter, _light_values
from dida_core import Command, CommandRejected
from dida_core.baba_sites import BabaSite, site_key
from dida_core.capabilities import CapabilityError, classify_device_type, validate_state

CAM = "b9e15771-a6de-44f0-b578-1bd3718d6360"   # west
LAMP = f"baba:{CAM}:light"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Stands in for aiohttp. Answers from a script and records every call, so a
    test can assert on what went out as well as on what came back."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, object]] = []

    def _next(self, method, url, body):
        self.calls.append((method, url, body))
        answer = self.answers.pop(0) if self.answers else _Resp(200, {"on": False, "bright": 0})
        return answer() if callable(answer) else answer

    def get(self, url, **_kw):
        return self._next("GET", url, None)

    def post(self, url, json=None, **_kw):
        return self._next("POST", url, json)


class _Bus:
    def __init__(self):
        self.states = []    # (entity_id, capability, value)
        self.entities = []  # (entity_id, capabilities)

    async def publish_state(self, u):
        self.states.append((u.entity_id, u.capability, u.value))

    async def publish_entity(self, info):
        self.entities.append((info.entity_id, list(info.capabilities)))

    async def publish_reachability(self, event):
        pass


def _adapter(*answers):
    from dida_adapter_baba.adapter import _Site

    a = BabaAdapter()
    a._bus = _Bus()
    a._session = _Session(*answers)
    site = _Site(BabaSite(**{"name": "Kuća", "nats_url": "nats://198.51.100.11:4222", "go2rtc": "",
                  "go2rtc_user": "", "go2rtc_password": "",
                  "api_url": "http://198.51.100.11:8080", "peer_key": "PEER"}))
    site.roster = {CAM}
    a._sites[site.key] = site
    a._camera_names[CAM] = "West"
    a._known_cameras.add(CAM)
    return a, site


async def _settle():
    for _ in range(4):  # let the spawned publish tasks run
        await asyncio.sleep(0)


def _cmd(capability, command, **args):
    return Command(entity_id=LAMP, capability=capability, command=command,
                   ts_ns=0, args=args)


@pytest.mark.asyncio
async def test_the_lamp_is_its_own_entity_beside_the_camera():
    """`on_off` on the CAMERA entity would classify the camera as a light — the wall
    would lose the camera it is named after. So the lamp is a sibling, exactly like a
    zone or the doorbell button, and it is announced only once the device has
    answered: a switch for a lamp that is not there is worse than no switch."""
    a, site = _adapter(_Resp(200, {"on": True, "bright": 10, "mode": 3}))
    state = await a._read_light(site, CAM)
    a._publish_light(CAM, *state)
    await _settle()
    assert a._bus.entities == [(LAMP, ["on_off", "brightness"])]
    assert classify_device_type(["on_off", "brightness"]) == "light"
    assert classify_device_type(["camera", "motion", "person_count", "object_class"]) != "light", \
        "the camera stays a camera — its own capabilities are untouched"
    assert (LAMP, "on_off", True) in a._bus.states
    assert (LAMP, "brightness", 10) in a._bus.states
    assert a._session.calls[0][1] == f"http://198.51.100.11:8080/cameras/{CAM}/light"


@pytest.mark.asyncio
async def test_a_no_lamp_answer_is_rested_but_not_final():
    """404 means this model has no white LED — asking it every minute for the life of
    the process is a request that can only ever be refused. But it is ALSO what a BABA
    that does not serve this endpoint yet answers, and the two halves of this feature
    deploy independently: held forever, the wrong deploy order leaves a house whose
    lamps are simply never announced, silent after the first minute. So the verdict
    rests, and expires."""
    import time as _t

    a, site = _adapter(_Resp(404))
    assert await a._read_light(site, CAM) is None
    assert a._has_light[CAM] is False
    assert a._bus.entities == [], "nothing is announced for a camera with no lamp"
    assert a._lamp_recheck[CAM] > _t.monotonic(), "and it is not asked again right away"
    from dida_adapter_baba.adapter import _NO_LIGHT_RECHECK_S
    assert a._lamp_recheck[CAM] <= _t.monotonic() + _NO_LIGHT_RECHECK_S, "but it does come back"


@pytest.mark.asyncio
async def test_a_camera_that_did_not_answer_is_not_a_lamp_that_is_off():
    """The difference that matters at dusk. A failed read must leave the last value
    standing: publishing "off" for a camera that was briefly away would tell the rule
    to switch on a lamp that is already lit, and — worse the other way — a lamp
    reported off while it is on is a rule that never fires again."""
    a, site = _adapter(_Resp(200, {"on": True, "bright": 10}), _Resp(502, text="camera did not answer"))
    a._publish_light(CAM, *await a._read_light(site, CAM))
    await _settle()
    before = list(a._bus.states)

    with pytest.raises(RuntimeError):
        await a._read_light(site, CAM)
    await _settle()
    assert a._bus.states == before, "a failed read publishes nothing at all"
    assert a._light_state[CAM] == (True, 10), "and the last thing the device said still stands"
    assert CAM not in a._has_light or a._has_light[CAM] is True, \
        "a camera that did not answer is not written off as lampless"


@pytest.mark.asyncio
async def test_the_roster_prune_leaves_the_lamp_alone():
    """The roster cannot say which camera has a lamp — the device answers that. Left
    to the level-2 prune the lamp is deleted on every roster refresh and re-announced
    a minute later, losing the room and floor-plan place it was given. A zone, which
    IS roster catalog, must still be prunable."""
    a, site = _adapter()
    a.broker = FakeBroker(
        entities=[{"entity_id": f"baba:{CAM}", "device_key": f"baba:{CAM}"},
                  {"entity_id": LAMP, "device_key": f"baba:{CAM}"},
                  {"entity_id": f"baba:{CAM}:zone:gone", "device_key": f"baba:{CAM}"}],
        forget={"kept": 0})
    a._cfg = type("C", (), {"get": lambda self, k, d="": '[{"name": "Kuća", "nats_url": "nats://x:4222"}]'
                            if k == "sites" else d})()
    site.live_subs = set()   # the roster declares neither the lamp nor the dead zone
    await a._prune_stale()
    forgotten = [e for a in a.broker.asked("forget") for e in a.get("entity_ids", ())]
    assert forgotten == [f"baba:{CAM}:zone:gone"], "the zone goes, the lamp stays"


@pytest.mark.asyncio
async def test_what_is_published_is_what_the_device_did():
    """Read back after the write, always. The camera can refuse, clamp, or be driven
    by somebody else in the same second — publishing the command we sent instead of
    the answer we got is how DIDA ends up sure of a lamp that is not on."""
    a, _ = _adapter(_Resp(200, {"on": False, "bright": 0}))
    await a.handle_command(_cmd("on_off", "turn_on"))
    await _settle()
    method, _url, body = a._session.calls[0]
    assert (method, body) == ("POST", {"on": True}), "we asked for on"
    assert (LAMP, "on_off", False) in a._bus.states, "and published what came back"


@pytest.mark.asyncio
async def test_toggle_before_the_lamp_was_ever_read_does_nothing():
    """A blind toggle is a coin flip on a lamp the neighbours can see. Until a poll
    has said which way it is, there is nothing to invert."""
    a, _ = _adapter()
    await a.handle_command(_cmd("on_off", "toggle"))
    await _settle()
    assert a._session.calls == []

    a._light_state[CAM] = (True, 10)
    await a.handle_command(_cmd("on_off", "toggle"))
    await _settle()
    assert a._session.calls[0][2] == {"on": False}


@pytest.mark.asyncio
async def test_brightness_alone_neither_lights_nor_darkens_the_lamp():
    """Setting how bright it would be is not the same as switching it on. The state
    that rides along is the one the device last reported, not a default."""
    a, _ = _adapter(_Resp(200, {"on": False, "bright": 60}))
    a._light_state[CAM] = (False, 10)
    await a.handle_command(_cmd("brightness", "set_brightness", value=60))
    await _settle()
    assert a._session.calls[0][2] == {"on": False, "bright": 60}
    assert (LAMP, "brightness", 60) in a._bus.states


@pytest.mark.asyncio
async def test_a_refused_write_is_never_published_as_success():
    """BABA answers with the camera's own words when a write does not take. Mirroring
    the request anyway would leave DIDA holding a lamp it believes is on."""
    a, _ = _adapter(_Resp(502, text="GetWhiteLed: camera did not answer"))
    await a.handle_command(_cmd("on_off", "turn_on"))
    await _settle()
    assert a._bus.states == [] and a._bus.entities == []


@pytest.mark.asyncio
async def test_everything_that_is_not_the_lamp_is_still_read_only():
    """The camera, its zones and its scenes are BABA's truth and are not writable
    here — a command for one is refused where the journal shows it and never
    reaches the network."""
    a, _ = _adapter()
    for eid in (f"baba:{CAM}", f"baba:{CAM}:zone:abc"):
        with pytest.raises(CommandRejected, match="not writable"):
            await a.handle_command(Command(entity_id=eid, capability="on_off",
                                           command="turn_on", ts_ns=0))
    await _settle()
    assert a._session.calls == []


def test_a_light_state_without_on_is_not_a_light_state():
    """`bright` alone says nothing about whether the lamp is lit, and a missing `on`
    read as False is a switch position nobody set. `bright` itself is optional — a
    lamp that only switches has none."""
    assert _light_values({"on": True, "bright": 40}) == (True, 40)
    assert _light_values({"on": True}) == (True, None)
    assert _light_values({"on": False, "bright": 140}) == (False, 100), "clamped to the capability's range"
    for bad in ({"bright": 40}, {"on": 1}, [], None, {"on": "true"}):
        with pytest.raises(RuntimeError):
            _light_values(bad)


@pytest.mark.asyncio
async def test_a_camera_that_cannot_answer_is_said_once_and_then_rested():
    """The patio's camera is not a Reolink: BABA reaches through to the device and
    gets nothing, which is a 502 forever, not a 404. Retried every minute and
    complained about every time, three such cameras write four thousand identical
    lines a day — which is not louder than one line, it is quieter. So: loud once,
    then rested, and the rest is what stops the retry."""
    import time as _t

    from dida_adapter_baba.adapter import _LIGHT_FAILS_BEFORE_REST

    a, site = _adapter()
    said = []
    for _ in range(_LIGHT_FAILS_BEFORE_REST):
        before = a._light_said.get(CAM, 0.0)
        a._light_unanswered(CAM, site, RuntimeError("HTTP 502"))
        said.append(a._light_said.get(CAM, 0.0) != before)
    assert said == [True] + [False] * (_LIGHT_FAILS_BEFORE_REST - 1), "one line, not one per pass"
    assert a._lamp_recheck[CAM] > _t.monotonic(), "and then it is left alone for a while"


@pytest.mark.asyncio
async def test_a_lamp_that_stops_answering_keeps_its_minute():
    """A camera whose lamp DIDA has read and now cannot is a REGRESSION, not a model
    without a lamp: it must keep being retried at the poll rate. Quiet, but not
    rested."""
    a, site = _adapter()
    a._has_light[CAM] = True
    for _ in range(5):
        a._light_unanswered(CAM, site, RuntimeError("HTTP 502"))
    assert CAM not in a._lamp_recheck, "the retry cadence is not backed off for a known lamp"
    assert a._light_fails[CAM] == 5


@pytest.mark.asyncio
async def test_an_answer_clears_the_complaint():
    """Once it answers again, the next failure is a new fact and is said out loud
    again — a rest that never resets is a fault reported once in the lifetime of the
    process."""
    a, site = _adapter(_Resp(200, {"on": True, "bright": 10}))
    a._light_unanswered(CAM, site, RuntimeError("HTTP 502"))
    assert a._light_fails[CAM] == 1

    await a._poll_light(site, CAM)          # the camera answers
    await _settle()
    assert CAM not in a._light_fails and CAM not in a._light_said
    assert (LAMP, "on_off", True) in a._bus.states

    a._light_unanswered(CAM, site, RuntimeError("HTTP 502"))
    assert a._light_fails[CAM] == 1, "the count — and the complaint — start again"


@pytest.mark.asyncio
async def test_the_camera_says_how_much_light_it_has():
    """The one instrument in the house that reaches a camera's dark. Mirrored
    verbatim from BABA's snapshot — DIDA re-derives nothing, because the band is
    already debounced and hysteretic where the pixels are."""
    a, _ = _adapter()
    a._camera_names[CAM] = "West"
    a._known_cameras.add(CAM)

    class Msg:
        data = json.dumps({"camera_id": CAM, "camera_name": "West",
                           "motion": False, "light_condition": "dark"}).encode()

    await a._on_state(a._sites[site_key("Kuća")], Msg())
    await _settle()
    assert (f"baba:{CAM}", "light_condition", "dark") in a._bus.states
    validate_state("light_condition", "dark")
    with pytest.raises(CapabilityError):
        validate_state("light_condition", "pitch black")


@pytest.mark.asyncio
async def test_an_absent_band_is_not_darkness():
    """BABA omits the field when it is not asserting a band. Read as "dark" that
    would arm a floodlight on a camera nobody has measured — so an absent field must
    leave the last value exactly where it stands."""
    a, _ = _adapter()
    a._camera_names[CAM] = "West"
    a._known_cameras.add(CAM)

    async def feed(payload):
        class Msg:
            data = json.dumps(payload).encode()
        await a._on_state(a._sites[site_key("Kuća")], Msg())
        await _settle()

    await feed({"camera_id": CAM, "camera_name": "West", "light_condition": "dark"})
    before = [s for s in a._bus.states if s[1] == "light_condition"]
    await feed({"camera_id": CAM, "camera_name": "West", "motion": True})
    after = [s for s in a._bus.states if s[1] == "light_condition"]
    assert after == before, "no field, no verdict — nothing is published"
