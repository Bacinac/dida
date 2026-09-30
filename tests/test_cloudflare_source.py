"""The ingress source parser/formatter — services.conf is the single source for the
Caddyfile, the tunnel, split-DNS and the Homepage tiles, so a row that survives a
round-trip wrong silently changes routing for the whole homelab.
"""
import pytest
from dida_adapter_cloudflare.adapter import format_source, parse_source, validate_row

SAMPLE = """\
# hostname                 origin                             serve  opts
baba                       http://192.168.10.11:5173          both   # section: Monitoring | icon: /icons/baba.png
dev-proxmox                https://192.168.1.12:8006          both  nochunk   # section: Development | icon: proxmox.png
development                ssh://192.168.1.210:22             wan
hp                         http://192.168.1.101:3000          both
"""


def test_parses_columns_and_trailing_metadata():
    rows = parse_source(SAMPLE)
    assert [r["host"] for r in rows] == ["baba", "dev-proxmox", "development", "hp"]
    assert rows[0]["section"] == "Monitoring" and rows[0]["icon"] == "/icons/baba.png"
    assert rows[1]["opts"] == ["nochunk"]
    assert rows[2]["serve"] == "wan"
    # a row without a comment carries no tile, which is not the same as an empty one
    assert rows[3]["section"] is None and rows[3]["icon"] is None


def test_round_trip_is_stable():
    once = format_source(parse_source(SAMPLE))
    assert parse_source(once) == parse_source(format_source(parse_source(once)))


def test_round_trip_preserves_every_field():
    for before, after in zip(parse_source(SAMPLE),
                             parse_source(format_source(parse_source(SAMPLE))), strict=True):
        assert before == after


def test_rejects_a_row_the_renderer_would_refuse():
    with pytest.raises(ValueError):
        validate_row({"host": "x", "origin": "ssh://10.0.0.1:22", "serve": "both", "opts": []})
    with pytest.raises(ValueError):
        validate_row({"host": "app.example.com", "origin": "http://10.0.0.1", "serve": "both", "opts": []})
    with pytest.raises(ValueError):
        validate_row({"host": "x", "origin": "10.0.0.1:8080", "serve": "both", "opts": []})
    with pytest.raises(ValueError):
        validate_row({"host": "x", "origin": "http://10.0.0.1", "serve": "both", "opts": ["nope"]})
    # ssh:// is fine once it is wan-only
    validate_row({"host": "x", "origin": "ssh://10.0.0.1:22", "serve": "wan", "opts": []})


def test_a_malformed_line_fails_loud():
    with pytest.raises(ValueError):
        parse_source("only-two-columns http://10.0.0.1\n")
    with pytest.raises(ValueError):
        parse_source("host http://10.0.0.1 sideways\n")
