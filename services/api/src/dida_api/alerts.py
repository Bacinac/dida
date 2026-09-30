"""System-health alerting — the rule-driven layer on top of the metrics surface.

An in-process evaluator (spawned in the API lifespan) polls the same health
snapshot `/system/stats` exposes — engine backlog, history buffer drops, adapter
offline count — plus two the API reads directly (automations auto-disabled by
their failure breaker, in Postgres; state-disk free space, via statvfs), evaluates
the seeded threshold rules with a per-rule anti-flap hold, and on a rule crossing
into alarm:

  - records a `fired` row in ClickHouse alert_history,
  - notifies the chosen recipients (critical severity only) unless that alert is
    silenced,
  - holds the alert active (in memory) so /system/alerts shows it live.

When the condition clears it records `resolved` and notifies again. Rules are
seeded (migration 0028) and tunable by an admin via /system/alert-rules;
recipients live in app_settings `alert_recipients` (/system/alert-recipients),
silences in `alert_silences` (/system/alerts/silence).

Deliberately NOT device automations: system health is not a device capability, so
routing it through the canonical entity model would pollute the capability
vocabulary and clutter the device pickers. This is a purpose-built ops watchdog
that reuses the one notify delivery path (bus → notify adapter → Web Push). It
covers IN-BAND degradation (a peripheral fails while the core is up); whole-service
outages (engine/nats/postgres down) stay with the external liveness monitor —
belt and suspenders, same split as the CI/local-gate story.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import asyncpg
from dida_core import app_setting, prepare_command, set_app_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.backup import _BACKUP_DIR
from dida_api.system import _gather

log = logging.getLogger("dida.api.alerts")

router = APIRouter(tags=["alerts"])

POLL_S = 30  # evaluation cadence; hold_s thresholds are wall-clock, independent of this
STATE_PATH = os.environ.get("DIDA_STATE_PATH", "/state")  # its filesystem = the pg/ch/nats disk

_ROW = ["ts", "key", "scope", "severity", "event", "message", "value"]
RECIPIENTS_KEY = "alert_recipients"
NOTIFY_CTL = "dida.notify.ctl"


async def alert_recipients(pool) -> list[str]:
    raw = await app_setting(pool, RECIPIENTS_KEY)
    return [str(r) for r in json.loads(raw)] if raw else []


async def notify_routes(app) -> list[str] | None:
    """The notify targets that can actually reach a device right now, or None when
    the notify adapter does not answer."""
    try:
        resp = await app.state.bus.nc.request(NOTIFY_CTL, b'{"action": "routes"}', timeout=3)
        return [str(r) for r in json.loads(resp.data)["routes"]]
    except Exception:
        log.debug("notify routes not answered", exc_info=True)
        return None


def _newest_backup_age_h() -> float | None:
    """Hours since the newest backup was written, or None when there is not one.

    Read from the directory rather than from the scheduler's own record: a job
    that believes it ran and a file that exists are different claims, and it was
    the second one that stopped being true."""
    newest = 0.0
    try:
        with os.scandir(_BACKUP_DIR) as entries:
            for entry in entries:
                if entry.is_file() and (entry.name.endswith(".dump")
                                        or entry.name.endswith(".tar.gz")):
                    newest = max(newest, entry.stat().st_mtime)
    except OSError:
        return None
    if not newest:
        return None
    return max(0.0, (time.time() - newest) / 3600.0)


def _disk_free_pct(path: str) -> float | None:
    """Percent of the filesystem at `path` that is free, or None if it can't be
    stat'd. In the API container /state is a subdir of DIDA_STATE_HOST, the one
    tier that also backs postgres/clickhouse/nats — so this tracks the disk that
    actually fills."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    if st.f_blocks == 0:
        return None
    return 100.0 * st.f_bavail / st.f_blocks


class Evaluator:
    """Holds the anti-flap hold timers, the active-alert set, and the buffer-drop
    edge baseline across ticks. One instance per API process, on app.state."""

    def __init__(self, app) -> None:
        self.app = app
        self._pending: dict[tuple[str, str], float] = {}   # (key, scope) -> first monotonic time seen active
        self._active: dict[tuple[str, str], dict] = {}      # (key, scope) -> live alert record
        self._last_dropped: int | None = None               # baseline for the buffer_drop edge
        self._silences: dict[tuple[str, str], datetime | None] = {}  # (key, scope) -> until (None = until resolved)

    async def _rules(self) -> dict[str, dict]:
        rows = await self.app.state.pool.fetch(
            "SELECT key, severity, threshold, hold_s FROM alert_rules WHERE enabled"
        )
        return {r["key"]: dict(r) for r in rows}

    async def _conditions(self, rules: dict[str, dict]) -> list[tuple[str, str, bool, float, str]]:
        """Evaluate every enabled rule against a fresh health snapshot. Returns one
        (key, scope, active, value, message) per evaluable (rule, scope) pair."""
        out: list[tuple[str, str, bool, float, str]] = []
        health = await _gather(self.app)
        eng = health.get("engine") or {}

        if "consumer_lag" in rules:
            backlog = eng.get("backlog", -1)
            thr = rules["consumer_lag"]["threshold"] or 1000
            # backlog == -1 → engine silent (its own out-of-band concern, watched
            # externally); don't fire a lag alert on a down engine.
            active = backlog >= 0 and backlog > thr
            out.append(("consumer_lag", "", active, float(max(backlog, 0)),
                        f"JetStream zaostatak: {max(backlog, 0)} poruka u redu"))

        if "buffer_drop" in rules:
            hist = eng.get("history")
            if hist is None:
                # Engine silent this tick (the dida.engine.stats request timed out) —
                # `dropped` is unknowable, so a timed-out poll must NOT corrupt the
                # baseline or page. Same silent-engine guard consumer_lag has (a real
                # false-critical bug lived here without it). Report inactive, hold the
                # baseline so the next real reading compares against the true prior count.
                out.append(("buffer_drop", "", False, float(self._last_dropped or 0),
                            "History buffer u redu"))
            else:
                dropped = int(hist.get("dropped", 0))
                # First observation just sets the baseline — never fire on startup.
                active = False if self._last_dropped is None else dropped > self._last_dropped
                self._last_dropped = dropped
                out.append(("buffer_drop", "", active, float(dropped),
                            f"History buffer ispušta zapise (ukupno {dropped}) — ClickHouse nedostupan ili pun"))

        if "adapter_offline" in rules:
            # `error` counts as much as `offline`: an adapter that answers while
            # its backend is dead is the worse failure of the two, because the
            # process looks alive and nothing it was asked to do gets done.
            for a in health.get("adapters", []):
                state = a.get("state")
                # An adapter switched off on purpose (runner profile disabled) is
                # not broken — without this, every installation screams critical
                # for each adapter it simply doesn't use. Emitted with broken=False
                # (not skipped) so a prior alert resolves the moment it's disabled.
                broken = state in ("offline", "error") and a.get("enabled", True)
                detail = a.get("detail") or ""
                msg = (f"Adapter „{a['name']}” ne odgovara" if state == "offline"
                       else f"Adapter „{a['name']}” u kvaru{f' — {detail}' if detail else ''}")
                out.append(("adapter_offline", a["name"], broken, 1.0 if broken else 0.0, msg))

        if "breaker_open" in rules:
            n = int(await self.app.state.pool.fetchval(
                "SELECT count(*) FROM automations WHERE enabled = false AND last_error IS NOT NULL"
            ))
            out.append(("breaker_open", "", n > 0, float(n),
                        f"{n} automatizacija zaustavljeno breakerom (ponovljene greške)"))

        if "device_unreachable" in rules:
            # One alert per device the adapter has declared unreachable. Scoped by
            # device_key so several down devices are several alerts, each clearing on
            # its own recovery. The verdict comes from the adapter (esphome
            # connection, z2m availability, …), never from a stale timestamp — a
            # switch quiet for hours is not unreachable, and this must not say so.
            for d in await self.app.state.pool.fetch(
                "SELECT device_key, COALESCE(label, name, device_key) AS nm "
                "FROM devices WHERE reachable = false ORDER BY device_key"
            ):
                out.append(("device_unreachable", d["device_key"], True, 1.0,
                            f"Uređaj „{d['nm']}” nedostupan"))

        if "backup_stale" in rules:
            # The one failure that stays invisible until the day it matters. A
            # reboot raced the NAS coming up, the mount timed out, `nofail` let the
            # machine carry on, and for 23 hours nothing left this box — with only
            # a log line every ten minutes to say so. Age of the newest backup is
            # the one measure that catches every way this breaks: an unmounted
            # share, a permission, a broken pg_dump, a scheduler that stopped.
            hours = _newest_backup_age_h()
            thr = rules["backup_stale"]["threshold"] or 36
            active = hours is None or hours > thr
            out.append(("backup_stale", "", active,
                        float(hours if hours is not None else -1),
                        "Nema nijednog backupa" if hours is None
                        else f"Zadnji backup star {hours:.0f} h"))

        if "disk_low" in rules:
            free = _disk_free_pct(STATE_PATH)
            thr = rules["disk_low"]["threshold"] or 15
            active = free is not None and free < thr
            out.append(("disk_low", "", active, float(free if free is not None else 0.0),
                        f"Malo prostora na disku stanja: {free:.0f}% slobodno" if free is not None
                        else "Disk stanja nedostupan"))

        if "alert_unrouted" in rules:
            wanted = await alert_recipients(self.app.state.pool)
            routes = await notify_routes(self.app)
            reached = [r for r in wanted if routes is not None and r in routes]
            if routes is None:
                msg = "Notify adapter ne odgovara — kritične uzbune ne mogu izaći"
            elif not wanted:
                msg = "Kritične uzbune nemaju odabranog primatelja"
            else:
                names = ", ".join(w.split(":", 1)[-1] for w in wanted)
                msg = f"Nijedan primatelj kritičnih uzbuna nema uređaj za obavijesti ({names})"
            out.append(("alert_unrouted", "", not reached, float(len(reached)), msg))

        return out

    async def _load_silences(self) -> None:
        pool = self.app.state.pool
        rows = await pool.fetch("SELECT key, scope, until FROM alert_silences")
        now = datetime.now(UTC)
        expired = {(r["key"], r["scope"]) for r in rows if r["until"] is not None and r["until"] <= now}
        if expired:
            await pool.execute("DELETE FROM alert_silences WHERE until IS NOT NULL AND until <= now()")
        self._silences = {(r["key"], r["scope"]): r["until"] for r in rows
                          if (r["key"], r["scope"]) not in expired}
        for k in expired:
            rec = self._active.get(k)
            if rec is not None and rec["severity"] == "critical":
                await self._notify(f"⚠️ Još traje: {rec['message']}")

    def silence(self, k: tuple[str, str], until: datetime | None) -> None:
        self._silences[k] = until

    def unsilence(self, k: tuple[str, str]) -> None:
        self._silences.pop(k, None)

    async def tick(self) -> None:
        await self._load_silences()
        rules = await self._rules()
        conditions = await self._conditions(rules)
        now = asyncio.get_running_loop().time()
        seen: set[tuple[str, str]] = set()

        for key, scope, active, value, message in conditions:
            k = (key, scope)
            seen.add(k)
            if active:
                self._pending.setdefault(k, now)
                if now - self._pending[k] >= rules[key]["hold_s"] and k not in self._active:
                    await self._fire(key, scope, rules[key]["severity"], value, message)
                elif k in self._active:
                    self._active[k]["message"] = message  # refresh live text/value while it stays active
                    self._active[k]["value"] = value
            else:
                self._pending.pop(k, None)
                if k in self._active:
                    await self._resolve(k, message)

        # A rule that became disabled, or a scope (adapter) that vanished from the
        # snapshot, leaves a stale active alert with no condition to clear it — resolve it.
        for k in list(self._active):
            if k not in seen:
                await self._resolve(k, "Uvjet više nije prisutan")

        # Same for armed-but-not-yet-fired hold timers: a scope that vanished before
        # its hold elapsed would otherwise leak a _pending entry forever.
        for k in list(self._pending):
            if k not in seen:
                self._pending.pop(k, None)

    async def _fire(self, key: str, scope: str, severity: str, value: float, message: str) -> None:
        self._active[(key, scope)] = {
            "key": key, "scope": scope, "severity": severity,
            "message": message, "value": value, "since": time.time(),
        }
        log.warning("ALERT fired %s%s [%s]: %s", key, f"/{scope}" if scope else "", severity, message)
        await self._record(key, scope, severity, "fired", message, value)
        if severity == "critical" and (key, scope) not in self._silences:
            await self._notify(f"⚠️ {message}")

    async def _resolve(self, k: tuple[str, str], message: str) -> None:
        rec = self._active.pop(k, None)
        if rec is None:
            return
        self._pending.pop(k, None)
        log.info("ALERT resolved %s%s", k[0], f"/{k[1]}" if k[1] else "")
        await self._record(k[0], k[1], rec["severity"], "resolved", message, rec.get("value", 0.0))
        if k in self._silences:
            if self._silences[k] is None:
                self._silences.pop(k)
                await self.app.state.pool.execute(
                    "DELETE FROM alert_silences WHERE key = $1 AND scope = $2 AND until IS NULL", *k
                )
        elif rec["severity"] == "critical":
            await self._notify(f"✅ Riješeno: {message}")

    async def _record(self, key: str, scope: str, severity: str, event: str, message: str, value: float) -> None:
        ch = getattr(self.app.state, "ch", None)
        if ch is None:
            return  # best-effort audit; a down ClickHouse is itself the buffer_drop alert
        try:
            await ch.insert(
                "alert_history",
                [[datetime.now(UTC), key, scope, severity, event, message[:500], float(value)]],
                column_names=_ROW,
            )
        except Exception:
            log.warning("alert_history insert failed", exc_info=True)

    async def _notify(self, message: str) -> None:
        """The chosen recipients, via the one notify path (bus → notify adapter)."""
        try:
            for target in await alert_recipients(self.app.state.pool):
                await self.app.state.bus.publish_command(await prepare_command(
                    self.app.state.pool, target, "notify", "notify",
                    {"title": "DIDA", "message": message}, source="system:alerts"))
        except Exception:
            log.warning("alert notify failed", exc_info=True)

    def active_list(self) -> list[dict]:
        out = []
        for k, rec in self._active.items():
            silenced = k in self._silences
            until = self._silences.get(k)
            out.append({**rec, "silenced": silenced,
                        "silenced_until": until.isoformat() if until is not None else None})
        return sorted(out, key=lambda a: (a["severity"] != "critical", a["since"]))


async def alert_loop(app) -> None:
    """The background evaluator — spawned in the API lifespan, cancelled at teardown."""
    ev = Evaluator(app)
    app.state.alert_evaluator = ev
    while True:
        try:
            await ev.tick()
        except Exception:
            log.exception("alert evaluation tick failed")
        await asyncio.sleep(POLL_S)


# ── HTTP surface ─────────────────────────────────────────────────────────────

def _utc_iso(value):
    """ClickHouse hands back a NAIVE datetime that is UTC. Shipped as it comes, the
    browser reads it as local time and the page dates every alert two hours early in
    summer — an ops log that disagrees with the clock on the wall is worse than none.
    Every other ClickHouse endpoint here ships epoch millis and never has the
    question; this one is a string, so it has to say which zone it is in."""
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


@router.get("/system/alerts")
async def list_alerts(request: Request, _user: AuthUser = Depends(current_user)) -> dict:
    """Currently-firing alerts (live, from the evaluator) + the recent history
    (ClickHouse). Any signed-in user — read-only ops data, same as /system/stats."""
    ev = getattr(request.app.state, "alert_evaluator", None)
    active = ev.active_list() if ev is not None else []
    history: list[dict] = []
    ch = getattr(request.app.state, "ch", None)
    if ch is not None:
        try:
            res = await ch.query(
                "SELECT ts, key, scope, severity, event, message, value "
                "FROM alert_history ORDER BY ts DESC LIMIT 100"
            )
            cols = res.column_names
            history = [
                {c: (_utc_iso(v) if hasattr(v, "isoformat") else v) for c, v in zip(cols, row, strict=True)}
                for row in res.result_rows
            ]
        except Exception:
            log.warning("alert_history query failed", exc_info=True)
    return {"active": active, "history": history}


@router.get("/system/alert-rules")
async def list_alert_rules(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Every alert rule + its current tuning (admin — these gate the ops watchdog)."""
    rows = await request.app.state.pool.fetch(
        "SELECT key, severity, threshold, hold_s, enabled FROM alert_rules ORDER BY key"
    )
    return [dict(r) for r in rows]


class AlertRulePatch(BaseModel):
    threshold: float | None = Field(default=None, ge=0)
    hold_s: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None


@router.put("/system/alert-rules/{key}", status_code=204)
async def update_alert_rule(
    key: str, body: AlertRulePatch, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    """Tune a rule's threshold / hold / enabled. Only the fields sent are changed;
    the rule set itself is fixed (seeded), so there's no create/delete."""
    sets: list[str] = []
    args: list = []
    data = body.model_dump(exclude_unset=True)
    for col in ("threshold", "hold_s", "enabled"):
        if col in data:
            args.append(data[col])
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        raise HTTPException(400, "nothing to update")
    args.append(key)
    updated = await request.app.state.pool.fetchval(
        f"UPDATE alert_rules SET {', '.join(sets)}, updated_at = now() WHERE key = ${len(args)} RETURNING key",  # noqa: S608
        *args,
    )
    if updated is None:
        raise HTTPException(404, f"no alert rule {key!r}")


class SilenceIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=100)
    scope: str = Field("", max_length=300)
    hours: float | None = Field(None, gt=0, le=24 * 30)


@router.post("/system/alerts/silence", status_code=204)
async def silence_alert(body: SilenceIn, request: Request, admin: AuthUser = Depends(require_admin)) -> None:
    """Stop one alert (rule + scope) from notifying for `hours`, or until it resolves
    when `hours` is omitted. The alert itself stays visible and recorded."""
    until = datetime.now(UTC) + timedelta(hours=body.hours) if body.hours is not None else None
    try:
        await request.app.state.pool.execute(
            "INSERT INTO alert_silences (key, scope, until, created_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (key, scope) DO UPDATE SET until = EXCLUDED.until, "
            "created_by = EXCLUDED.created_by, created_at = now()",
            body.key, body.scope, until, admin.username,
        )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(404, f"no alert rule {body.key!r}") from None
    ev = getattr(request.app.state, "alert_evaluator", None)
    if ev is not None:
        ev.silence((body.key, body.scope), until)


@router.delete("/system/alerts/silence", status_code=204)
async def unsilence_alert(
    request: Request, key: str, scope: str = "", _admin: AuthUser = Depends(require_admin)
) -> None:
    await request.app.state.pool.execute(
        "DELETE FROM alert_silences WHERE key = $1 AND scope = $2", key, scope
    )
    ev = getattr(request.app.state, "alert_evaluator", None)
    if ev is not None:
        ev.unsilence((key, scope))


@router.get("/system/alert-recipients")
async def get_alert_recipients(request: Request, _admin: AuthUser = Depends(require_admin)) -> dict:
    """Who critical alerts go to, every target that could be chosen, and which of
    those can reach a device right now (None when the notify adapter is silent)."""
    pool = request.app.state.pool
    targets = await pool.fetch(
        "SELECT entity_id, COALESCE(label, name, entity_id) AS name FROM entities "
        "WHERE adapter = 'notify' AND entity_id <> 'notify:all' ORDER BY entity_id"
    )
    return {
        "recipients": await alert_recipients(pool),
        "targets": [dict(t) for t in targets],
        "routes": await notify_routes(request.app),
    }


class RecipientsIn(BaseModel):
    recipients: list[str] = Field(..., max_length=50)


@router.put("/system/alert-recipients", status_code=204)
async def put_alert_recipients(
    body: RecipientsIn, request: Request, _admin: AuthUser = Depends(require_admin)
) -> None:
    pool = request.app.state.pool
    known = {r["entity_id"] for r in await pool.fetch(
        "SELECT entity_id FROM entities WHERE adapter = 'notify' AND entity_id <> 'notify:all'"
    )}
    unknown = sorted(set(body.recipients) - known)
    if unknown:
        raise HTTPException(400, f"unknown notify target(s): {', '.join(unknown)}")
    await set_app_setting(pool, RECIPIENTS_KEY, json.dumps(sorted(set(body.recipients))))
