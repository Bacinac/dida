from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    set_reachable,
    validate_command,
)
from home_core.tasks import spawn

from dida_adapter_roidmi.mapping import (
    ACTION_HOME,
    ACTION_START,
    ACTION_STOP,
    FAN_OPTIONS,
    PROP_FANSPEED,
    PROPS,
    fan_id,
    status_caps,
)
from dida_adapter_roidmi.miio import MiioClient, MiioError

log = logging.getLogger("dida.adapter.roidmi")

NAMESPACE = "roidmi"
_CAPS = ["vacuum", "battery", "enum", "enum_options"]
# Consecutive failed polls before the badge goes error: one lost UDP datagram
# must not flap the badge, three in a row is a device that is actually gone.
_FAILS_BEFORE_ERROR = 3


class RoidmiAdapter:
    """Roidmi robot vacuum (Xiaomi ecosystem) over the local miio protocol.

    Fully local: one configured host + the device token (extracted once from
    the Mi Home cloud). A status poll reads run-state, battery and suction
    level; commands are MiOT actions (start / stop-in-place / return to dock)
    plus the suction level as the entity's `enum`. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._client: MiioClient | None = None
        self._key: tuple | None = None      # (host, token) the client was built for
        self._entity_id = f"{NAMESPACE}:vacuum"
        self._name = "Roidmi"
        self._last: dict[str, object] = {}  # cap -> last published (dedupe)
        self._fails = 0
        self._announced = False
        self._fault: str | None = None      # last reported device fault (log on transitions)
        self._reach: bool | None = None     # last reachability we published (edge-trigger)
        self._io = asyncio.Lock()

    def _conn_key(self) -> tuple | None:
        host = (self._cfg.get("host") if self._cfg else "").strip()
        token = (self._cfg.get("token") if self._cfg else "").strip()
        if not host or not token:
            return None
        return (host, token)

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        spawn(self._cfg.poll_loop(), log=log, name="roidmi config poll")
        await self._cfg.load()
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("roidmi: poll loop error: %s", exc, exc_info=True)
                self.status.error(str(exc) or "poll error")
            await asyncio.sleep(self._cfg.int("poll_seconds", 30) if self._cfg else 30)

    async def _poll(self) -> None:
        async with self._io:
            await self._poll_device()

    async def _poll_device(self) -> None:
        key = self._conn_key()
        if key is None:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
                self._client = None
                self._key = None
            self.status.idle("no host/token (Settings → Adapters)")
            return
        if key != self._key:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
            self._client = MiioClient(*key)
            self._key = key
            self._announced = False
            self._last.clear()
        self._name = (self._cfg.get("name") if self._cfg else "") or "Roidmi"
        try:
            results = await asyncio.to_thread(self._client.get_properties, PROPS)
        except MiioError as exc:
            self._fails += 1
            if self._fails >= _FAILS_BEFORE_ERROR:
                self.status.error(f"unreachable: {exc}")
                await self._publish_reach(False, str(exc))
            # Log the descent, then go quiet. The badge and the entity's
            # reachability already carry "gone" for as long as it lasts; a vacuum
            # left off its dock for a week must not cost 2k log lines a day and
            # bury every other adapter's warnings.
            if self._fails <= _FAILS_BEFORE_ERROR:
                log.warning("roidmi: poll failed (%d in a row): %s", self._fails, exc)
            elif self._fails == _FAILS_BEFORE_ERROR + 1:
                log.warning("roidmi: still unreachable — quiet until it answers again")
            return
        if self._fails:
            log.info("roidmi: reachable again after %d failed poll(s)", self._fails)
        self._fails = 0
        await self._publish_reach(True)
        if not self._announced:
            await self._announce()
        caps, fault = status_caps(results)
        for cap, value in caps.items():
            await self._set(cap, value)
        if fault is not None:
            # A fault is actionable (stuck, dustbin full…): the badge goes error
            # so the Alerts pipeline rings, not just a quiet state change. The
            # transition is also LOGGED — the badge is transient, and a fault
            # that clears before anyone looks would otherwise leave no trace.
            if fault != self._fault:
                log.error("roidmi: device fault: %s (battery %s)", fault, caps.get("battery"))
            self.status.error(f"{self._name}: {fault}")
        else:
            if self._fault is not None:
                log.info("roidmi: device fault cleared (%s)", caps.get("vacuum", "?"))
            self.status.ok(f"{self._name} · {caps.get('vacuum', '?')}")
        self._fault = fault

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """The poll socket's verdict, published on a CHANGE only — the same
        threshold as the badge, so the device row and the alert agree with it."""
        host = self._key[0] if self._key else None
        if self._bus is None or not host or self._reach == ok:
            return
        self._reach = ok
        await set_reachable(self._bus, host, NAMESPACE, ok, detail="" if ok else detail)

    async def _announce(self) -> None:
        assert self._bus is not None and self._client is not None
        host = self._key[0] if self._key else None
        await self._bus.publish_entity(EntityInfo(
            entity_id=self._entity_id, adapter=NAMESPACE, name=self._name,
            device=host, device_name=self._name, capabilities=_CAPS,
        ))
        await self._set("enum_options", json.dumps(FAN_OPTIONS))
        self._announced = True
        log.info("roidmi vacuum %r → %s at %s", self._name, self._entity_id, host)

    async def _set(self, capability: str, value: object) -> None:
        if self._last.get(capability) == value:
            return
        self._last[capability] = value
        assert self._bus is not None
        host = self._key[0] if self._key else None
        await self._bus.publish_state(StateUpdate(
            entity_id=self._entity_id, capability=capability, value=value,  # type: ignore[arg-type]
            adapter=NAMESPACE, ts_ns=time.time_ns(), name=self._name,
            device=host, device_name=self._name,
            unit="%" if capability == "battery" else None,
        ))

    async def handle_command(self, command: Command) -> None:
        async with self._io:
            await self._command(command)
        await asyncio.sleep(1)
        with contextlib.suppress(Exception):
            await self._poll()

    async def _command(self, command: Command) -> None:
        if command.entity_id != self._entity_id:
            raise CommandRejected("unknown vacuum")
        if self._client is None:
            raise CommandRejected("not configured")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        try:
            if command.capability == "vacuum":
                siid, aiid = {"start": ACTION_START, "pause": ACTION_STOP, "dock": ACTION_HOME}[command.command]
                await asyncio.to_thread(self._client.action, siid, aiid)
            elif command.capability == "enum" and command.command == "set_option":
                option = str(command.args.get("value", ""))
                level = fan_id(option)
                if level is None:
                    raise CommandRejected(f"unknown suction option {option!r} (one of {FAN_OPTIONS})")
                await asyncio.to_thread(self._client.set_property, *PROP_FANSPEED, level)
            else:
                raise CommandRejected(f"{command.capability}/{command.command} is not a vacuum command")
        except MiioError as exc:
            self.status.error(f"{command.command} failed: {exc}")
            raise CommandRejected(f"device refused: {exc}") from exc
    async def stop(self) -> None:
        async with self._io:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
                self._client = None
