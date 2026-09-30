from __future__ import annotations

import asyncio
import json
import logging
import time

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    StateUpdate,
    validate_command,
)
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.midea")

NAMESPACE = "midea"
ENTITY_ID = f"{NAMESPACE}:ac"


class MideaAdapter:
    """Drives a Midea AC over the local protocol (msmart-ng).

    A poll loop refreshes device state and publishes climate capabilities;
    commands set the corresponding attribute and apply() it back. All device
    network I/O is serialised behind a lock so a command and a poll can't race
    the same connection. Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._active_key: tuple | None = None
        self._bus: Bus | None = None
        self._dev = None
        self._lock = asyncio.Lock()
        self._last: dict[str, object] = {}
        self._mode_to_str: dict = {}
        self._str_to_mode: dict = {}
        self._fan_to_str: dict = {}
        self._str_to_fan: dict = {}

    def _conn_key(self) -> tuple | None:
        """AC access (host, id, token, k1, port) from the DB, or None if unset."""
        host = (self._cfg.get("host") if self._cfg else "").strip()
        dev_id = (self._cfg.get("id") if self._cfg else "").strip()
        token = (self._cfg.get("token") if self._cfg else "").strip()
        k1 = (self._cfg.get("k1") if self._cfg else "").strip()
        if not (host and dev_id and token and k1):
            return None
        port = self._cfg.int("port", 6444) if self._cfg else 6444
        return (host, dev_id, token, k1, port)

    def _build_maps(self, AC) -> None:
        M = AC.OperationalMode
        self._mode_to_str = {
            M.COOL: "cool", M.HEAT: "heat", M.AUTO: "auto",
            M.DRY: "dry", M.FAN_ONLY: "fan_only", M.SMART_DRY: "dry",
        }
        self._str_to_mode = {
            "cool": M.COOL, "heat": M.HEAT, "auto": M.AUTO,
            "dry": M.DRY, "fan_only": M.FAN_ONLY,
        }
        F = AC.FanSpeed
        self._fan_to_str = {
            F.AUTO: "auto", F.LOW: "low", F.MEDIUM: "medium",
            F.HIGH: "high", F.SILENT: "silent", F.MAX: "max",
        }
        self._str_to_fan = {v: k for k, v in self._fan_to_str.items()}

    async def start(self, bus: Bus) -> None:
        from msmart.device import AirConditioner as AC

        self._bus = bus
        self._build_maps(AC)
        self._cfg = AdapterConfig("midea", self.broker)
        # Supervise: (re)authenticate the AC when the UI sets/changes the config.
        failures = 0
        while True:
            try:
                await self._cfg.load()
                key = self._conn_key()
                if key != self._active_key:
                    await self._apply(AC, key)
                if self._dev is not None:
                    async with self._lock:
                        await self._dev.refresh()
                    self._publish()
                    failures = 0
                    self.status.ok(f"{key[0]} (id={key[1]})" if key else "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.warning("midea: refresh/publish failed (%d in a row): %s", failures, exc, exc_info=True)
                if failures >= 3:
                    self.status.error(str(exc) or "AC unreachable")
            await asyncio.sleep(self._cfg.int("poll_seconds", 30) if self._cfg else 30)

    async def _apply(self, AC, key: tuple | None) -> None:
        self._dev = None
        self._active_key = key
        if key is None:
            self.status.idle("no AC configured")
            log.info("midea: no config — idle (set it in Settings → Adapters)")
            return
        host, dev_id, token, k1, port = key
        self.status.connecting(host)
        try:
            dev = AC(ip=host, port=int(port), device_id=int(dev_id))
            await dev.authenticate(token, k1)
            self._dev = dev
            self.status.ok(f"{host} (id={dev_id})")
            log.info("midea adapter up — AC at %s (id=%s)", host, dev_id)
        except Exception as exc:
            self.status.error(str(exc) or "auth failed")
            log.warning("midea: connect failed: %s (will retry)", exc, exc_info=True)
            self._active_key = None  # force a retry on the next tick

    def _hvac_mode(self) -> str:
        if not self._dev.power_state:
            return "off"
        return self._mode_to_str.get(self._dev.operational_mode, "auto")

    def _publish(self) -> None:
        readings = {
            "hvac_mode": self._hvac_mode(),
            "fan_mode": self._fan_to_str.get(self._dev.fan_speed, "auto"),
            # Metadata: the modes/fans THIS AC supports, so the UI dropdowns list
            # the device's real set (not the full canonical one), like esphome climate.
            "hvac_mode_options": json.dumps(["off", *self._str_to_mode]),
            "fan_mode_options": json.dumps(list(dict.fromkeys(self._fan_to_str.values()))),
        }
        if self._dev.target_temperature is not None:
            readings["target_temperature"] = float(self._dev.target_temperature)
        if self._dev.indoor_temperature is not None:
            readings["temperature"] = float(self._dev.indoor_temperature)
        for cap, value in readings.items():
            if self._last.get(cap) == value:
                continue
            self._last[cap] = value
            unit = "°C" if cap in ("temperature", "target_temperature") else None
            spawn(self._bus.publish_state(StateUpdate(
                entity_id=ENTITY_ID, capability=cap, value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(), unit=unit, name="Air Conditioner",
            )), log=log, name=f"publish {cap}")

    async def handle_command(self, command: Command) -> None:
        if command.entity_id != ENTITY_ID:
            raise CommandRejected("unknown AC")
        if self._dev is None:
            raise CommandRejected("AC not connected")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        try:
            # Mutate the device object AND apply() under the SAME lock: otherwise a
            # concurrent poll refresh() (which also holds the lock, for seconds) can
            # complete between our mutation and apply(), overwriting the command with
            # the AC's old reported state → the command silently no-ops.
            async with self._lock:
                self._apply_command(command)
                await self._dev.apply()
                # The AC needs a moment to settle before its reported state
                # reflects the change; refreshing too soon reads the old value.
                await asyncio.sleep(1.5)
                await self._dev.refresh()
            self._publish()
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandRejected(f"cannot map {command.command}: {exc}") from exc
        except Exception as exc:
            raise CommandRejected(f"AC refused: {exc}") from exc

    def _apply_command(self, command: Command) -> None:
        cap, args = command.capability, command.args
        if cap == "hvac_mode":  # set_hvac_mode
            mode = str(args.get("value"))
            if mode == "off":
                self._dev.power_state = False
            else:
                self._dev.power_state = True
                self._dev.operational_mode = self._str_to_mode[mode]
        elif cap == "target_temperature":  # set_temperature
            self._dev.target_temperature = float(args.get("value"))
        elif cap == "fan_mode":  # set_fan_mode
            self._dev.fan_speed = self._str_to_fan[str(args.get("value"))]

    async def stop(self) -> None:
        if self._dev is not None:
            try:
                close = getattr(self._dev, "close", None)
                if close is not None:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                log.debug("midea: device close failed", exc_info=True)
                pass
            self._dev = None
