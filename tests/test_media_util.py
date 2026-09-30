"""Regression tests for the pure media/radio helpers (dida_core.media).

icy_stream_title() and radio_probe() do real network I/O (urllib / aiohttp respectively) to probe a
live radio stream's ICY metadata — that I/O is not unit-testable without a
live stream or a mock HTTP server, so this file only exercises
icy_stream_title()'s deterministic FAIL-CLOSED path: any URL/connection error
is swallowed and it returns "" (never raises). Verified with inputs that fail
instantly and offline (a URL with no recognizable scheme, an empty URL, a
loopback port nothing listens on) — no dependency on outbound internet access
being available in the test environment. radio_probe() (async, aiohttp) and
the CODEC/_EXT lookup tables (plain dict constants, not functions) are out of
scope here.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_media_util.py"
"""
from dida_core.media import icy_stream_title


def test_icy_stream_title_malformed_url_is_fail_closed():
    assert icy_stream_title("not-a-url") == "", \
        "a URL with no recognizable scheme raises inside urlopen -> caught -> ''"
    assert icy_stream_title("") == "", "empty URL -> caught -> ''"


def test_icy_stream_title_connection_refused_is_fail_closed():
    # Loopback + a privileged port nothing listens on: refused instantly, no
    # dependency on outbound internet access being available in the test env.
    assert icy_stream_title("http://127.0.0.1:1/no-such-stream") == "", \
        "connection refused is swallowed -> '', never raises"
