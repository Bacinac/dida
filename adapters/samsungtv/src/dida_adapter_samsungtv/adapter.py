"""Samsung TV adapter — power over the TV's own LAN API, no SmartThings in the path.

State is the REST description on port 8001 (unauthenticated): on 2019+ models
what its `PowerState` says; on older ones an answer there together with an open
media renderer (DMR, 9197), because in standby they answer 8001 with the screen
dark. Turning off is the power
key over the remote-control websocket on 8002, which the TV authorises once with
an on-screen "Allow" prompt; the token it hands back is kept encrypted. Turning on
is a wake-on-LAN packet to the MAC the TV reported while it was on.
Implements `dida_core.Adapter`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time

import aiohttp
from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    forget_reachable,
    set_reachable,
)
from dida_core.broker import BrokerError
from home_core.tasks import spawn

from dida_adapter_samsungtv.mapping import (
    NAMESPACE,
    broadcast_for,
    entity_id,
    identity,
    magic_packet,
    stated_power,
)

log = logging.getLogger("dida.adapter.samsungtv")

# The library logs every token it receives at INFO, and adapter logs are shipped
# to ClickHouse; a remote-control credential must not end up in app_logs.
logging.getLogger("samsungtvws").setLevel(logging.WARNING)

_REMOTE_NAME = "DIDA"
_REST_TIMEOUT = 3.0
# Long enough for someone to walk to the TV and accept the prompt on the first pairing.
_PAIR_TIMEOUT = 60.0
_WAKE_PORTS = (9, 7)
# On Wi-Fi a TV that is plainly on misses a single request now and then (measured:
# three lone misses in six minutes), so off is only believed after a run of them.
_MISSES_BEFORE_OFF = 3
# A 2018 TV in standby wakes every 25 minutes and answers 8001/8002/8080 for 30 s to
# 2 min with the screen dark (measured 27.–28. 9. 2026); its renderer stays shut
# through every wake and is open whenever the screen is. It also opens ~25 s after
# 8001 on a real power-on, which is the price of not reading a wake as on.
_RENDERER_PORT = 9197
_RENDERER_TIMEOUT = 1.0
# Longer than any wake: answering this long with the renderer shut means the renderer
# is off in the TV's settings, and the TV would read off for good without a word.
_DARK_LIMIT = 600.0
# The TV drops a key sent the moment its remote channel opens: measured, KEY_POWER and
# KEY_POWEROFF sent straight after the connect event were ignored three times, the
# same key two seconds into a listening connection switched it off at once.
_KEY_READY = 2.0
_KEY_HOLD_OPEN = 1.0
# After a command the poll tightens so the switch settles in seconds, not a full interval.
_SETTLE_POLLS = (1.0, 2.0, 3.0, 5.0, 8.0)


class SamsungTVAdapter:
    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self.broker = None
        self._cfg: AdapterConfig | None = None
        self._session: aiohttp.ClientSession | None = None
        self._host = ""
        self._name = ""
        self._entity = ""
        self._device: dict = {}            # learned while on: duid + mac
        self._token: str | None = None
        self._announced: tuple | None = None
        self._on: bool | None = None       # None until the TV has answered or stayed silent long enough
        self._published: tuple | None = None
        self._reachable = False
        self._misses = 0
        self._dark_since: float | None = None
        self._pair_tried = False           # one prompt per power-on, never a prompt loop
        self._refused = False
        self._wake = asyncio.Event()

    # --- persisted, adapter-managed ----------------------------------------

    async def _load_private(self) -> None:
        raw = await self.broker.call("stored", key="_device")
        if raw:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                blob = json.loads(raw)
                self._device = blob if isinstance(blob, dict) else {}
        try:
            self._token = await self.broker.call("stored", key="_token")
        except BrokerError as exc:
            log.error("samsungtv: _token unreadable (%s) — the TV will prompt again", exc)
            self._token = None

    async def _save(self, key: str, value: str) -> None:
        await self.broker.call("store", key=key, value=value)

    # --- lifecycle ---------------------------------------------------------

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        self._session = aiohttp.ClientSession()
        await self._cfg.load()
        await self._load_private()
        spawn(self._cfg.poll_loop(), log=log, name="samsungtv config poll")
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("samsungtv: poll error: %s", exc, exc_info=True)
                self.status.error(str(exc) or "poll error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(1, self._cfg.int("poll_seconds", 5)))
            self._wake.clear()

    async def _poll(self) -> None:
        assert self._cfg is not None
        self._host = self._cfg.get("host").strip()
        if not self._host:
            self.status.idle("no TV IP — Settings → Adapters")
            return
        self._name = self._cfg.get("name").strip() or "Samsung TV"
        entity = entity_id(self._name)
        if entity != self._entity:
            if self._entity:
                forget_reachable(self._entity, NAMESPACE)
            self._entity, self._reachable = entity, False
        info = await self._describe()
        if info is not None:
            await self._learn(info)
        await self._announce()
        seen = stated_power(info)
        dark = False
        if seen is None and info is not None:
            seen = await self._renderer_up() or None
            dark = seen is None
        self._dark_since = (self._dark_since or time.monotonic()) if dark else None
        if seen is None:
            self._misses += 1
            if self._misses >= _MISSES_BEFORE_OFF:
                self._on = False
        else:
            self._misses = 0
            self._on = seen
        on = bool(self._on)
        if info is not None and not self._reachable:
            # The only reachability verdict this adapter gives. A TV that stops
            # answering has, as far as the LAN can tell, been switched off; calling
            # that "unreachable" would ring the device alarm every evening.
            self._reachable = True
            await set_reachable(self._bus, self._entity, NAMESPACE, True)
        if self._on is not None:
            await self._publish_power(on)
        if on and self._token is None and not self._pair_tried:
            self._pair_tried = True
            spawn(self._pair(), log=log, name="samsungtv pairing")
        if not on:
            self._pair_tried = False
        self._report(info, on)

    def _report(self, info: dict | None, on: bool) -> None:
        if info is None and not self._device:
            self.status.connecting(f"waiting for the TV to answer on {self._host}")
        elif self._refused:
            self.status.error("the TV refused DIDA's remote — allow it under External Device Manager → Device Connection Manager")
        elif self._token is None and on:
            self.status.connecting("accept DIDA on the TV to allow turning it off")
        elif self._dark_since is not None and time.monotonic() - self._dark_since > _DARK_LIMIT:
            self.status.error(f"the TV answers but its media renderer (port {_RENDERER_PORT}) stays shut — power reads off")
        else:
            self.status.ok(f"{self._name} · {'on' if on else 'off'}")

    async def _describe(self) -> dict | None:
        from samsungtvws.async_rest import SamsungTVAsyncRest

        assert self._session is not None
        rest = SamsungTVAsyncRest(self._host, session=self._session, port=8001, timeout=_REST_TIMEOUT)
        try:
            info = await rest.rest_device_info()
        except Exception as exc:
            log.debug("samsungtv: %s not answering: %s", self._host, exc, exc_info=True)
            return None
        return info if isinstance(info, dict) else None

    async def _renderer_up(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, _RENDERER_PORT), _RENDERER_TIMEOUT)
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    async def _learn(self, info: dict) -> None:
        learned = {**self._device, **identity(info)}
        if learned != self._device:
            self._device = learned
            await self._save("_device", json.dumps(learned))
            log.info("samsungtv: %s is %s (mac %s)", self._host, learned.get("duid"), learned.get("mac"))

    async def _announce(self) -> None:
        assert self._bus is not None
        sig = (self._entity, self._name, self._device.get("duid"))
        if sig == self._announced:
            return
        await self._bus.publish_entity(EntityInfo(
            entity_id=self._entity, adapter=NAMESPACE, capabilities=["on_off"], name=self._name,
            device=self._entity, device_name=self._name, device_type="media",
            native_key=self._device.get("duid"),
        ))
        self._announced = sig

    async def _publish_power(self, on: bool) -> None:
        assert self._bus is not None
        if (self._entity, on) == self._published:
            return
        self._published = (self._entity, on)
        await self._bus.publish_state(StateUpdate(
            entity_id=self._entity, capability="on_off", value=on, adapter=NAMESPACE,
            ts_ns=time.time_ns(), name=self._name, device=self._entity, device_name=self._name,
        ))

    # --- remote control ----------------------------------------------------

    def _remote(self):
        from samsungtvws.async_remote import SamsungTVWSAsyncRemote

        return SamsungTVWSAsyncRemote(
            self._host, token=self._token, port=8002, timeout=_REST_TIMEOUT,
            key_press_delay=0, name=_REMOTE_NAME,
        )

    async def _open(self, remote, timeout_s: float, *, listen: bool = False) -> None:
        """Open the remote channel and keep whatever token the TV issued with it."""
        async with asyncio.timeout(timeout_s):
            await (remote.start_listening() if listen else remote.open())
        if remote.token and remote.token != self._token:
            self._token = remote.token
            await self._save("_token", self._token)
            self._refused = False
            log.info("samsungtv: the TV authorised DIDA's remote")

    async def _pair(self) -> None:
        from samsungtvws.exceptions import UnauthorizedError

        remote = self._remote()
        try:
            await self._open(remote, _PAIR_TIMEOUT)
        except UnauthorizedError:
            self._refused = True
            log.warning("samsungtv: the TV refused DIDA — allow it under Settings → General → "
                        "External Device Manager → Device Connection Manager")
        except Exception as exc:
            log.warning("samsungtv: pairing did not complete (%s) — the TV will ask again next time it is on", exc, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await remote.close()

    async def _power_key(self) -> None:
        from samsungtvws.exceptions import UnauthorizedError
        from samsungtvws.remote import SendRemoteKey

        remote = self._remote()
        try:
            try:
                await self._open(remote, _PAIR_TIMEOUT if self._token is None else _REST_TIMEOUT * 2, listen=True)
            except UnauthorizedError:
                self._refused = True
                raise
            await asyncio.sleep(_KEY_READY)
            await remote.send_command(SendRemoteKey.click("KEY_POWER"), key_press_delay=_KEY_HOLD_OPEN)
        finally:
            with contextlib.suppress(Exception):
                await remote.close()

    def _send_wake(self) -> None:
        mac = self._device.get("mac")
        if not mac:
            raise RuntimeError("the TV's MAC is not known yet — it has to be seen on once before DIDA can wake it")
        with open("/proc/net/route") as fh:
            broadcast = broadcast_for(self._host, fh.read())
        if broadcast is None:
            raise RuntimeError(f"{self._host} is not on a directly connected network — a wake packet cannot reach it")
        packet = magic_packet(mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in _WAKE_PORTS:
                sock.sendto(packet, (broadcast, port))
                sock.sendto(packet, (self._host, port))

    async def handle_command(self, command: Command) -> None:
        if command.entity_id != self._entity or command.capability != "on_off":
            raise CommandRejected(f"only {self._entity} on_off is controllable")
        cmd = command.command
        if cmd == "toggle":
            cmd = "turn_off" if self._on else "turn_on"
        try:
            if cmd == "turn_off":
                if not self._on:
                    return
                await self._power_key()
            elif cmd == "turn_on":
                if self._on:
                    return
                await asyncio.to_thread(self._send_wake)
            else:
                raise CommandRejected(f"on_off has no {cmd}")
        except CommandRejected:
            raise
        except Exception as exc:
            raise CommandRejected(f"{cmd} failed: {exc}") from exc
        log.info("samsungtv: %s sent (%s)", cmd, command.source or "?")
        spawn(self._settle(), log=log, name="samsungtv settle")

    async def _settle(self) -> None:
        for delay in _SETTLE_POLLS:
            await asyncio.sleep(delay)
            self._wake.set()

    async def stop(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
