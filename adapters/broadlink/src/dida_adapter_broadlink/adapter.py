from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
from pathlib import Path

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    answer_discover,
    validate_command,
)

log = logging.getLogger("dida.adapter.broadlink")

NAMESPACE = "broadlink"
CODES_PATH = Path(os.environ.get("DIDA_BROADLINK_CODES", "/state/codes.json"))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "device"


def _mac_hex(mac: str) -> str:
    """Raw hex (no separators) for python-broadlink's unhexlify — the UI stores the
    MAC in the standard `aa:bb:cc:…` form like every other adapter, tolerate any of
    `:`, `-`, `.` (or none)."""
    return re.sub(r"[^0-9a-fA-F]", "", mac)


class BroadlinkAdapter:
    """Sends learned IR/RF codes through a Broadlink RM blaster (python-broadlink,
    which is synchronous, so device I/O runs in an executor). One blaster fronts
    several physical devices (Screen, Laser, Projector, …); every command is its own
    write-only `press`-button entity, grouped under its device — pressing it transmits
    the code. No readable state (pure transmitter). Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._host = ""
        self._mac = ""
        self._type = 0
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._dev = None
        self._codes: dict[str, dict[str, bytes]] = {}  # device -> {command: raw bytes}
        self._entity_cmd: dict[str, tuple[str, str]] = {}  # entity_id -> (device, command)
        self._power: dict[str, bool] = {}  # on_off entity_id -> optimistic state (write-only IR)
        self._active_key: tuple | None = None

    def _load_codes(self) -> dict[str, dict[str, bytes]]:
        # One physical device (Screen, Laser, Projector, …) per top-level key, each
        # exposed as its own entity — a single blaster fronts several devices.
        if not CODES_PATH.exists():
            return {}  # learned IR codes are an off-repo file; absent = no commands yet
        try:
            data = json.loads(CODES_PATH.read_text())
        except (ValueError, OSError) as exc:
            # A corrupt/unreadable codes.json must NOT crash start() (it runs before
            # the supervise-loop try) — that would wedge the container in a restart
            # loop. Degrade to "no commands yet" (same contract as an absent file)
            # and surface it loud in the log so the bad file gets noticed.
            log.warning("broadlink: cannot read %s: %s — no commands loaded", CODES_PATH, exc)
            return {}
        if not isinstance(data, dict):
            # Valid JSON but not an object (a bare list / null / string) — the .items()
            # loop below would AttributeError; degrade to no-commands, same contract.
            log.warning("broadlink: %s is not a JSON object — no commands loaded", CODES_PATH)
            return {}
        codes: dict[str, dict[str, bytes]] = {}
        for device, cmds in data.items():
            for cmd, code in (cmds or {}).items():
                try:
                    # Broadlink base64 codes are commonly stored unpadded (HA, community
                    # dumps) — pad to a multiple of 4 so strict b64decode accepts them.
                    codes.setdefault(device, {})[cmd] = base64.b64decode(code + "=" * (-len(code) % 4))
                except (ValueError, TypeError):
                    log.warning("broadlink: bad code for %s/%s", device, cmd)
        return codes

    def _all_commands(self) -> list[str]:
        return sorted(f"{dev} {cmd}" for dev, cmds in self._codes.items() for cmd in cmds)

    def _reindex(self) -> None:
        self._entity_cmd = {
            f"{NAMESPACE}:{_slug(dev)}_{_slug(cmd)}": (dev, cmd)
            for dev, cmds in self._codes.items()
            for cmd in cmds
        }

    def _power_cmd(self, device: str) -> str | None:
        """A learned command named "On" is exposed as the device's on_off toggle (a
        party laser: On = power, its other keys are modes). None → the device is
        press-only. No discrete "Off" is assumed — the same IR toggles both ways."""
        for cmd in self._codes.get(device, {}):
            if cmd.strip().lower() == "on":
                return cmd
        return None

    def _connect(self):
        import broadlink

        dev = broadlink.gendevice(self._type, (self._host, 80), binascii.unhexlify(_mac_hex(self._mac)))
        dev.auth()
        return dev

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("broadlink", self.broker)
        self._codes = self._load_codes()
        self._reindex()
        await bus.nc.subscribe("dida.discover.broadlink", cb=self._on_discover)
        # Supervise: (re)connect when the UI sets/changes host/mac/type; re-publish
        # the (static) command list each tick to self-heal the boot race.
        while True:
            try:
                await self._cfg.load()
                # Re-read codes.json each tick: learned/edited codes apply live
                # (it used to load once at boot only).
                codes = self._load_codes()
                if codes != self._codes:
                    self._codes = codes
                    self._reindex()
                    log.info("broadlink: codes reloaded — %d device(s), %d command(s)",
                             len(self._codes), len(self._all_commands()))
                key = self._conn_key()
                if key != self._active_key:
                    await self._apply(key)
                if self._dev is not None:
                    await self._announce()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("broadlink: supervise loop error")
                self.status.error(str(exc) or "supervise error")
            await asyncio.sleep(30)

    async def _on_discover(self, msg) -> None:
        await answer_discover(self._bus, msg, "broadlink", self._discover)

    async def _discover(self, subnets: list[str]) -> dict:
        import ipaddress

        import broadlink

        # Broadlink discovery is a UDP broadcast. Local (255…) works if DIDA shares
        # the device L2; a per-subnet directed broadcast reaches a routed VLAN only
        # if the router forwards directed broadcasts (many don't).
        targets = ["255.255.255.255"]
        for cidr in subnets:
            try:
                targets.append(str(ipaddress.ip_network(cidr.strip(), strict=False).broadcast_address))
            except ValueError:
                log.warning("broadlink: bad subnet %r", cidr)
        loop = asyncio.get_running_loop()
        seen: dict[str, dict] = {}
        for tgt in targets:
            try:
                devs = await loop.run_in_executor(
                    None, lambda t=tgt: broadlink.discover(timeout=4, discover_ip_address=t)
                )
            except Exception as exc:
                log.debug("broadlink discover on %s: %s", tgt, exc, exc_info=True)
                continue
            for d in devs:
                ip = d.host[0] if isinstance(d.host, (tuple, list)) else str(d.host)
                raw = binascii.hexlify(d.mac).decode() if isinstance(d.mac, (bytes, bytearray)) else _mac_hex(str(d.mac))
                mac = ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))  # standard aa:bb:… form
                model = getattr(d, "model", None) or d.__class__.__name__
                seen[ip] = {"label": f"{model} ({ip})", "set": {"host": ip, "mac": mac, "type": hex(d.devtype)}}
        return {"devices": list(seen.values())}

    def _conn_key(self) -> tuple | None:
        host = self._cfg.get("host") if self._cfg else ""
        mac = self._cfg.get("mac") if self._cfg else ""
        if not host or not mac:
            return None
        try:
            typ = int((self._cfg.get("type") if self._cfg else "0") or "0", 0)
        except ValueError:
            # Configured-but-wrong is an ERROR badge; "idle: no device configured"
            # hid the bad hex behind a neutral state.
            self.status.error(f"bad device type {self._cfg.get('type')!r}")
            log.warning("broadlink: bad device type %r", self._cfg.get("type") if self._cfg else "")
            return None
        return (host, mac, typ)

    async def _apply(self, key: tuple | None) -> None:
        self._dev = None
        self._active_key = key
        if key is None:
            self.status.idle("no device configured")
            log.info("broadlink: no device configured — idle (set it in Settings → Adapters)")
            return
        self._host, self._mac, self._type = key
        self.status.connecting(self._host)
        loop = asyncio.get_running_loop()
        try:
            self._dev = await loop.run_in_executor(None, self._connect)
            cmds = self._all_commands()
            self.status.ok(f"{self._host} · {len(self._codes)} device(s), {len(cmds)} command(s)")
            log.info("broadlink adapter up — %s, %d device(s), %d command(s): %s",
                     self._host, len(self._codes), len(cmds), ", ".join(cmds))
        except Exception as exc:
            self.status.error(f"{self._host}: {exc}" if str(exc) else f"{self._host} unreachable")
            log.warning("broadlink: connect to %s failed: %s (will retry)", self._host, exc, exc_info=True)
            self._active_key = None  # force a retry on the next tick

    async def _announce(self) -> None:
        if self._bus is None:
            return
        # Every command is its own press-button entity, grouped under its device
        # (Screen/Laser/Projector) → one row + expose toggle per button, like any
        # other multi-control device. No readable state (pure transmit) → announce
        # via the entity catalog. EXCEPTION: a device with an "On" command exposes that
        # ONE entity as an on_off toggle (a light default) so it can carry state (for a
        # marker glow) and drive turn_on/turn_off; its other keys stay press modes.
        for device, cmds in self._codes.items():
            dslug = _slug(device)
            pcmd = self._power_cmd(device)
            for cmd in sorted(cmds):
                is_power = cmd == pcmd
                eid = f"{NAMESPACE}:{dslug}_{_slug(cmd)}"
                await self._bus.publish_entity(EntityInfo(
                    entity_id=eid, adapter=NAMESPACE,
                    capabilities=["on_off"] if is_power else ["press"], name=cmd,
                    device=dslug, device_name=device,
                    device_type="light" if is_power else "button",
                ))
                if is_power:
                    # Seed the optimistic state so the marker reads a definite on/off
                    # (write-only IR gives no feedback; a press below flips it).
                    await self._bus.publish_state(StateUpdate(
                        entity_id=eid, capability="on_off", value=self._power.get(eid, False),
                        adapter=NAMESPACE, ts_ns=time.time_ns(),
                        name=cmd, device=dslug, device_name=device))

    async def handle_command(self, command: Command) -> None:
        lookup = self._entity_cmd.get(command.entity_id)
        if lookup is None:
            raise CommandRejected("no learned code")
        device, cmd = lookup
        if self._dev is None:
            raise CommandRejected("device not connected")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        if command.capability == "on_off":
            await self._handle_power(command, device, cmd)
            return
        if command.capability != "press" or command.command != "press":
            return
        code = self._codes.get(device, {}).get(cmd)
        if code is None:
            raise CommandRejected(f"no learned code for {device}/{cmd}")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._dev.send_data, code)
            log.info("broadlink sent %s/%s", device, cmd)
        except Exception as exc:
            # A failed transmit means the blaster went unreachable (unplugged / off
            # the net). Surface it (red badge) and force a reconnect + re-auth on the
            # next supervise tick, instead of swallowing every press behind a stale
            # green while the button silently does nothing.
            self.status.error(f"{self._host}: transmit failed")
            self._dev = None
            self._active_key = None
            raise CommandRejected(f"send {device}/{cmd} failed: {exc}") from exc

    async def _handle_power(self, command: Command, device: str, cmd: str) -> None:
        # The "On" IR is a toggle (no discrete Off), so only blast when the desired
        # state differs from what we last set — blasting while already there would flip
        # it the wrong way. No device feedback: this is optimistic and can drift if the
        # laser is toggled by its physical remote, but a fresh turn_on/off re-aligns it.
        eid = command.entity_id
        cur = self._power.get(eid, False)
        want = {"turn_on": True, "turn_off": False}.get(command.command, not cur)
        if want != cur:
            code = self._codes.get(device, {}).get(cmd)
            if code is None:
                raise CommandRejected(f"no learned code for {device}/{cmd}")
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._dev.send_data, code)
                log.info("broadlink sent %s/%s (power→%s)", device, cmd, "on" if want else "off")
            except Exception as exc:
                self.status.error(f"{self._host}: transmit failed")
                self._dev = None
                self._active_key = None
                raise CommandRejected(f"send {device}/{cmd} failed: {exc}") from exc
            if want:
                # Power-on lands the laser in its default running mode (Auto), not whatever
                # mode it happened to hold — send the "Auto" IR after it warms up.
                auto = next((c for name, c in self._codes.get(device, {}).items()
                             if name.strip().lower() == "auto"), None)
                if auto is not None:
                    await asyncio.sleep(0.6)
                    try:
                        await asyncio.get_running_loop().run_in_executor(None, self._dev.send_data, auto)
                        log.info("broadlink sent %s/Auto (power-on default mode)", device)
                    except Exception:
                        log.debug("broadlink: auto mode after power-on not sent", exc_info=True)
                        pass  # best-effort mode set; the power state already stands
        self._power[eid] = want
        if self._bus is not None:
            await self._bus.publish_state(StateUpdate(
                entity_id=eid, capability="on_off", value=want,
                adapter=NAMESPACE, ts_ns=time.time_ns(),
                name=cmd, device=_slug(device), device_name=device))

    async def stop(self) -> None:
        self._dev = None
