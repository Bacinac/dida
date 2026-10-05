from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityKind,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    answer_discover,
    probe_hosts,
    set_reachable,
    slug,
    subnet_hosts,
)
from home_core.tasks import spawn

from dida_adapter_esphome.mapping import (
    NAMESPACE,
    EntityMap,
    decode_state,
    is_diagnostic,
    map_entity,
    send_command,
)

log = logging.getLogger("dida.adapter.esphome")

# NATS subjects (dida.state.<entity_id>) can't contain spaces/dots/wildcards.
# ESPHome device names are free-form ("PMIS Cleo Room"), so slugify both parts
# into a stable, subject-safe entity_id used identically for state and commands.
def _entity_id(device: str, object_id: str) -> str:
    d = slug(device, default="dev")
    o = slug(object_id, default="x")
    return f"{NAMESPACE}:{d}:{o}"


class DeviceConn:
    """One ESPHome node: its API client + the key->EntityMap it exposes."""

    def __init__(self, name: str, client) -> None:
        self.name = name
        self.client = client
        self.entities: dict[int, EntityMap] = {}
        self.reconnect = None


class EsphomeAdapter:
    """Bridges ESPHome nodes (native API) onto the DIDA bus. One persistent,
    auto-reconnecting connection per node — a node reboot/Wi-Fi blip only blanks
    that node, and the whole adapter is isolated in its own container."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._azc = None
        self._cfg: AdapterConfig | None = None
        self._conns: dict[str, DeviceConn] = {}     # key -> DeviceConn
        self._sig: dict[str, str] = {}              # key -> config signature
        self._routing: dict[str, tuple[DeviceConn, EntityMap]] = {}  # entity_id -> (conn, em)
        self._state: dict[tuple[str, str], object] = {}             # (entity_id, cap) -> value
        self._unit: dict[tuple[str, str], str | None] = {}          # (entity_id, cap) -> unit
        self._unexposed: set[str] = set()   # entity_ids the user turned off (don't publish state)
        # Per-node liveness, keyed by device_key (slug), surfaced to the UI so a
        # node that can't connect fails LOUD (why) instead of a silent "Connecting…".
        self._status: dict[str, dict] = {}  # slug -> {state, code, reason, since, entities, host, name}
        self._reach: dict[str, bool] = {}   # slug -> last reachability we published (edge-trigger)

    @staticmethod
    def _node_key(name_or_host: str) -> str:
        """device_key the registry/API use — slug of the node's name-or-host.
        Mirrors `_entity_id`'s device slug and the API's `_esphome_key`."""
        return slug(name_or_host, default="dev")

    @staticmethod
    def _classify_error(err: BaseException) -> str:
        """Map a connect exception to a short, stable reason code the UI localizes."""
        name = type(err).__name__.lower()
        text = f"{name} {err}".lower()
        if "encrypt" in text or "noise" in text or "psk" in text or "handshake" in text:
            return "encryption"
        if "resolve" in text or "resolv" in text:
            return "unresolved"
        if "timeout" in text or "socket" in text or "connect" in text or "unreachable" in text:
            return "unreachable"
        return "error"

    def _read_config_raw(self) -> str:
        """Node JSON from the DB (Settings → Adapters), decrypted."""
        return (self._cfg.get("config") if self._cfg else "").strip()

    @staticmethod
    def _parse_config(raw: str) -> list[dict] | None:
        """Parsed node list, [] for a genuinely EMPTY config, None for a BROKEN
        one. The distinction is the fail-loud guard: a typo saved in the textarea
        (or an undecryptable blob after a key rotation) must NOT read as "no
        nodes" — that would silently disconnect the whole ESPHome estate."""
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, list):
            return None
        return [d for d in data if isinstance(d, dict) and d.get("host")]

    async def start(self, bus: Bus) -> None:
        from zeroconf.asyncio import AsyncZeroconf

        self._bus = bus
        self._cfg = AdapterConfig("esphome", self.broker)
        self._azc = AsyncZeroconf()
        await bus.nc.subscribe("dida.discover.esphome", cb=self._on_discover)
        await bus.nc.subscribe("dida.esphome.ctl", cb=self._on_ctl)
        # Supervise: spawn/respawn/cancel a per-node connection as the UI list
        # changes (keyed by name + content) → add/remove/edit nodes, no restart.
        while True:
            try:
                await self._cfg.load()
                parsed = self._parse_config(self._read_config_raw())
                if parsed is None:
                    # Broken config JSON: keep the CURRENT node set running and
                    # scream — tearing everything down over a typo is the outage.
                    log.error("esphome: config JSON invalid — keeping current %d node(s)",
                              len(self._conns))
                    self.status.error("config JSON invalid")
                    await asyncio.sleep(10)
                    continue
                desired: dict[str, dict] = {}
                for dev in parsed:
                    desired[dev.get("name") or dev["host"]] = dev
                for key, dev in desired.items():
                    sig = json.dumps(dev, sort_keys=True)
                    if self._sig.get(key) != sig:
                        await self._drop_device(key)
                        await self._add_device(key, dev)
                        self._sig[key] = sig
                for key in set(self._conns) - set(desired):
                    await self._drop_device(key)
                # Refresh the user's expose choices; re-emit cached state for any
                # entity just turned back on so the UI fills without waiting.
                prev = self._unexposed
                self._unexposed = await self._load_unexposed()
                if prev - self._unexposed:
                    self._republish(prev - self._unexposed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("esphome: supervise loop error")
                self.status.error(str(exc) or "supervise error")  # fail loud, don't leave a stale badge
            await asyncio.sleep(10)

    async def _load_unexposed(self) -> set[str]:
        """entity_ids the user has turned off (exposed=false) — skip publishing them."""
        try:
            rows = await self.broker.call("entities", own=True)
            return {r["entity_id"] for r in rows if not r["exposed"]}
        except Exception as exc:
            # Keep the last-known expose set, but don't do it silently — a dead
            # query here means expose toggles stop taking effect.
            log.warning("esphome: could not reload expose set, keeping %d cached: %s", len(self._unexposed), exc, exc_info=True)
            return self._unexposed

    def _republish(self, entity_ids: set[str]) -> None:
        ts = time.time_ns()
        for (eid, cap), value in list(self._state.items()):
            if eid not in entity_ids:
                continue
            route = self._routing.get(eid)
            em = route[1] if route else None
            spawn(self._bus.publish_state(
                StateUpdate(entity_id=eid, capability=cap, value=value, adapter=NAMESPACE,
                            ts_ns=ts, unit=self._unit.get((eid, cap)),
                            name=(em.name or None) if em else None,
                            diagnostic=em.diagnostic if em else False, device=eid.split(":")[1]),
            ), log=log, name=f"republish {eid}")

    async def _on_discover(self, msg) -> None:
        await answer_discover(self._bus, msg, "esphome", self._discover)

    async def _on_ctl(self, msg) -> None:
        """Control channel (request/reply). action=status → per-node liveness so
        the UI can show online/connecting/offline/error(+reason) per card."""
        try:
            req = json.loads(msg.data) if msg.data else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        result: dict = {"error": "unknown action"}
        if req.get("action") == "status":
            result = {"nodes": self._status}
        if msg.reply and self._bus is not None:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())

    async def _discover(self, subnets: list[str]) -> dict:
        """mDNS (names, when DIDA shares the device L2) + a unicast port-6053 probe
        across the configured subnet(s) (works over routing; name unknown → the
        user fills name + noise_psk)."""
        from dida_core import mdns_browse

        have = {d.get("host") for d in (self._parse_config(self._read_config_raw()) or [])}
        found: dict[str, str] = {}  # ip -> name ("" if only port-probed)
        try:
            for name, ip in await mdns_browse("_esphomelib._tcp.local.", 4.0):
                found[ip] = name
        except Exception as exc:
            log.debug("esphome mdns failed: %s", exc, exc_info=True)
        for ip in await self._scan(subnet_hosts(subnets)):
            found.setdefault(ip, "")
        return {"devices": [
            {"label": f"{name or 'ESPHome node'} ({ip})",
             "appendJson": {"config": {"name": name, "host": ip, "noise_psk": ""}}}
            for ip, name in sorted(found.items()) if ip not in have
        ]}

    @staticmethod
    async def _scan(hosts: list[str]) -> list[str]:
        def api_port(timeout: float):
            async def probe(ip: str) -> str | None:
                try:
                    _r, w = await asyncio.wait_for(asyncio.open_connection(ip, 6053), timeout=timeout)
                    w.close()
                    return ip
                except (OSError, TimeoutError):
                    return None
            return probe

        # A node momentarily busy (serving another connection) can miss the first
        # fast probe; a second, more patient pass over the non-responders catches
        # it, so a live node isn't silently dropped from discovery.
        found = set(await probe_hosts(hosts, api_port(1.5), 128))
        misses = [ip for ip in hosts if ip not in found]
        if misses:
            found |= set(await probe_hosts(misses, api_port(3.0), 128))
        return sorted(found, key=ipaddress.ip_address)

    async def _add_device(self, key: str, dev: dict) -> None:
        from aioesphomeapi import APIClient, ReconnectLogic

        name = dev.get("name") or dev["host"]
        client = APIClient(
            dev["host"], int(dev.get("port", 6053)),
            dev.get("password") or "",
            noise_psk=dev.get("noise_psk") or None,
        )
        conn = DeviceConn(name, client)
        self._conns[key] = conn
        self._status[self._node_key(name)] = {
            "state": "connecting", "code": None, "reason": None,
            "since": time.time(), "entities": 0, "host": dev["host"],
            "name": dev.get("name") or None,
        }
        conn.reconnect = ReconnectLogic(
            client=client,
            on_connect=lambda c=conn: self._on_connect(c),
            on_disconnect=lambda expected, c=conn: self._on_disconnect(c, expected),
            on_connect_error=lambda err, c=conn: self._on_connect_error(c, err),
            zeroconf_instance=self._azc.zeroconf,
            name=name,
        )
        await conn.reconnect.start()
        log.info("esphome: node %s added", name)

    async def _set_status(self, name: str, **fields) -> None:
        key = self._node_key(name)
        st = self._status.setdefault(key, {})
        st.update(fields)
        st["since"] = time.time()
        self._update_badge()
        # Reachability is the connection verdict: online → reachable, offline/error
        # → not. Published only on a CHANGE (this method is also called just to bump
        # the entity count), so the device row and the timeline see one transition,
        # not a heartbeat.
        reachable = st.get("state") == "online"
        if self._reach.get(key) != reachable:
            self._reach[key] = reachable
            await set_reachable(self._bus, key, NAMESPACE, reachable,
                                detail="" if reachable else (st.get("reason") or st.get("code") or ""))

    def _update_badge(self) -> None:
        """Global adapter badge DERIVED from the per-node dict — never a stale
        'ok N node(s)' while every node is actually offline/error."""
        if not self._status:
            self.status.idle("no nodes configured")
            return
        online = sum(1 for s in self._status.values() if s.get("state") == "online")
        total = len(self._status)
        if online == total:
            self.status.ok(f"{total} node(s)")
        elif online:
            self.status.ok(f"{online}/{total} node(s) online")
        else:
            self.status.error(f"0/{total} nodes online")

    async def _drop_device(self, key: str) -> None:
        conn = self._conns.pop(key, None)
        self._sig.pop(key, None)
        self._status.pop(self._node_key(key), None)
        self._reach.pop(self._node_key(key), None)
        self._update_badge()
        if conn is None:
            return
        try:
            if conn.reconnect is not None:
                await conn.reconnect.stop()
        except Exception:
            log.debug("esphome: reconnect logic of a dropped node did not stop", exc_info=True)
            pass
        # Drop this node's command routes (stale routes would mis-dispatch) AND its
        # cached state/units — otherwise renames grow those dicts without bound.
        self._routing = {eid: r for eid, r in self._routing.items() if r[0] is not conn}
        prefix = f"{NAMESPACE}:{self._node_key(conn.name)}:"
        self._state = {k: v for k, v in self._state.items() if not k[0].startswith(prefix)}
        self._unit = {k: v for k, v in self._unit.items() if not k[0].startswith(prefix)}
        log.info("esphome: node %s removed", conn.name)

    async def _on_connect(self, conn: DeviceConn) -> None:
        try:
            entities, _services = await conn.client.list_entities_services()
        except Exception as exc:
            log.exception("list_entities failed for %s", conn.name)
            await self._set_status(conn.name, state="error",
                             code=self._classify_error(exc), reason=str(exc))
            return
        # The node's own friendly name (e.g. "pmis-bea-room") — the scan can't read
        # it over the VLAN, but device_info can once connected. Used as the card header.
        dev_name: str | None = None
        try:
            di = await conn.client.device_info()
            dev_name = getattr(di, "friendly_name", "") or getattr(di, "name", "") or None
        except Exception:
            log.debug("esphome: device info unreadable", exc_info=True)
            pass
        conn.entities = {}
        mapped = 0
        for info in entities:
            object_id = getattr(info, "object_id", "") or ""
            entity_id = _entity_id(conn.name, object_id)
            device_key = entity_id.split(":")[1]
            name = getattr(info, "name", "") or object_id.replace("_", " ").title()
            diag = is_diagnostic(info)
            em = map_entity(info)
            if em is None:
                # Unsupported type (camera, date, …): announce it (no caps) so the UI
                # lists it as a field DIDA can't expose yet — then skip routing/state.
                etype = type(info).__name__.removesuffix("Info").lower()
                await self._bus.publish_entity(EntityInfo(
                    entity_id=entity_id, adapter=NAMESPACE, capabilities=[], name=name,
                    device=device_key, device_name=dev_name, device_type=etype, diagnostic=diag))
                continue
            em.name = name
            em.diagnostic = diag
            conn.entities[em.key] = em
            self._routing[entity_id] = (conn, em)
            # Announce the entity (catalog) so it shows even before/without state
            # (e.g. a write-only press button) and carries its full capability set.
            await self._bus.publish_entity(EntityInfo(
                entity_id=entity_id, adapter=NAMESPACE, capabilities=em.caps, name=name,
                device=device_key, device_name=dev_name, device_type=em.etype, diagnostic=diag))
            mapped += 1
        conn.client.subscribe_states(lambda state, c=conn: self._on_state(c, state))
        await self._set_status(conn.name, state="online", code=None, reason=None,
                         entities=len(entities))
        log.info("connected %s — %d/%d entities mapped", conn.name, mapped, len(entities))

    async def _on_disconnect(self, conn: DeviceConn, expected: bool) -> None:
        # Unexpected drop of a previously-online node → OFFLINE (ReconnectLogic keeps
        # retrying). Expected (we're dropping/editing it) leaves status to _drop_device.
        # Guard against a late callback resurrecting a node we already dropped.
        if not expected and self._conns.get(conn.name) is conn:
            await self._set_status(conn.name, state="offline", code=None, reason=None)
        log.info("disconnected %s (expected=%s)", conn.name, expected)

    async def _on_connect_error(self, conn: DeviceConn, err: BaseException) -> None:
        # A connect attempt failed (bad/absent noise_psk, host unreachable, name
        # unresolved…). ReconnectLogic keeps retrying; we surface WHY so the card
        # shows a real reason instead of a perpetual "Connecting…".
        await self._set_status(conn.name, state="error",
                         code=self._classify_error(err), reason=str(err))
        log.warning("connect error %s: %s", conn.name, err)

    def _on_state(self, conn: DeviceConn, state) -> None:
        em = conn.entities.get(state.key)
        if em is None:
            return
        entity_id = _entity_id(conn.name, em.object_id)
        if entity_id in self._unexposed:
            return  # user turned this entity off — don't read/publish its state
        device_key = entity_id.split(":")[1]
        ts = time.time_ns()
        for cap, value, unit in decode_state(em, state):
            # Dedupe: ESPHome re-pushes states on its own cadence; only forward
            # actual changes so we don't flood the bus / history with repeats.
            # An event is never re-pushed, and two presses in a row are equal.
            if cap != CapabilityKind.BUTTON.value and self._state.get((entity_id, cap)) == value:
                continue
            self._state[(entity_id, cap)] = value
            self._unit[(entity_id, cap)] = unit
            spawn(self._bus.publish_state(
                StateUpdate(entity_id=entity_id, capability=cap, value=value,
                            adapter=NAMESPACE, ts_ns=ts, unit=unit, name=em.name or None,
                            diagnostic=em.diagnostic, device=device_key)
            ), log=log, name=f"publish {entity_id}/{cap}")

    async def handle_command(self, command: Command) -> None:
        route = self._routing.get(command.entity_id)
        if route is None:
            raise CommandRejected("unknown or disconnected entity")
        conn, em = route
        cur = self._state.get((command.entity_id, "on_off"))
        try:
            ok = await send_command(conn.client, em, command.command, dict(command.args),
                                    current_on=(cur is True))
        except Exception as exc:
            raise CommandRejected(f"device refused: {exc}") from exc
        if not ok:
            raise CommandRejected(f"{em.etype} has no {command.command}")

    async def stop(self) -> None:
        for conn in self._conns.values():
            if conn.reconnect is not None:
                await conn.reconnect.stop()
        self._conns.clear()
        self._sig.clear()
        if self._azc is not None:
            await self._azc.async_close()
