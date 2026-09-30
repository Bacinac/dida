"""Radio tuner — a synthetic controller so automations (a remote's prev/next,
a schedule, …) can cycle the stations on a player.

The stations are OPUS's: the api asks the player for them (cached a minute) and
this only resolves the neighbouring one and pushes a `play_media` command, exactly
like the media page does. Entity `radio:tuner` has no adapter — the API handles
its commands directly. Target player and current station persist in
`app_settings`, so a restart (and the next prev/next) resume from where the
listener left off.

The same list is answered on the bus (`dida.radio.stations`) for the adapters
that need to know whether a box is playing one of them; they hold no table.
"""

from __future__ import annotations

import json
import logging

from dida_core import Command, app_setting, prepare_command, set_app_setting
from dida_core.media import STATIONS_SUBJECT

from dida_api import opus

log = logging.getLogger("dida.api.radio_tuner")

TUNER = "radio:tuner"


async def resolve_target(pool) -> str | None:
    """The player `radio:tuner` will drive — the configured `radio_player`, or None
    when there is none. Exposed so the /command boundary can control-check the REAL target
    before a relay: radio:tuner is synthetic (no registry row), so its own
    is_hidden/require_control are permissive and would otherwise let a user who is
    control-denied on the actual player start it through the tuner (privilege
    escalation). Mirrors scenes.recall re-running the per-target boundary."""
    return (await app_setting(pool, "radio_player")) or None


async def _set_current(pool, station_id: int) -> None:
    await set_app_setting(pool, "radio_current_id", str(station_id))


def _pick_index(stations, command: str, cur_id, station_sel) -> int | None:
    """Which station index to play, or None to do nothing (pause/stop/unknown command).

    An explicit `station_sel` (station name, case-insensitive, or its id) PINS that
    station — so a scene/automation plays a specific one, not just whatever's current
    (falls back to current-or-first if the name/id matches nothing). Otherwise
    next/previous cycle from the current, and play/(re)play stays on current-or-first.
    Assumes a non-empty `stations` list (the caller returns early when there are none)."""
    n = len(stations)
    idx = next((i for i, s in enumerate(stations) if s["id"] == cur_id), -1)
    if station_sel is not None:
        key = str(station_sel).strip().lower()
        return next(
            (i for i, s in enumerate(stations) if str(s["name"]).lower() == key or str(s["id"]) == key),
            max(idx, 0),
        )
    if command == "next":
        return 0 if idx < 0 else (idx + 1) % n
    if command == "previous":
        return n - 1 if idx < 0 else (idx - 1) % n
    if command in ("play", "play_media", "play_index"):
        return max(idx, 0)  # (re)play the current station, or the first
    return None


async def start(app) -> None:
    """Subscribe to the bus: handle `radio:tuner` media_transport commands, and
    answer the station list to whoever asks."""
    bus = app.state.bus
    pool = app.state.pool

    async def on_command(cmd: Command) -> None:
        if cmd.entity_id != TUNER:
            return
        try:
            stations = await opus.stations(pool)
        except opus.OpusUnavailable as exc:
            log.error("radio_tuner: %s but no stations — %s", cmd.command, exc)
            return
        if not stations:
            log.warning("radio_tuner: %s but OPUS lists no stations", cmd.command)
            return
        cur = await app_setting(pool, "radio_current_id")
        try:
            cur_id = int(cur) if cur is not None else None
        except ValueError:
            cur_id = None
        idx = _pick_index(stations, cmd.command, cur_id, cmd.args.get("station"))
        if idx is None:
            return  # pause/stop/… — nothing to play

        st = stations[idx]
        target = await resolve_target(pool)
        if target is None:
            log.error("radio_tuner: %s from %s refused — no radio_player is configured",
                      cmd.command, cmd.source)
            return
        # A relay PROPAGATES the source it received (the initiator — a user, an
        # automation, the IKEA remote's rule) so the audit trail keeps who started it.
        await bus.publish_command(await prepare_command(
            pool, target, "media_transport", "play_media",
            {"uri": st["url"], "title": st["name"], "art": st.get("logo") or ""},
            source=cmd.source))
        await _set_current(pool, st["id"])
        log.info("radio_tuner: %s → %r on %s", cmd.command, st["name"], target)

    async def on_stations(msg) -> None:
        try:
            rows = await opus.stations(pool)
        except opus.OpusUnavailable as exc:
            log.warning("stations asked on the bus, none to give: %s", exc)
            rows = []
        await msg.respond(json.dumps(rows).encode())

    await bus.subscribe_commands(on_command, TUNER.split(":", 1)[0])
    await bus.nc.subscribe(STATIONS_SUBJECT, cb=on_stations)
    target = await resolve_target(pool)
    if target is None:
        log.warning("radio_tuner ready — no radio_player is configured, %s refuses to play", TUNER)
    else:
        log.info("radio_tuner ready — %s cycles the OPUS stations onto %s", TUNER, target)
