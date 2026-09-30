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
    answer_discover,
    probe_hosts,
    slug,
    subnet_hosts,
    validate_command,
)
from home_core.tasks import spawn

from dida_adapter_shelly.mapping import command_to_rpc, component_updates

log = logging.getLogger("dida.adapter.shelly")

NAMESPACE = "shelly"

# Device ids ("shelly1g4-a0b1c2d3e4f5") are already subject-safe; component keys
# ("switch:0") are not — the colon would break a NATS subject, so they go through
# core `slug()`.


class _Dev:
    __slots__ = ("comp_names", "dev_id", "ip", "name")

    def __init__(self, ip: str, dev_id: str, name: str, comp_names: dict[str, str]) -> None:
        self.ip = ip
        self.dev_id = dev_id
        self.name = name
        self.comp_names = comp_names  # "switch:0" -> friendly name


class ShellyAdapter:
    """Bridges Shelly Gen2+ devices onto the DIDA bus via their local RPC.

    HTTP RPC for reads and commands; an outbound WebSocket per device receives
    NotifyStatus pushes for real-time state. One fault domain: a device outage
    or malformed frame is contained here and retried, never reaching the engine.
    Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None
        self._devices: dict[str, _Dev] = {}  # dev_id -> _Dev (for command routing)
        self._live: set[str] = set()  # dev_ids with a LIVE websocket (drives the badge)
        self._last: dict[tuple[str, str], object] = {}  # (entity_id, cap) -> value (dedupe)
        self._curated: set[str] = set()  # entity_ids that have a primary (non-diagnostic) cap
        self._tasks: dict[str, asyncio.Task] = {}  # ip -> per-device run task
        self._down_ticks = 0  # consecutive supervise ticks with zero live devices

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig("shelly", self.broker)
        await bus.nc.subscribe("dida.discover.shelly", cb=self._on_discover)
        # Supervise: spawn/cancel a per-device task as the UI host list changes,
        # so devices can be added/removed without restarting the adapter.
        while True:
            try:
                await self._cfg.load()
                hosts = {h.strip() for h in (self._cfg.get("hosts") or "").split(",") if h.strip()}
                for ip in hosts - set(self._tasks):
                    log.info("shelly: adding host %s", ip)
                    self._tasks[ip] = spawn(self._run_device(ip), log=log, name=f"shelly device {ip}")
                for ip in set(self._tasks) - hosts:
                    log.info("shelly: removing host %s", ip)
                    self._tasks.pop(ip).cancel()
                    # Prune the removed host's device record so the badge doesn't
                    # overcount and a command can't route to its stale IP.
                    for dev_id in [d for d, dev in self._devices.items() if dev.ip == ip]:
                        self._devices.pop(dev_id, None)
                        self._live.discard(dev_id)
                # Badge from LIVE websockets, not ever-connected devices — and go
                # red (not eternal "connecting") once everything has been down for
                # a few ticks, so a dead fleet can't hide behind a stale count.
                if not hosts:
                    self.status.idle("no devices configured")
                    self._down_ticks = 0
                elif self._live:
                    self.status.ok(f"{len(self._live)}/{len(hosts)} connected")
                    self._down_ticks = 0
                else:
                    self._down_ticks += 1
                    if self._down_ticks >= 3:
                        self.status.error(f"0/{len(hosts)} reachable")
                    else:
                        self.status.connecting(f"0/{len(hosts)} connected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("shelly: supervise loop error")
                self.status.error(str(exc) or "supervise error")  # fail loud, don't leave a stale badge
            await asyncio.sleep(10)

    async def _on_discover(self, msg) -> None:
        await answer_discover(self._bus, msg, "shelly", self._discover)

    async def _discover(self, subnets: list[str]) -> dict:
        """Unicast /shelly probe across the configured IoT subnet(s) — works over
        routing, so DIDA needn't share the devices' L2. mDNS is added when it can."""
        from dida_core import mdns_browse

        already = {h.strip() for h in (self._cfg.get("hosts") or "").split(",") if h.strip()} if self._cfg else set()
        found: dict[str, str] = {}  # ip -> label
        try:
            for name, ip in await mdns_browse("_shelly._tcp.local.", 4.0):
                found.setdefault(ip, name)
        except Exception as exc:
            log.debug("shelly mdns failed: %s", exc, exc_info=True)
        for ip, label in await self._scan(subnet_hosts(subnets)):
            found.setdefault(ip, label)
        return {"devices": [
            {"label": f"{lbl} ({ip})", "appendCsv": {"hosts": ip}}
            for ip, lbl in sorted(found.items(), key=lambda kv: kv[0]) if ip not in already
        ]}

    async def _scan(self, hosts: list[str]) -> list[tuple[str, str]]:
        import aiohttp

        async def probe(ip: str) -> tuple[str, str] | None:
            try:
                async with self._session.get(  # type: ignore[union-attr]
                    f"http://{ip}/shelly", timeout=aiohttp.ClientTimeout(total=1.2)
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        return ip, str(d.get("id") or d.get("model") or "shelly")
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return None
            return None

        return await probe_hosts(hosts, probe)

    async def _rpc(self, ip: str, method: str, params: dict | None = None) -> dict:
        import aiohttp

        body: dict = {"id": 1, "method": method}
        if params is not None:
            body["params"] = params
        async with self._session.post(  # type: ignore[union-attr]
            f"http://{ip}/rpc", json=body, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result", {})

    async def _run_device(self, ip: str) -> None:
        backoff = 1
        while True:
            try:
                await self._sync_and_listen(ip)
                backoff = 1
            except asyncio.CancelledError:
                self._mark_down(ip)
                raise
            except Exception as exc:
                self._mark_down(ip)
                log.warning("shelly %s: %s (retry in %ds)", ip, exc, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _mark_down(self, ip: str) -> None:
        for dev_id, dev in self._devices.items():
            if dev.ip == ip:
                self._live.discard(dev_id)

    async def _sync_and_listen(self, ip: str) -> None:
        import aiohttp

        info = await self._rpc(ip, "Shelly.GetDeviceInfo")
        dev_id = info.get("id") or slug(ip)
        cfg = await self._rpc(ip, "Shelly.GetConfig")
        dev_name = ((cfg.get("sys") or {}).get("device") or {}).get("name") or dev_id
        comp_names = {
            k: v["name"]
            for k, v in cfg.items()
            if ":" in k and isinstance(v, dict) and v.get("name")
        }
        self._devices[dev_id] = _Dev(ip, dev_id, dev_name, comp_names)
        self._live.add(dev_id)
        log.info("shelly %s connected: %s (%s)", ip, dev_name, info.get("model", "?"))

        status = await self._rpc(ip, "Shelly.GetStatus")
        self._emit(dev_id, status)

        # ws_connect takes ClientWSTimeout, NOT ClientTimeout — the wrong type
        # raised a DeprecationWarning on connect and a TypeError on the close path.
        async with self._session.ws_connect(  # type: ignore[union-attr]
            f"ws://{ip}/rpc", heartbeat=55, timeout=aiohttp.ClientWSTimeout(ws_close=10)
        ) as ws:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=300)
                except TimeoutError:
                    # No push for a while — reconcile so we can't drift silently.
                    self._emit(dev_id, await self._rpc(ip, "Shelly.GetStatus"))
                    continue
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if frame.get("method") in ("NotifyStatus", "NotifyFullStatus"):
                        self._emit(dev_id, frame.get("params") or {})
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        raise RuntimeError("websocket closed")  # -> reconnect + full resync

    def _emit(self, dev_id: str, status: dict) -> None:
        dev = self._devices.get(dev_id)
        if dev is None or self._bus is None:
            return
        ts = time.time_ns()
        for comp_key, comp_st in status.items():
            if not isinstance(comp_st, dict):
                continue
            ctype = comp_key.split(":", 1)[0]
            readings = component_updates(ctype, comp_st)
            if not readings:
                continue
            entity_id = f"{NAMESPACE}:{dev_id}:{slug(comp_key)}"
            name = dev.comp_names.get(comp_key) or dev.name
            # Diagnostic is per ENTITY (the dashboard files whole cards), so a
            # component is diagnostic only if it has NO primary capability — e.g.
            # wifi (signal only), not switch:0 (on_off + internal temp). Sticky:
            # a partial NotifyStatus carrying only the diagnostic field can't
            # flip an already-curated entity.
            if any(not diag for *_, diag in readings):
                self._curated.add(entity_id)
            entity_diag = entity_id not in self._curated
            for cap, value, unit, _ in readings:
                if self._last.get((entity_id, cap)) == value:
                    continue
                self._last[(entity_id, cap)] = value
                spawn(
                    self._bus.publish_state(
                        StateUpdate(
                            entity_id=entity_id,
                            capability=cap,
                            value=value,
                            adapter=NAMESPACE,
                            ts_ns=ts,
                            unit=unit,
                            name=name,
                            # All components of one Shelly (switch_0, wifi, …) are
                            # facets of ONE physical device — group them into one card,
                            # named by the device's configured name (not its slug id).
                            device=dev_id,
                            device_name=dev.name,
                            diagnostic=entity_diag,
                        )
                    ),
                    log=log,
                    name=f"publish {entity_id}/{cap}",
                )

    async def handle_command(self, command: Command) -> None:
        if self._session is None:
            raise CommandRejected("not started")
        try:
            _, dev_id, comp = command.entity_id.split(":", 2)
            ctype, cid_s = comp.rsplit("_", 1)
            cid = int(cid_s)
        except (ValueError, IndexError) as exc:
            raise CommandRejected("unparseable entity id") from exc
        dev = self._devices.get(dev_id)
        if dev is None:
            raise CommandRejected(f"unknown device {dev_id}")
        try:
            validate_command(command.capability, command.command)
            method, params = command_to_rpc(ctype, cid, command.capability, command.command, command.args)
        except (CapabilityError, KeyError, ValueError) as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        try:
            await self._rpc(dev.ip, method, params)
        except Exception as exc:
            raise CommandRejected(f"{method} on {dev.ip} failed: {exc}") from exc

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        # Let the cancellations settle BEFORE closing the session, else the
        # per-device tasks are still holding websockets/requests on it and aiohttp
        # logs "Unclosed connection"/"Task was destroyed but it is pending".
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
