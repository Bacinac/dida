"""Regression tests for tunnel mode — the ingress of an installation that has no
host applier, where the connectors' own config.yml is the source.

Run inside the cloudflare adapter image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-cloudflare:latest \
      -c "python -m pytest tests/test_cloudflare_tunnel.py"

The invariant that matters is the round trip: whatever is read has to render back
to the same ingress, because this file IS the routing — a lossy rewrite silently
drops a hostname, and nobody finds out until that hostname stops answering.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from dida_adapter_cloudflare.adapter import CloudflareAdapter, validate_row
from dida_adapter_cloudflare.tunnel import CATCHALL, parse_config, render_config

ZONE = "example.com"


async def test_a_save_during_dns_work_reconciles_the_latest_saved_routes(tmp_path, monkeypatch):
    adapter = CloudflareAdapter()
    adapter._zone = ZONE
    adapter._token = "test-token"
    adapter._config = str(tmp_path / "config.yml")
    adapter._tunnel_meta = {"tunnel": "test-tunnel"}
    started, release = asyncio.Event(), asyncio.Event()
    batches = []

    async def ensure(rows):
        batches.append([row["host"] for row in rows])
        if len(batches) == 1:
            started.set()
            await release.wait()

    monkeypatch.setattr(adapter, "_ensure_dns", ensure)
    first = {"host": "a", "origin": "http://192.0.2.1:80"}
    second = {"host": "b", "origin": "http://192.0.2.2:80"}
    adapter._write_tunnel([first])
    await asyncio.wait_for(started.wait(), timeout=1)
    adapter._write_tunnel([first, second])
    release.set()
    await asyncio.wait_for(adapter._dns_task, timeout=1)
    assert batches == [["a"], ["a", "b"]]
    assert adapter._dns_error is None


async def test_dns_failures_remain_visible_until_the_latest_revision_succeeds(monkeypatch):
    adapter = CloudflareAdapter()
    adapter._token = "test-token"
    adapter.status = AsyncMock()
    adapter.status.error = lambda message: errors.append(message)
    errors, batches = [], []
    waiting, retry = asyncio.Event(), asyncio.Event()

    async def ensure(rows):
        batches.append([row["host"] for row in rows])
        if len(batches) == 1:
            raise OSError("DNS unavailable")

    async def sleep(seconds):
        waiting.set()
        await retry.wait()

    monkeypatch.setattr(adapter, "_ensure_dns", ensure)
    monkeypatch.setattr("dida_adapter_cloudflare.adapter.asyncio.sleep", sleep)
    adapter._schedule_dns([{"host": "a"}])
    await asyncio.wait_for(waiting.wait(), timeout=1)
    assert adapter._dns_error == "DNS unavailable"
    assert errors == ["DNS: DNS unavailable"]
    adapter._schedule_dns([{"host": "a"}, {"host": "b"}])
    retry.set()
    await asyncio.wait_for(adapter._dns_task, timeout=1)
    assert batches == [["a"], ["a", "b"]]
    assert adapter._dns_error is None

LIVE = """tunnel: 239a26fc-cc17-4151-b212-4d7f1b616de6
credentials-file: /etc/cloudflared/credentials.json
ingress:

  # ----- 192.168.2.11 -----

  - hostname: cabin-proxmox.example.com
    service: https://192.168.2.11:8006
    originRequest:
      noTLSVerify: true
      disableChunkedEncoding: true
  - hostname: px-cabin.example.com
    service: ssh://192.168.2.11:22

  # ----- 192.168.2.100 -----

  - hostname: cabin-dida.example.com
    service: http://192.168.2.100:5273

  # ----- 192.168.2.101 -----

  - hostname: cabin-baba-nats.example.com
    service: http://192.168.2.101:4223
  - hostname: cabin-baba.example.com
    service: http://192.168.2.101:5173
  - service: http_status:404
"""


def _hosts(text):
    return [ln.split(":", 1)[1].strip() for ln in text.splitlines()
            if ln.strip().startswith("- hostname:")]


def test_every_route_survives_a_read():
    meta, rows = parse_config(LIVE, ZONE)
    assert meta["tunnel"] == "239a26fc-cc17-4151-b212-4d7f1b616de6"
    assert meta["credentials-file"] == "/etc/cloudflared/credentials.json"
    assert {r["host"] for r in rows} == {
        "cabin-proxmox", "px-cabin", "cabin-dida", "cabin-baba-nats", "cabin-baba"}


def test_the_catch_all_is_not_a_route():
    _, rows = parse_config(LIVE, ZONE)
    assert all(r["origin"] != CATCHALL for r in rows)


def test_the_catch_all_comes_back_last():
    meta, rows = parse_config(LIVE, ZONE)
    out = render_config(meta, rows, ZONE)
    entries = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("- ")]
    assert entries[-1] == f"- service: {CATCHALL}"


def test_round_trip_keeps_the_same_hostnames():
    meta, rows = parse_config(LIVE, ZONE)
    again = render_config(meta, rows, ZONE)
    assert sorted(_hosts(again)) == sorted(_hosts(LIVE))
    # and re-reading the render yields the same rows, not a drifting subset
    _, rows2 = parse_config(again, ZONE)
    assert sorted(r["host"] for r in rows2) == sorted(r["host"] for r in rows)
    assert {r["host"]: r["origin"] for r in rows2} == {r["host"]: r["origin"] for r in rows}


def test_origin_traits_are_implied_the_same_way_twice():
    meta, rows = parse_config(LIVE, ZONE)
    out = render_config(meta, rows, ZONE)
    block = out.split("- hostname: cabin-proxmox.example.com")[1].split("- hostname:")[0]
    assert "noTLSVerify: true" in block            # implied by the https:// origin
    assert "disableChunkedEncoding: true" in block  # carried by the nochunk option


def test_a_plain_http_route_gets_no_origin_request():
    meta, rows = parse_config(LIVE, ZONE)
    out = render_config(meta, rows, ZONE)
    block = out.split("- hostname: cabin-dida.example.com")[1].split("- hostname:")[0]
    assert "originRequest" not in block


def test_adding_a_route_adds_exactly_one():
    meta, rows = parse_config(LIVE, ZONE)
    rows.append({"host": "cabin-new", "origin": "http://192.168.2.50:8080",
                 "serve": "wan", "opts": [], "section": None, "icon": None})
    out = render_config(meta, rows, ZONE)
    assert len(_hosts(out)) == len(_hosts(LIVE)) + 1
    assert "cabin-new.example.com" in _hosts(out)


def test_removing_a_route_removes_exactly_it():
    meta, rows = parse_config(LIVE, ZONE)
    kept = [r for r in rows if r["host"] != "cabin-baba-nats"]
    out = render_config(meta, kept, ZONE)
    assert "cabin-baba-nats.example.com" not in _hosts(out)
    assert len(_hosts(out)) == len(_hosts(LIVE)) - 1


def test_parsed_rows_pass_the_shared_validator():
    """Tunnel rows go through the same CRUD as source rows, so they have to satisfy
    the same rules — a bare host under the zone, a scheme on the origin."""
    _, rows = parse_config(LIVE, ZONE)
    for row in rows:
        validate_row(row)


def test_an_ssh_route_stays_wan():
    _, rows = parse_config(LIVE, ZONE)
    ssh = next(r for r in rows if r["origin"].startswith("ssh://"))
    assert ssh["serve"] == "wan"


def test_routes_group_by_the_host_they_expose():
    meta, rows = parse_config(LIVE, ZONE)
    out = render_config(meta, rows, ZONE)
    assert "# ----- 192.168.2.11 -----" in out
    assert out.index("# ----- 192.168.2.11 -----") < out.index("# ----- 192.168.2.100 -----")


def test_an_empty_ingress_still_renders_a_valid_file():
    out = render_config({"tunnel": "t", "credentials-file": "/c.json"}, [], ZONE)
    assert f"- service: {CATCHALL}" in out
    assert parse_config(out, ZONE)[1] == []


@pytest.mark.parametrize("text", ["", "tunnel: t\ncredentials-file: /c.json\n"])
def test_a_file_without_ingress_reads_as_no_routes(text):
    assert parse_config(text, ZONE)[1] == []


# --- provisioning: the pure rules a new installation's tunnel is minted under -----


def test_a_tunnel_name_must_be_a_dns_label():
    from dida_adapter_cloudflare.provision import validate_tunnel_name

    assert validate_tunnel_name("dida-kuca") is None
    assert validate_tunnel_name("a") is None
    for bad in ("", "Kuca", "kuca kuca", "-kuca", "kuca-", "kuća", "a" * 64):
        assert validate_tunnel_name(bad) is not None, bad


def test_the_tunnel_secret_is_32_random_base64_bytes():
    import base64

    from dida_adapter_cloudflare.provision import new_tunnel_secret

    s1, s2 = new_tunnel_secret(), new_tunnel_secret()
    assert len(base64.b64decode(s1)) == 32
    assert s1 != s2, "a fixed secret would share one key across installations"


def test_the_credentials_file_is_exactly_what_cloudflared_expects():
    import json

    from dida_adapter_cloudflare.provision import credentials_json

    body = json.loads(credentials_json("acct1", "tun1", "sec1"))
    assert body == {"AccountTag": "acct1", "TunnelSecret": "sec1", "TunnelID": "tun1"}


async def test_create_refuses_over_an_existing_ingress(tmp_path):
    """One tunnel per installation: minting a second over a live config would
    orphan the first's credentials and break the running connectors."""
    from dida_adapter_cloudflare.adapter import CloudflareAdapter

    a = CloudflareAdapter()
    cfg = tmp_path / "config.yml"
    cfg.write_text("tunnel: t1\ncredentials-file: /x.json\ningress:\n  - service: http_status:404\n")
    a._config = str(cfg)
    a._source = str(tmp_path / "absent.conf")
    a._token = "tok"
    result = await a._act_tunnel_create("dida-kuca")
    assert "already has an ingress" in result["error"]


async def test_create_refuses_without_the_owners_token(tmp_path):
    from dida_adapter_cloudflare.adapter import CloudflareAdapter

    a = CloudflareAdapter()
    a._config = str(tmp_path / "config.yml")
    a._source = str(tmp_path / "absent.conf")
    a._token = ""
    result = await a._act_tunnel_create("dida-kuca")
    assert "token" in result["error"]
