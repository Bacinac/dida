"""System observability — the live health/throughput surface the PROPOSAL promised
and the System page was a redirect stub for.

`/system/stats` (JSON, for the System page) aggregates the engine's throughput
counters + JetStream backlog + history-buffer health (request/reply on
`dida.engine.stats`), every adapter's fail-loud status badge, and Postgres/
ClickHouse reachability. `/metrics` re-emits the same numbers in Prometheus text
format so an external scraper/alerting stack can watch them — disabled by default,
enabled only when `DIDA_METRICS_TOKEN` is set (secure-by-default; no accidental
public exposure behind a tunnel).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

from dida_core import ADAPTER_CONFIG
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from dida_api.adapters import _adapter_status
from dida_api.auth import AuthUser, current_user

log = logging.getLogger("dida.api.system")

router = APIRouter(tags=["system"])

_STATES = ("ok", "connecting", "idle", "error", "offline")  # offline = adapter not answering


async def _gather(app) -> dict:
    """The live health snapshot behind /system/stats — engine throughput + JetStream
    backlog + history buffer, every adapter's badge, and datastore reachability.
    Takes the app (not a Request) so the alert evaluator can reuse the exact snapshot."""
    bus = app.state.bus
    pool = app.state.pool
    ch = getattr(app.state, "ch", None)

    # Engine throughput + backlog + history buffer (request/reply; None if silent).
    engine: dict | None = None
    try:
        resp = await bus.nc.request("dida.engine.stats", b"", timeout=1.5)
        engine = json.loads(resp.data)
    except Exception:
        log.debug("system: engine stats not answered", exc_info=True)
        engine = None

    # Whether each adapter is SUPPOSED to run, from the runner's profile state.
    # An installation only enables a subset (Cabin runs no tuya/unifi/volumio);
    # a deliberately switched-off adapter is silent by choice, not broken — the
    # distinction that keeps adapter_offline from crying wolf on every install.
    # Runner silent → empty map → every adapter defaults to enabled (fail loud).
    enabled_by: dict = {}
    try:
        resp = await bus.nc.request("dida.runner.ctl", b'{"action": "state"}', timeout=1.5)
        profiles = json.loads(resp.data).get("profiles") or {}
        enabled_by = {n: bool(p.get("enabled")) for n, p in profiles.items() if isinstance(p, dict)}
    except Exception:
        log.debug("system: runner state not answered", exc_info=True)
        enabled_by = {}

    # Every adapter's live badge, concurrently (short timeout, fail-loud).
    names = sorted(ADAPTER_CONFIG)
    raw = await asyncio.gather(*(_adapter_status(app, n) for n in names))
    adapters: list[dict] = []
    counts = dict.fromkeys(_STATES, 0)
    for name, st in zip(names, raw, strict=True):
        state = (st or {}).get("state") or "offline"
        counts[state if state in counts else "offline"] += 1
        adapters.append({"name": name, "state": state, "detail": (st or {}).get("detail", ""),
                         "enabled": enabled_by.get(name, True)})

    # Datastore reachability.
    pg_ok = False
    try:
        pg_ok = (await pool.fetchval("SELECT 1")) == 1
    except Exception:
        log.debug("system: postgres probe failed", exc_info=True)
        pg_ok = False
    ch_ok = False
    if ch is not None:
        try:
            await ch.query("SELECT 1")
            ch_ok = True
        except Exception:
            log.debug("system: clickhouse probe failed", exc_info=True)
            ch_ok = False

    return {
        "engine": engine,
        "adapters": adapters,
        "adapter_counts": counts,
        "db": {"postgres": pg_ok, "clickhouse": ch_ok},
    }


@router.get("/system/stats")
async def system_stats(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """Live system health for the System page (any signed-in user; read-only ops data)."""
    return await _gather(request.app)


def _prom(stats: dict) -> str:
    lines: list[str] = []

    def metric(name: str, value, help_: str, typ: str = "gauge", labels: str = "") -> None:
        if not any(ln.startswith(f"# TYPE {name} ") for ln in lines):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {typ}")
        lines.append(f"{name}{'{' + labels + '}' if labels else ''} {value}")

    eng = stats.get("engine")
    metric("dida_engine_up", 1 if eng else 0, "Engine answered the stats request.")
    if eng:
        metric("dida_engine_accepted_total", eng.get("accepted", 0), "State updates validated + projected.", "counter")
        metric("dida_engine_rejected_total", eng.get("rejected", 0), "Updates rejected at the capability boundary.", "counter")
        metric("dida_engine_stale_total", eng.get("stale", 0), "Out-of-order / duplicate updates dropped.", "counter")
        metric("dida_engine_backlog", eng.get("backlog", -1), "Pending JetStream messages on the state consumer.")
        metric("dida_engine_uptime_seconds", eng.get("uptime_s", 0), "Engine process uptime.")
        h = eng.get("history") or {}
        metric("dida_history_buffered", h.get("buffered", 0), "Rows queued for ClickHouse.")
        metric("dida_history_dropped_total", h.get("dropped", 0), "History rows dropped (ClickHouse down, buffer full).", "counter")
        metric("dida_history_connected", 1 if h.get("connected") else 0, "History writer has a live ClickHouse client.")

    for state, n in stats.get("adapter_counts", {}).items():
        metric("dida_adapters", n, "Adapters by connection state.", "gauge", f'state="{state}"')
    for db, up in stats.get("db", {}).items():
        metric("dida_datastore_up", 1 if up else 0, "Datastore reachable.", "gauge", f'db="{db}"')

    return "\n".join(lines) + "\n"


@router.get("/metrics")
async def metrics(request: Request, token: str = "", authorization: str = Header("")) -> Response:
    """Prometheus exposition. Disabled unless DIDA_METRICS_TOKEN is set; then requires
    it as `?token=` or `Authorization: Bearer <token>` (secure-by-default)."""
    want = os.environ.get("DIDA_METRICS_TOKEN", "").strip()
    if not want:
        raise HTTPException(404, "metrics disabled — set DIDA_METRICS_TOKEN to enable scraping")
    got = token or authorization.removeprefix("Bearer ").strip()
    if not (got and hmac.compare_digest(got, want)):
        raise HTTPException(401, "invalid metrics token")
    return Response(_prom(await _gather(request.app)), media_type="text/plain; version=0.0.4; charset=utf-8")
