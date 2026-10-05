"""A light switched on by hand holds against automations while its room is occupied.

"By hand" is anything the automation engine did not ask for: the UI, the app, a
voice assistant, a scene, a wall switch. While the flag stands, no rule may turn
that light off. It ends when the light goes off by any means, or once the light's
area has been empty for RELEASE_AFTER_S; from then on rules act on it as before.

An area with no presence sensor can never be seen empty, so a light there is never
held: the rules keep it.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import asyncpg
from dida_core import Command, emit_journal

log = logging.getLogger("dida.automation.manual")

PRESENCE_CAPS = ("occupancy", "motion")
# A report of "on" this soon after a rule commanded the light is that rule's doing.
AUTOMATION_WINDOW_S = 20.0
# Presence sensors flicker (an FP2 zone hand-off, a still sitter); the room has to
# stay empty this long before the hold lets go.
RELEASE_AFTER_S = 60.0


class ManualOverrides:
    def __init__(self, bus, pool: asyncpg.Pool) -> None:
        self._bus = bus
        self._pool = pool
        self._light_area: dict[str, int] = {}
        self._presence: dict[int, list[tuple[str, str]]] = {}
        self._held: dict[str, datetime] = {}
        self._commanded: dict[str, float] = {}
        self._empty_since: dict[int, float] = {}

    @property
    def held(self) -> dict[str, datetime]:
        return self._held

    async def load_held(self) -> None:
        rows = await self._pool.fetch("SELECT entity_id, since FROM manual_overrides")
        self._held = {r["entity_id"]: r["since"] for r in rows}

    async def refresh(self) -> None:
        rows = await self._pool.fetch(
            "SELECT entity_id, area_id, device_type, capabilities, hidden_caps FROM entities "
            "WHERE area_id IS NOT NULL AND NOT diagnostic"
        )
        presence: dict[int, list[tuple[str, str]]] = {}
        lights: dict[str, int] = {}
        for r in rows:
            caps, hidden = r["capabilities"] or [], r["hidden_caps"] or []
            for cap in PRESENCE_CAPS:
                if cap in caps and cap not in hidden:
                    presence.setdefault(r["area_id"], []).append((r["entity_id"], cap))
            if r["device_type"] == "light" and "on_off" in caps:
                lights[r["entity_id"]] = r["area_id"]
        self._empty_since = {area: since for area, since in self._empty_since.items()
                             if presence.get(area) == self._presence.get(area)}
        self._presence = presence
        self._light_area = {e: a for e, a in lights.items() if a in presence}

    def _occupied(self, area: int, snapshot: dict) -> bool | None:
        values = [snapshot.get(key) for key in self._presence.get(area, ())]
        if any(value is True for value in values):
            return True
        if values and all(value is False for value in values):
            return False
        return None

    def note_command(self, cmd: Command) -> None:
        if cmd.entity_id in self._light_area:
            self._commanded[cmd.entity_id] = time.monotonic()

    def holds(self, cmd: Command, snapshot: dict) -> bool:
        """Whether this rule command would turn off a light held by hand."""
        if cmd.capability != "on_off" or cmd.entity_id not in self._held:
            return False
        return cmd.command == "turn_off" or (
            cmd.command == "toggle" and snapshot.get((cmd.entity_id, "on_off")) is True)

    async def on_state(self, entity_id: str, capability: str, prev: object, value: object,
                       fresh: bool) -> None:
        now = time.monotonic()
        if capability in PRESENCE_CAPS and value is True:
            for area, keys in self._presence.items():
                if (entity_id, capability) in keys:
                    self._empty_since.pop(area, None)
        if capability != "on_off" or entity_id not in self._light_area:
            return
        if value is False and entity_id in self._held:
            await self._release(entity_id, "turned off")
        elif (value is True and prev is False and fresh and entity_id not in self._held
              and now - self._commanded.get(entity_id, float("-inf")) > AUTOMATION_WINDOW_S):
            await self._hold(entity_id)

    async def sweep(self, snapshot: dict) -> None:
        now = time.monotonic()
        for entity_id in list(self._held):
            area = self._light_area.get(entity_id)
            if area is None:
                await self._release(entity_id, "no longer a light in a room with presence")
                continue
            if snapshot.get((entity_id, "on_off")) is False:
                await self._release(entity_id, "turned off")
                continue
            if self._occupied(area, snapshot) is not False:
                self._empty_since.pop(area, None)
                continue
            since = self._empty_since.setdefault(area, now)
            if now - since >= RELEASE_AFTER_S:
                await self._release(entity_id, "room empty")

    async def _hold(self, entity_id: str) -> None:
        since = datetime.now(UTC)
        await self._pool.execute(
            "INSERT INTO manual_overrides (entity_id, since) VALUES ($1, $2) "
            "ON CONFLICT (entity_id) DO NOTHING", entity_id, since)
        self._held[entity_id] = since
        self._empty_since.pop(self._light_area[entity_id], None)
        log.info("manual: %s switched on by hand — rules may not turn it off while its room is occupied",
                 entity_id)
        await emit_journal(self._bus, "manual_override", entity_id=entity_id, source="automation",
                           message="switched on by hand — held on while the room is occupied")

    async def _release(self, entity_id: str, why: str) -> None:
        await self._pool.execute("DELETE FROM manual_overrides WHERE entity_id = $1", entity_id)
        self._held.pop(entity_id, None)
        log.info("manual: %s released (%s)", entity_id, why)
        await emit_journal(self._bus, "manual_released", entity_id=entity_id, source="automation",
                           message=why)
