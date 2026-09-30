"""Camera proxy credentials — which secret goes to which upstream.

A BABA camera descriptor names TWO servers on the same box: go2rtc (the pixels)
and BABA's REST API (the archive). They take different credentials, and the
descriptor carries a single `auth: baba` pointer, so the proxy works out which
from the location's configured bases: the descriptor stays free of secrets, and
a URL under neither base gets nothing.

Getting that split wrong fails in two ways that both look like "the wall is
black": the peer key sent to go2rtc (401, no frames) or — worse — BABA's peer key
handed to whatever is listening on the go2rtc port. Neither is visible from the
UI, so pin it here.

With several installs there is a third way to get it wrong, and it is the worst:
presenting ONE house's credentials to ANOTHER house's box. The descriptor's
`site` decides, so that choice is pinned too.

Run inside the api image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_api_camera_auth.py"
"""
import asyncio
import json
from base64 import b64decode

import pytest
from dida_api import camera

GO2RTC = "http://198.51.100.11:11984"
BABA_API = "http://198.51.100.11:8080"
FAR_GO2RTC = "https://cabin.example/go2rtc"
FAR_API = "https://cabin.example/api"

HOME = {
    "name": "Kuća", "nats_url": "nats://198.51.100.11:4222",
    "go2rtc": GO2RTC, "go2rtc_user": "baba", "go2rtc_password": "G2PW",
    "api_url": BABA_API, "peer_key": "PEER",
}
FAR = {
    "name": "Cabin", "nats_url": "nats://cabin.example:4222",
    "go2rtc": FAR_GO2RTC, "go2rtc_user": "baba", "go2rtc_password": "FARPW",
    "api_url": FAR_API, "peer_key": "FARPEER",
}

DESC = {
    "auth": "baba",
    "site": "Kuća",
    "stream": "gate",
    "snapshot": f"{GO2RTC}/api/frame.jpeg?src=gate",
    "mp4": f"{GO2RTC}/api/stream.mp4?src=gate",
    "events": f"{BABA_API}/events",
    "baba_id": "cam-uuid",
}
FAR_DESC = {
    **DESC,
    "site": "Cabin",
    "snapshot": f"{FAR_GO2RTC}/api/frame.jpeg?src=drive",
    "events": f"{FAR_API}/events",
}


_stored: dict[str, str] = {}


def seed(monkeypatch, sites, adapter="baba"):
    _stored[adapter] = json.dumps(sites)


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    """Stand in for the adapter config so no DB is needed."""
    _stored.clear()

    async def _site_config(pool, adapter, *, refresh=False):
        return _stored.get(adapter)

    monkeypatch.setattr(camera, "_site_config", _site_config)
    seed(monkeypatch, [HOME, FAR])


def headers(url, desc=DESC):
    return asyncio.run(camera._auth_headers(desc, url, None))


def test_media_plane_gets_go2rtc_basic_auth():
    h = headers(DESC["snapshot"])
    assert "X-Peer-Key" not in h, "BABA's peer key must never be offered to go2rtc"
    scheme, token = h["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert b64decode(token).decode() == "baba:G2PW"


def test_archive_plane_gets_the_peer_key():
    h = headers(DESC["events"])
    assert h == {"X-Peer-Key": "PEER"}
    assert "Authorization" not in h


def test_one_hostname_two_planes():
    """Reached over a tunnel, a remote install serves both planes on ONE hostname
    under different paths, and BOTH are answered by BABA's web server: it adds
    go2rtc's own basic auth itself and authenticates us as it does everywhere
    else. So the peer key covers the pixels too — offering go2rtc's password to
    that proxy just gets a 401 and an empty wall."""
    assert headers(FAR_DESC["events"], FAR_DESC) == {"X-Peer-Key": "FARPEER"}
    assert headers(FAR_DESC["snapshot"], FAR_DESC) == {"X-Peer-Key": "FARPEER"}


def test_direct_go2rtc_still_gets_basic_auth():
    """The LAN shape: separate ports, so the pixels come from go2rtc itself and
    it wants its own credentials. The peer key must not go there — that port is
    not BABA's API and has no business holding the key."""
    h = headers(DESC["snapshot"])
    assert "X-Peer-Key" not in h
    assert b64decode(h["Authorization"].split(" ", 1)[1]).decode() == "baba:G2PW"


def test_each_install_gets_its_own_credentials():
    """The whole point of the site list: a second BABA is a DIFFERENT box with
    different secrets. Sending the house's key to the holiday home (or the other
    way round) hands a working credential to a machine that was never meant to
    have it."""
    assert headers(FAR_DESC["events"], FAR_DESC) == {"X-Peer-Key": "FARPEER"}
    assert headers(DESC["events"]) == {"X-Peer-Key": "PEER"}
    home_media = headers(DESC["snapshot"])
    assert b64decode(home_media["Authorization"].split(" ", 1)[1]).decode() == "baba:G2PW"


def test_unknown_site_sends_nothing(monkeypatch):
    """A descriptor naming a location that is no longer configured (renamed,
    removed) gets NO credential. 401 is a bug report; the wrong house's key is a
    disclosure."""
    stray = {**DESC, "site": "Nekakva vikendica"}
    assert headers(stray["snapshot"], stray) == {}
    assert headers(stray["events"], stray) == {}


def test_siteless_descriptor_resolves_only_when_unambiguous(monkeypatch):
    """Descriptors published before the location list existed carry no `site`.
    With exactly one install there is nothing to confuse, so they keep working;
    with several, guessing would be picking a house at random."""
    old = {k: v for k, v in DESC.items() if k != "site"}
    seed(monkeypatch, [HOME])
    assert headers(old["events"], old) == {"X-Peer-Key": "PEER"}
    seed(monkeypatch, [HOME, FAR])
    assert headers(old["events"], old) == {}


def test_open_go2rtc_sends_no_credential(monkeypatch):
    """An install that never set a go2rtc password: send nothing, rather than
    falling back to the peer key (which would leak it to an open port)."""
    seed(monkeypatch, [{**HOME, "go2rtc_user": "", "go2rtc_password": ""}])
    assert headers(DESC["snapshot"]) == {}


def test_tile_width_is_asked_of_the_source_not_the_browser():
    """A tile-sized frame has to be requested from go2rtc, which resizes before
    sending: downscaling in the browser would already have spent the bytes, and
    on a remote site those bytes are the whole uplink. The scaled URL must stay
    inside the media plane so it still gets go2rtc's credential, not the archive's."""
    small = camera._scaled(DESC["snapshot"], 640)
    assert small.endswith("&width=640") and "src=gate" in small
    assert headers(small) == headers(DESC["snapshot"])
    assert b64decode(headers(small)["Authorization"].split(" ", 1)[1]).decode() == "baba:G2PW"
    far = camera._scaled(FAR_DESC["snapshot"], 640)
    assert headers(far, FAR_DESC) == headers(FAR_DESC["snapshot"], FAR_DESC) == {"X-Peer-Key": "FARPEER"}


def test_unscalable_source_is_fetched_as_published():
    """Frigate publishes its own downscaled snapshot URL and takes different
    resize parameters. Appending go2rtc's would be a guess at another product's
    API — a broken tile — so a source we can't ask is left alone."""
    frig = "https://nvr.example/api/drive/latest.jpg?height=480"
    assert camera._scaled(frig, 640) == frig
    assert camera._scaled(FAR_DESC["snapshot"], None) == FAR_DESC["snapshot"]


def test_descriptor_carries_no_secret():
    """The descriptor reaches the browser. Whatever we send upstream is resolved
    server-side from adapter config, so neither secret may appear in it."""
    blob = str(DESC) + str(FAR_DESC)
    assert "G2PW" not in blob and "PEER" not in blob
    assert "FARPW" not in blob and "FARPEER" not in blob


def test_a_notification_frame_is_asked_for_at_phone_width(monkeypatch, tmp_path):
    """A push carries a link the phone downloads in a few seconds and decodes into
    memory. The doorbell's full frame is 1920×2560; the phone never shows more than
    its own width, so the source is asked for that, not the original."""
    import types

    asked = []

    class Pool:
        async def fetchval(self, sql, entity_id):
            return json.dumps(DESC) if entity_id == "baba:gate" else None

    async def fetch(desc, url, pool, timeout_s):
        asked.append(url)
        return types.SimpleNamespace(status_code=200, content=b"\xff\xd8jpeg")

    monkeypatch.setattr(camera, "_fetch_upstream", fetch)
    monkeypatch.setattr(camera, "_SNAP_DIR", tmp_path)
    app = types.SimpleNamespace(state=types.SimpleNamespace(pool=Pool()))
    link = asyncio.run(camera.mint_snapshot(app, "baba:gate:zone:front"))
    assert asked == [f"{GO2RTC}/api/frame.jpeg?src=gate&width=1080"]
    assert link and (tmp_path / link.rsplit("/", 1)[1]).read_bytes() == b"\xff\xd8jpeg"



def test_the_live_mp4_rides_the_media_plane_credentials():
    """The full-resolution stream is go2rtc's, like the snapshot: go2rtc's own
    basic auth on the LAN, the peer key through BABA's proxy, never the other
    plane's secret."""
    h = headers(f"{GO2RTC}/api/stream.mp4?src=gate")
    assert b64decode(h["Authorization"].split(" ", 1)[1]).decode() == "baba:G2PW"
    assert headers(f"{FAR_GO2RTC}/api/stream.mp4?src=drive", FAR_DESC) == {"X-Peer-Key": "FARPEER"}


def test_the_ring_still_is_the_archive_plane():
    """BABA's decoded-frame still is its REST API, so it takes the peer key and
    never go2rtc's password."""
    assert headers(f"{BABA_API}/cameras/cam-uuid/live.jpg?boxes=false") == {"X-Peer-Key": "PEER"}


def _confined(desc, entity_id="baba:gate"):
    return asyncio.run(camera._confine(None, entity_id, desc))


def test_a_descriptor_url_outside_its_location_is_not_proxied():
    """The descriptor is a bus message. One naming another host — with `auth: baba`
    and the house's site name — would otherwise have the proxy present that
    house's go2rtc password and peer key to it."""
    forged = {**DESC, "snapshot": "http://203.0.113.9/api/frame.jpeg?src=gate",
              "events": f"{BABA_API}/events"}
    out = _confined(forged)
    assert "snapshot" not in out and out["events"] == f"{BABA_API}/events"


def test_the_other_houses_host_is_not_this_houses_camera():
    crossed = {**DESC, "mp4": f"{FAR_GO2RTC}/api/stream.mp4?src=drive"}
    assert "mp4" not in _confined(crossed)


def test_a_namespace_without_configured_locations_proxies_nothing():
    """Only the adapters that own camera locations have hosts to fetch from; a
    camera descriptor published under any other namespace names none."""
    out = _confined(DESC, entity_id="virtual:gate")
    assert not any(isinstance(v, str) and v.startswith("http") for v in out.values())


def test_frigate_is_confined_to_its_named_site(monkeypatch):
    seed(monkeypatch, [{"name": "Seaside", "url": "https://nvr.example",
                        "go2rtc": "https://nvr.example/go2rtc"}], adapter="frigate")
    desc = {"site": "Seaside", "snapshot": "https://nvr.example/api/drive/latest.jpg?height=480",
            "mp4": "https://nvr.example/go2rtc/api/stream.mp4?src=drive",
            "events": "http://192.168.1.1/api/events"}
    out = _confined(desc, entity_id="frigate:drive")
    assert set(out) == {"site", "snapshot", "mp4"}
