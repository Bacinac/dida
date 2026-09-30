"""Camera proxying: which credentials go where, and which URL is asked for.

`camera.py` is 409 statements at 27%, and the untested parts include the two that
carry consequences beyond a broken image.

CREDENTIALS. Several NVRs share the one `frigate` adapter, and one Frigate instance
runs at a remote site over someone else's network. `_site_creds` picks the login by
ORIGIN. A loose match would send this house's Frigate password to another host —
not a bug you notice, because the request still returns a picture from the site that
happened to answer.

BYTES. The wall polls every camera every few seconds. A full 2K frame for a 320-px
tile is ten times the bytes for no visible detail, and on the remote site's
0.79 Mbit/s uplink that difference decides whether a recorded clip gets through at
all. `_scaled` asks go2rtc to resize server-side — silently dropping the parameter
would keep working, slowly, and only on the slow link.

All pure functions of their inputs, so no network and no database.
"""

from __future__ import annotations

import json

from dida_api.camera import _origin, _scaled, _site_creds, _within
from dida_core.baba_sites import BabaSite, site_of

SITES = json.dumps([
    {"url": "http://192.168.1.20:5000", "user": "home", "password": "home-pw"},
    {"url": "https://seaside.example.net", "user": "seaside", "password": "seaside-pw"},
])


# --- credentials --------------------------------------------------------------


def test_the_credentials_belong_to_the_origin_being_asked():
    assert _site_creds(SITES, "http://192.168.1.20:5000") == ("home", "home-pw")
    assert _site_creds(SITES, "https://seaside.example.net") == ("seaside", "seaside-pw")


def test_an_unknown_origin_gets_NO_credentials_rather_than_the_first_ones():
    """Falling back to "the first site" is how one house's password reaches
    another's NVR — and the request still returns a picture, so nothing looks wrong."""
    assert _site_creds(SITES, "http://192.168.1.99:5000") is None


def test_a_different_PORT_on_the_same_host_is_a_different_origin():
    """Two Frigate instances on one box is an ordinary setup; the port is part of
    the identity, not decoration."""
    assert _site_creds(SITES, "http://192.168.1.20:5001") is None


def test_a_different_SCHEME_is_a_different_origin():
    """http vs https to the same name is a different endpoint, and sending the
    password over the plaintext one is the failure worth refusing."""
    assert _site_creds(SITES, "http://seaside.example.net") is None


def test_a_site_with_half_its_credentials_is_treated_as_having_none():
    """A half-filled row in the editor must not produce a login attempt with an
    empty password — that is an auth failure the operator cannot explain."""
    half = json.dumps([{"url": "http://a.example", "user": "x", "password": ""}])
    assert _site_creds(half, "http://a.example") is None


def test_malformed_or_missing_site_config_yields_nothing():
    assert _site_creds(None, "http://a.example") is None
    assert _site_creds("{not json", "http://a.example") is None
    assert _site_creds('{"url": "http://a.example"}', "http://a.example") is None


def test_the_origin_ignores_the_path_and_query():
    assert _origin("http://host:5000/api/frame.jpeg?src=cam") == "http://host:5000"


# --- which install a camera belongs to ----------------------------------------


def _site(name, key):
    return BabaSite(name=name, nats_url=f"nats://{key}:4222", peer_key=key,
                    go2rtc_user=f"u-{key}", go2rtc_password=f"p-{key}")


def test_baba_credentials_are_matched_on_the_cameras_SITE():
    """Entity ids stay flat across installs, so the site name is the only thing that
    says which house this camera is in."""
    sites = [_site("Kuća", "k1"), _site("Seaside", "m1")]
    assert site_of(sites, "Seaside").peer_key == "m1"


def test_an_unknown_site_gets_NOTHING_rather_than_the_first_install():
    """Presenting the wrong house's peer key to a box is worse than a 401 — it is a
    credential leak that returns a picture."""
    sites = [_site("Kuća", "k1"), _site("Seaside", "m1")]
    assert site_of(sites, "Susjed") is None


def test_a_site_less_descriptor_falls_back_ONLY_when_there_is_one_install():
    """The single-location setup that predates the list keeps working…"""
    assert site_of([_site("Kuća", "k1")], "").peer_key == "k1"


def test_a_site_less_descriptor_with_SEVERAL_installs_gets_nothing():
    """…but with more than one there is no safe guess, and guessing hands one
    house's key to another's camera."""
    sites = [_site("Kuća", "k1"), _site("Seaside", "m1")]
    assert site_of(sites, "") is None


# --- bytes on the wire --------------------------------------------------------


def test_a_width_is_appended_to_a_snapshot_url():
    out = _scaled("http://host:1984/api/frame.jpeg?src=cam", 320)
    assert out.endswith("&width=320")


def test_a_url_with_no_query_gets_a_question_mark_not_an_ampersand():
    out = _scaled("http://host:1984/api/frame.jpeg", 320)
    assert "?width=320" in out and "&width" not in out


def test_no_width_means_the_url_is_untouched():
    url = "http://host:1984/api/frame.jpeg?src=cam"
    assert _scaled(url, None) == url


def test_a_url_we_do_not_know_how_to_resize_is_fetched_AS_PUBLISHED():
    """Frigate publishes its own downscaled URL. Appending a width go2rtc understands
    and Frigate does not would either be ignored or break the request — fetch what
    the adapter gave us instead of guessing."""
    url = "http://frigate:5000/api/cam/latest.jpg?h=360"
    assert _scaled(url, 320) == url


# --- media plane vs archive plane ---------------------------------------------


def test_the_plane_is_told_by_the_configured_base():
    lan = BabaSite(name="Kuća", nats_url="nats://host:4222",
                   go2rtc="http://host:1984", api_url="http://host:8080")
    assert lan.plane("http://host:1984/api/frame.jpeg?src=cam") == "media"
    assert lan.plane("http://host:8080/events") == "archive"
    assert lan.plane("http://host:19840/api/frame.jpeg") is None


def test_a_path_mounted_base_behind_a_tunnel_is_told_apart_too():
    far = BabaSite(name="Cabin", nats_url="wss://x.example",
                   go2rtc="https://x.example/go2rtc/", api_url="https://x.example/api")
    assert far.plane("https://x.example/go2rtc/api/frame.jpeg?src=cam") == "media"
    assert far.plane("https://x.example/api/events") == "archive"
    assert far.plane("https://x.example/admin") is None


# --- where the proxy may fetch from -------------------------------------------


def test_a_url_under_its_base_is_within():
    assert _within("http://host:1984/api/frame.jpeg?src=cam", "http://host:1984")
    assert _within("https://x.example/go2rtc/api/ws?src=a", "https://x.example/go2rtc/")


def test_a_look_alike_host_or_port_is_not_within():
    """A prefix without its slash would let `:19840` and `host.evil` through."""
    assert not _within("http://host:19840/api/frame.jpeg", "http://host:1984")
    assert not _within("http://host:1984.evil.example/x", "http://host:1984")
    assert not _within("http://host:1984@evil.example/x", "http://host:1984")


def test_a_dot_segment_is_refused_even_percent_encoded():
    """httpx collapses `..` before sending and the upstream may decode `%2e%2e`,
    so either walks a path-mounted base out to the rest of the hostname."""
    base = "https://x.example/go2rtc"
    assert not _within(f"{base}/../admin", base)
    assert not _within(f"{base}/api/%2e%2e/%2E%2E/admin", base)
    assert not _within(f"{base}/./api", base)


def test_no_base_admits_nothing():
    assert not _within("http://host:1984/api/frame.jpeg", "")
