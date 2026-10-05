"""History, logs and energy — everything that reads the ClickHouse firehoses.

Lifted out of app.py as one contiguous block. These eleven routes share a shape
(read-only queries over the three append-only stores, all bounded by MAX_HOURS)
and share nothing with the auth/command/assistant routes they used to sit
between.

The router is included AT THE POINT the routes used to occupy, not with the other
includes at the top of app.py: FastAPI matches in registration order, so keeping
the position keeps the order provably identical. That is checked, not assumed —
the route table is dumped before and after any move to this file.

Behaviour is unchanged; this is a move, not a rewrite.
"""

from __future__ import annotations

import logging

from dida_core import house_timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, can_see_page, current_user, require_admin
from dida_api.energy import (
    ROLES,
    list_meters,
    load_roles,
    query_energy,
    query_energy_hourly,
    save_roles,
)
from dida_api.history import MAX_HOURS, last_nonempty, query_series, query_sparklines
from dida_api.replay import MAX_ENTITIES, build_replay
from dida_api.visibility import can_view_entity, hidden_for

log = logging.getLogger("dida.api.history_routes")

# Ordered low -> high; the /logs level filter takes everything from the named
# level upward. Matches Python's own levels, which is what the handler stores.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

router = APIRouter(tags=["history"])


@router.get("/history")
async def history(request: Request, entity_id: str,
    capability: str | None = None,
    capabilities: str | None = None,
    hours: int = 24,
    user: AuthUser = Depends(current_user),
) -> dict:
    """Multi-series history for one entity from the ClickHouse firehose (+ rollups).

    Pass `capabilities=a,b,c` (or a single `capability=`). The tier (raw / hourly /
    daily) is chosen by range; each numeric series carries bucketed
    {ts, v, min, max, last}, an enum/text series carries {ts, s} change points."""
    # View-hidden OR out of the user's page scope → 404 (don't confirm it exists).
    # Closes history enumeration for a narrow-scoped login, matching /state.
    if not await can_view_entity(request.app.state.pool, user, entity_id):
        raise HTTPException(404, "entity not found")
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    hours = max(1, min(hours, MAX_HOURS))
    caps = [c.strip() for c in (capabilities.split(",") if capabilities else []) if c.strip()]
    if capability and capability.strip():
        caps.append(capability.strip())
    caps = list(dict.fromkeys(caps))  # dedupe, preserve order
    if not caps:
        raise HTTPException(400, "capability or capabilities required")
    tz = (await house_timezone(request.app.state.pool)).key
    series = [await query_series(ch, entity_id, c, hours, tz) for c in caps]
    return {"entity_id": entity_id, "hours": hours, "series": series}


class SparklineIn(BaseModel):
    # [[entity_id, capability], …] — POST, not a query string: a page full of cards
    # asks for dozens of pairs at once and that doesn't fit a URL.
    pairs: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    hours: int = Field(default=24, ge=1, le=24 * 7)


@router.post("/history/sparklines")
async def history_sparklines(request: Request, body: SparklineIn, user: AuthUser = Depends(current_user)) -> dict:
    """Thumbnail series for MANY entities in one round trip — what the device cards
    need to show the shape of the last day without a request each.

    Hidden entities are dropped silently rather than 404-ing the whole batch: the
    caller is a page rendering whatever it can see, and one fenced-off device must
    not blank every sparkline on it."""
    ch = request.app.state.ch
    if ch is None:
        return {}
    hidden = await hidden_for(request.app.state.pool, user)
    pairs = [(e, c) for e, c in body.pairs if e not in hidden]
    return await query_sparklines(ch, pairs, body.hours)


class ReplayIn(BaseModel):
    frm: int = Field(..., description="window start, epoch ms")
    to: int = Field(..., description="window end, epoch ms")
    entities: list[str] = Field(..., min_length=1)


@router.post("/history/replay")
async def history_replay(request: Request, body: ReplayIn, user: AuthUser = Depends(current_user)) -> dict:
    """Everything the given entities held across a past window, in one bundle.

    The caller passes the entities it RENDERS — the floor plan already knows which
    those are, and deriving that set here a second time would make two sources of
    truth for what is on the plan. Same view gate as /state: an entity the login
    can't see is dropped from the set rather than refused, so a narrow-scoped user
    still gets a replay of their own surface."""
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    hidden = await hidden_for(request.app.state.pool, user)
    ids = [e for e in dict.fromkeys(body.entities) if e not in hidden][:MAX_ENTITIES]
    if not ids:
        raise HTTPException(404, "no replayable entities")
    return await build_replay(ch, ids, body.frm, body.to)


@router.get("/history/last-nonempty")
async def history_last_nonempty(request: Request, entity_id: str,
    capability: str,
    user: AuthUser = Depends(current_user),
) -> dict:
    """The last value this capability held, and when it stopped holding it.

    For a reading whose empty state is itself uninformative — a parking place with
    no car in it — so the surface can show the memory ("Ana's Car, three hours
    ago") greyed out instead of a dash. Same view gate as /history."""
    if not await can_view_entity(request.app.state.pool, user, entity_id):
        raise HTTPException(404, "entity not found")
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    return await last_nonempty(ch, entity_id, capability)


@router.get("/history/commands")
async def history_commands(request: Request, entity_id: str | None = None,
    source: str | None = None,
    hours: int = 24,
    limit: int = 500,
    _admin: AuthUser = Depends(require_admin),
) -> dict:
    """The command audit trail (ClickHouse command_history): WHO told WHICH entity
    to do WHAT, newest first. `source` filters by substring (a username, an
    automation name, "matter"). Admin-only — sources expose other users' actions."""
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    hours = max(1, min(hours, MAX_HOURS))
    limit = max(1, min(limit, 2000))
    where = ["ts > now() - toIntervalHour({hours:UInt32})"]
    params: dict = {"hours": hours, "limit": limit}
    if entity_id and entity_id.strip():
        where.append("entity_id = {eid:String}")
        params["eid"] = entity_id.strip()
    if source and source.strip():
        where.append("positionCaseInsensitive(source, {src:String}) > 0")
        params["src"] = source.strip()
    res = await ch.query(
        "SELECT toUnixTimestamp64Milli(ts) AS ms, entity_id, capability, command, source, args "  # noqa: S608
        f"FROM command_history WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT {limit:UInt32}",
        parameters=params,
    )
    return {"commands": [
        {"ts": int(ms), "entity_id": eid, "capability": cap, "command": cmd,
         "source": src, "args": args}
        for ms, eid, cap, cmd, src, args in res.result_rows
    ]}


@router.get("/logs")
async def app_logs(request: Request, service: str | None = None,
    level: str | None = None,
    entity_id: str | None = None,
    q: str | None = None,
    hours: int = 6,
    limit: int = 500,
    _admin: AuthUser = Depends(require_admin),
) -> dict:
    """Application logs (ClickHouse app_logs), newest first.

    Admin-only, and not a close call: log lines quote hostnames, tokens' failure
    messages, file paths and whatever an adapter chose to print. `service` and
    `level` are exact; `q` is a case-insensitive substring over the message, which
    is what someone actually reaches for at 03:14.

    `level` filters at-or-above the named level rather than exactly it — asking
    for WARNING and not being shown the ERROR beside it is never what was meant.
    """
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "log store unavailable")
    hours = max(1, min(hours, MAX_HOURS))
    limit = max(1, min(limit, 2000))
    where = ["ts > now() - toIntervalHour({hours:UInt32})"]
    params: dict = {"hours": hours, "limit": limit}
    if service and service.strip():
        where.append("service = {svc:String}")
        params["svc"] = service.strip()
    if entity_id and entity_id.strip():
        where.append("entity_id = {eid:String}")
        params["eid"] = entity_id.strip()
    if q and q.strip():
        where.append("positionCaseInsensitive(message, {q:String}) > 0")
        params["q"] = q.strip()
    if level and level.strip().upper() in LOG_LEVELS:
        # At-or-above, not exactly: asking for WARNING and not being shown the
        # ERROR next to it is never what was meant.
        want = LOG_LEVELS[LOG_LEVELS.index(level.strip().upper()):]
        where.append("level IN {levels:Array(String)}")
        params["levels"] = list(want)
    res = await ch.query(
        "SELECT toUnixTimestamp64Milli(ts) AS ms, service, level, logger, entity_id, message, exc "  # noqa: S608
        f"FROM app_logs WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT {limit:UInt32}",
        parameters=params,
    )
    return {"logs": [
        {"ts": int(ms), "service": svc, "level": lvl, "logger": lg,
         "entity_id": eid, "message": msg, "exc": exc}
        for ms, svc, lvl, lg, eid, msg, exc in res.result_rows
    ]}


@router.get("/logs/services")
async def app_log_services(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Which services have logged in the last day — the viewer's filter options,
    read from the data rather than hardcoded, so a new adapter appears by itself."""
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "log store unavailable")
    res = await ch.query(
        "SELECT service, count() AS n FROM app_logs "
        "WHERE ts > now() - toIntervalHour(24) GROUP BY service ORDER BY service")
    return {"services": [{"service": s_, "count": int(n)} for s_, n in res.result_rows]}


@router.get("/history/energy")
async def history_energy(request: Request, days: int = 30, _user: AuthUser = Depends(current_user)) -> dict:
    """Cumulative house-energy balance (consumption / production / self-sufficiency /
    grid import-export) per day over the last `days` days — the History dashboard."""
    await _require_energy_access(request.app.state.pool, _user, aggregate=True)
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    pool = request.app.state.pool
    return await query_energy(ch, days, await load_roles(pool), await house_timezone(pool))


@router.get("/history/energy/hourly")
async def history_energy_hourly(request: Request, frm: int, to: int, _user: AuthUser = Depends(current_user)) -> dict:
    """Per-hour energy balance for one day (unix-second [frm, to) window, client-side
    local-day bounds) — consumption up / production down on an hourly axis."""
    await _require_energy_access(request.app.state.pool, _user, aggregate=True)
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    if to <= frm or to - frm > 40 * 3600:  # a day (+ DST slack); never an open range
        raise HTTPException(400, "invalid window")
    roles = await load_roles(request.app.state.pool)
    return await query_energy_hourly(ch, roles, frm, to)


@router.get("/history/energy/config")
async def energy_config_get(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """Every energy-metering entity + its assigned/suggested role, for the config UI."""
    hidden = await _require_energy_access(request.app.state.pool, _user)
    ch = request.app.state.ch
    if ch is None:
        raise HTTPException(503, "history store unavailable")
    pool = request.app.state.pool
    meters = await list_meters(ch, pool, await load_roles(pool))
    return {"meters": [m for m in meters if m["entity_id"] not in hidden], "roles": list(ROLES)}


async def _require_energy_access(pool, user: AuthUser, *, aggregate: bool = False) -> set[str]:
    if not can_see_page(user, "history"):
        raise HTTPException(403, "history access required")
    hidden = await hidden_for(pool, user)
    if aggregate and hidden and await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM entities WHERE entity_id = ANY($1::text[]) "
        "AND (capabilities ? 'energy' OR capabilities ? 'power'))", list(hidden)
    ):
        raise HTTPException(403, "house energy balance requires access to all meters")
    return hidden


class EnergyConfigIn(BaseModel):
    roles: dict[str, str]


@router.put("/history/energy/config", status_code=204)
async def energy_config_set(request: Request, body: EnergyConfigIn, _admin: AuthUser = Depends(require_admin)) -> None:
    """Persist entity→role assignments (Settings-grade; admin only)."""
    await save_roles(request.app.state.pool, body.roles)


# --- command (act on a device — validated, published to owning adapter) --
