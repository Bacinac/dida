from __future__ import annotations

import asyncio
import json
import logging
import time

from dida_core import (
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    validate_command,
)

log = logging.getLogger("dida.adapter.virtual")

NAMESPACE = "virtual"
RELOAD_INTERVAL = 3.0


def _initial_value(capability: str, options: list | None, default=None):
    # A seeded default (virtual_entities.default_value) wins — it lets a config helper
    # ship with a sensible value (a 23:00 quiet-hours boundary) instead of the type's
    # bare zero. Only used when the helper has no state yet; the user's later edit is
    # the source of truth from then on.
    if default is not None:
        return default
    if capability in ("boolean", "on_off"):
        return False
    if capability == "enum":
        return options[0] if options else ""
    if capability == "number":
        return 0.0
    if capability == "time":
        return 0  # 00:00
    return None


class VirtualAdapter:
    """Owns `virtual:*` entities defined in the virtual_entities table.

    Unlike a device adapter it has no protocol — its "device" is the DB. It
    seeds initial state for new helpers and maps commands to validated state
    updates. State lives in current_state (persisted), so a restart keeps the
    user's last value; we only seed a helper that has no state row yet.
    Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self.broker = None
        self._defs: dict[str, dict] = {}        # entity_id -> {name, capability, options}
        self._values: dict[str, object] = {}    # entity_id -> current main value (toggle cache)
        self._opts_pub: dict[str, str] = {}      # entity_id -> last published enum_options json
        self._cat_pub: dict[str, str] = {}       # entity_id -> last published category (re-announce on change)
        self._info_pub: dict[str, tuple] = {}    # entity_id -> last announced (name, category, capabilities)

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        log.info("virtual adapter up — owning virtual:* helpers")
        failures = 0
        while True:
            try:
                await self._reload()
                failures = 0
                self.status.ok(f"{len(self._defs)} device(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.warning("virtual: reload failed (%d in a row): %s", failures, exc, exc_info=True)
                if failures >= 3:
                    self.status.error(str(exc) or "db unreachable")
            await asyncio.sleep(RELOAD_INTERVAL)

    async def _reload(self) -> None:
        rows = await self.broker.call("virtual_entities")
        self._defs = {
            r["entity_id"]: {"name": r["name"], "capability": r["capability"],
                             "options": r["options"], "category": r["category"],
                             "default": r["default_value"]}
            for r in rows
        }
        seeded = {
            (r["entity_id"], r["capability"]): r["value"]
            for r in await self.broker.call("state", prefix=f"{NAMESPACE}:")
        }
        for eid, d in self._defs.items():
            cap = d["capability"]
            await self._announce(eid, d)
            # Seed the main value only if it has no state yet (don't clobber a
            # value the user already set — current_state is the source of truth).
            cur = seeded.get((eid, cap))
            if cur is None:
                # No state yet (new helper, or the seed raced the engine at boot
                # and was lost) — (re)seed. Re-publishing the same initial value
                # is idempotent, so this self-heals until it lands.
                await self._publish(eid, cap, _initial_value(cap, d["options"], d.get("default")),
                                    d["name"], d["category"])
            elif self._cat_pub.get(eid) != d["category"]:
                # Value unchanged, but the CATEGORY did (a helper flipped control<->
                # config in Settings). Re-announce once so the entity's grouping
                # tracks the DB without a restart — NOT every tick (that would spam
                # history with an unchanging value).
                self._values[eid] = cur
                await self._publish(eid, cap, cur, d["name"], d["category"])
            else:
                self._values[eid] = cur
                self._cat_pub[eid] = d["category"]  # first sight with matching state → remember
            # Publish enum options metadata when first seen or changed.
            if cap == "enum" and d["options"]:
                opts = json.dumps(d["options"])
                if self._opts_pub.get(eid) != opts:
                    await self._publish(eid, "enum_options", opts, d["name"], d["category"])
                    self._opts_pub[eid] = opts
            # A number helper's slider range → number_options (same shape device numbers
            # publish), so the UI renders a slider bounded to {min,max,step}.
            if cap == "number" and d["options"]:
                nopts = json.dumps(d["options"])
                if self._opts_pub.get(eid) != nopts:
                    await self._publish(eid, "number_options", nopts, d["name"], d["category"])
                    self._opts_pub[eid] = nopts

    async def _announce(self, entity_id: str, d: dict) -> None:
        # A state update creates the entity without a device_type, which the engine
        # otherwise fills only at its own start — until then a switch made in
        # Settings is invisible to the Matter bridge and so to Google.
        cap = d["capability"]
        caps = [cap, f"{cap}_options"] if cap in ("enum", "number") else [cap]
        sig = (d["name"], d["category"], tuple(caps))
        if self._bus is None or self._info_pub.get(entity_id) == sig:
            return
        await self._bus.publish_entity(EntityInfo(
            entity_id=entity_id, adapter=NAMESPACE, capabilities=caps,
            name=d["name"], category=d["category"],
        ))
        self._info_pub[entity_id] = sig

    async def _publish(self, entity_id: str, capability: str, value, name: str | None,
                       category: str = "control") -> None:
        if self._bus is None:
            return
        if capability not in ("enum_options", "number_options"):
            self._values[entity_id] = value
            self._cat_pub[entity_id] = category  # what the entity's category now IS on the bus
        await self._bus.publish_state(
            StateUpdate(
                entity_id=entity_id, capability=capability, value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=name, category=category,
            )
        )

    async def handle_command(self, command: Command) -> None:
        d = self._defs.get(command.entity_id)
        if d is None:
            raise CommandRejected("unknown helper")
        cap = d["capability"]
        try:
            validate_command(cap, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc

        if cap in ("boolean", "on_off"):
            # `boolean` = an input_boolean-style helper; `on_off` = a momentary
            # switch helper (e.g. a gate trigger an automation pulses + resets).
            # Same turn_on/turn_off/toggle semantics, different capability on the
            # wire (on_off lets the generic Matter bridge expose it as a switch).
            if command.command == "turn_on":
                value = True
            elif command.command == "turn_off":
                value = False
            else:  # toggle
                cur = self._values.get(command.entity_id)
                if cur is None:
                    rows = await self.broker.call("state", entity_ids=[command.entity_id], capabilities=[cap])
                    cur = rows[0]["value"] if rows else None
                value = not bool(cur)
        elif cap == "enum":
            value = command.args.get("value")
            if value is None:
                raise CommandRejected("set_option requires a value")  # never publish the literal string "None"
            opts = d["options"] or []
            if opts and value not in opts:
                raise CommandRejected(f"option {value!r} not in {opts}")
            value = str(value)
        elif cap == "number":
            try:
                value = float(command.args.get("value"))
            except (TypeError, ValueError) as exc:
                raise CommandRejected(f"set_value needs a number, got {command.args!r}") from exc
        elif cap == "time":
            # minutes since midnight; the capability spec clamps the range on validate,
            # but reject a non-integer here so we never publish a bogus value.
            try:
                value = int(command.args.get("value"))
            except (TypeError, ValueError) as exc:
                raise CommandRejected(f"set_time needs minutes-since-midnight, got {command.args!r}") from exc
        else:
            raise CommandRejected(f"{cap} helpers take no commands")

        await self._publish(command.entity_id, cap, value, d["name"], d["category"])

    async def stop(self) -> None:
        self._bus = None
