from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time

from dida_core import (
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    StateUpdate,
    set_reachable,
    slug,
    validate_command,
)
from dida_core.broker import BrokerError
from home_core.tasks import spawn

from dida_adapter_homekit.mapping import (
    READ_MAP,
    WRITE_MAP,
    encode_command,
    normalize_type,
    unit_for,
)

log = logging.getLogger("dida.adapter.homekit")

NAMESPACE = "homekit"
# Control plane (request/reply over NATS): the API asks the adapter — which is
# the HAP controller on the right network — to discover accessories and to pair
# one by its setup code. Pairing must run here (mDNS + the secure session live in
# this process), not in the API container.
CTL_SUBJECT = "dida.homekit.ctl"
# Events are the fast path, not the truth: the living-room FP2 flipped a zone to
# empty and no event ever arrived, so DIDA held "occupied" for most of an hour.
# Every watched characteristic is read back on this cadence and a divergence is
# corrected — and logged, because each one is an event that went missing.
RECONCILE_S = 30


def _normalize_pin(pin: str) -> str:
    """Accept '12345678' or '123-45-678' → canonical 'XXX-XX-XXX'."""
    digits = re.sub(r"[^0-9]", "", pin or "")
    if len(digits) == 8:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return pin


class _Reading:
    __slots__ = ("capability", "decode", "diagnostic", "entity_id", "name")

    def __init__(self, entity_id, name, capability, decode, diagnostic):
        self.entity_id = entity_id
        self.name = name
        self.capability = capability
        self.decode = decode
        self.diagnostic = diagnostic


class HomekitAdapter:
    """Bridges HomeKit (HAP-IP) accessories onto the DIDA bus via aiohomekit.

    DIDA is its own HAP controller; pairings (one per accessory, with this
    controller's keypair) live in Settings → Adapters (DB)
    and are minted once by `python -m dida_adapter_homekit.pair`. Characteristic
    change events stream in over a persistent secure session. Implements
    `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self.broker = None
        self._pairings: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # alias -> run task
        self._controller = None
        self._zc = None
        self._browser = None
        self._handles: dict[str, object] = {}  # alias -> aiohomekit pairing
        # (alias, aid, iid) -> _Reading ; and per-entity diagnostic + command targets
        self._readings: dict[tuple[str, int, int], _Reading] = {}
        self._curated: set[str] = set()
        self._writable: dict[tuple[str, str], tuple[str, int, int]] = {}
        self._last: dict[tuple[str, str], object] = {}
        self._last_at: dict[tuple[str, str], float] = {}
        self._connected: set[str] = set()  # aliases with a live session (drives the badge)
        self._reach: dict[str, bool] = {}

    async def _read_pairings(self) -> dict[str, dict]:
        """Pairings (adapter-managed, sealed): one blob under adapter_config — not a
        user-editable field, minted by the pairing flow."""
        try:
            raw = await self.broker.call("stored", key="pairings")
            data = json.loads(raw) if raw else {}
        except (BrokerError, json.JSONDecodeError) as exc:
            # This blob is LIVE HomeKit pairing state. Undecryptable (rotated key /
            # corrupt row) must show as a red badge, not read as "nothing paired".
            log.error("homekit: pairings blob unreadable (%s) — refusing to run empty", exc)
            self.status.error("pairings unreadable — key rotated?")
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    async def _save_pairings(self) -> None:
        await self.broker.call("store", key="pairings", value=json.dumps(self._pairings))

    async def start(self, bus: Bus) -> None:
        from aiohomekit import Controller
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        self._bus = bus
        self._pairings = await self._read_pairings()
        self._zc = AsyncZeroconf()
        # aiohomekit's IP transport reads accessories from a running _hap._tcp
        # browser on the shared zeroconf (HA provides one; standalone we make it).
        self._browser = AsyncServiceBrowser(
            self._zc.zeroconf,
            ["_hap._tcp.local.", "_hap._udp.local."],
            handlers=[lambda *a, **k: None],
        )
        self._controller = Controller(async_zeroconf_instance=self._zc)
        await self._controller.async_start()
        self._publish_status()
        # Control plane: the API drives discover/pair through this subject.
        await bus.nc.subscribe(CTL_SUBJECT, cb=self._on_ctl)
        log.info("homekit adapter up — %d pairing(s), control on %s", len(self._pairings), CTL_SUBJECT)
        for alias, data in self._pairings.items():
            self._tasks[alias] = spawn(self._run_pairing(alias, data), log=log, name=f"homekit {alias}")
        await asyncio.Event().wait()  # stay alive for control + characteristic pushes

    async def _on_ctl(self, msg) -> None:
        """Request/reply: {action: discover} | {action: pair, device_id, pin, name?}."""
        try:
            req = json.loads(msg.data)
            action = req.get("action")
            if action == "discover":
                result = await self._discover()
            elif action == "pair":
                result = await self._do_pair(
                    req.get("device_id", ""), req.get("pin", ""), req.get("name") or "",
                )
            else:
                result = {"error": f"unknown action {action!r}"}
        except Exception as exc:
            log.warning("homekit ctl failed: %s", exc, exc_info=True)
            result = {"error": str(exc)}
        if msg.reply:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())  # type: ignore[union-attr]

    async def _discover(self) -> dict:
        paired_ids = {
            str(d.get("AccessoryPairingID", "")).lower() for d in self._pairings.values()
        }
        devices = []
        try:
            async for disc in self._controller.async_discover(10):  # type: ignore[union-attr]
                d = disc.description
                unpaired = bool(int(getattr(d, "status_flags", 0)) & 0x01)
                devices.append({
                    "device_id": d.id,
                    "name": d.name,
                    "model": getattr(d, "model", "") or "",
                    "address": getattr(d, "address", "") or "",
                    # pairable = advertises unpaired AND we don't already have it
                    "pairable": unpaired and d.id.lower() not in paired_ids,
                })
        except Exception as exc:
            log.warning("homekit: discovery failed", exc_info=True)
            return {"error": f"discovery failed: {exc}"}
        return {"devices": devices}

    async def _do_pair(self, device_id: str, pin: str, name: str) -> dict:
        if not device_id or not pin:
            return {"error": "device_id and pin are required"}
        disc = await self._controller.async_find(device_id)  # type: ignore[union-attr]
        if disc is None:
            return {"error": "device not found on the network"}
        alias = slug(name) if name else slug(device_id)
        if alias in self._pairings:
            return {"error": f"alias {alias!r} already paired"}
        try:
            finish = await disc.async_start_pairing(alias)
            pairing = await finish(_normalize_pin(pin))
        except Exception as exc:
            log.warning("homekit: pairing failed", exc_info=True)
            return {"error": f"pairing failed: {exc}"}
        data = dict(pairing.pairing_data)
        data["name"] = name or data.get("name") or alias
        self._pairings[alias] = data
        await self._save_pairings()
        # Bring it online immediately — no restart.
        self._tasks[alias] = spawn(self._run_pairing(alias, data), log=log, name=f"homekit {alias}")
        self._publish_status()
        log.info("homekit paired %r (%s)", alias, device_id)
        return {"ok": True, "alias": alias, "name": data["name"]}

    def _publish_status(self) -> None:
        # Badge reflects CONNECTIVITY, not just the pairing count — "3 paired"
        # while all three are unreachable was a green lie (audit HK-2).
        n = len(self._pairings)
        if not n:
            self.status.idle("no accessories paired")
            return
        k = len(self._connected)
        if k == n:
            self.status.ok(f"{n} accessor{'y' if n == 1 else 'ies'} connected")
        elif k:
            self.status.ok(f"{k}/{n} connected")
        else:
            self.status.error(f"0/{n} accessories reachable")

    async def _run_pairing(self, alias: str, data: dict) -> None:
        while True:
            try:
                await self._sync(alias, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.discard(alias)
                self._publish_status()
                await self._set_reach(alias, False, str(exc))
                log.warning("homekit %s: %s (retry in 15s)", alias, exc, exc_info=True)
                await asyncio.sleep(15)

    async def _set_reach(self, alias: str, reachable: bool, detail: str = "") -> None:
        if self._reach.get(alias) != reachable:
            self._reach[alias] = reachable
            await set_reachable(self._bus, slug(alias), NAMESPACE, reachable, detail=detail)

    async def _follow_endpoint(self, alias: str, data: dict) -> None:
        # aiohomekit sees an accessory re-advertise on a new address or port but keeps
        # dialling the one in the pairing data. HAP accessories pick a fresh port on
        # every reboot, so without this rewrite a rebooted accessory is lost for good.
        from aiohomekit.exceptions import AccessoryNotFoundError

        try:
            disc = await self._controller.async_find(data["AccessoryPairingID"], timeout=10)  # type: ignore[union-attr]
        except AccessoryNotFoundError:
            return
        d = disc.description
        if d.address == data.get("AccessoryIP") and d.port == data.get("AccessoryPort") \
                and list(d.addresses) == data.get("AccessoryIPs", [data.get("AccessoryIP")]):
            return
        log.warning("homekit %s: accessory moved from %s:%s to %s:%s — pairing updated",
                    alias, data.get("AccessoryIP"), data.get("AccessoryPort"), d.address, d.port)
        data["AccessoryIP"] = d.address
        data["AccessoryIPs"] = list(d.addresses)
        data["AccessoryPort"] = d.port
        await self._save_pairings()

    async def _sync(self, alias: str, data: dict) -> None:
        await self._follow_endpoint(alias, data)
        # Close the previous pairing handle before minting a new one: each retry
        # otherwise leaks a half-open HAP session object (audit HK-4).
        old = self._handles.pop(alias, None)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()  # type: ignore[attr-defined]
        pairing = self._controller.load_pairing(alias, dict(data))  # type: ignore[union-attr]
        self._handles[alias] = pairing
        accessories = await pairing.list_accessories_and_characteristics()
        self._build_entities(alias, accessories)
        self._connected.add(alias)
        self._publish_status()
        await self._set_reach(alias, True)

        watch = [(a, i) for (al, a, i) in self._readings if al == alias]
        # Initial snapshot, then live subscription.
        if watch:
            try:
                initial = await pairing.get_characteristics(watch)
                self._emit(alias, initial)
            except Exception as exc:
                log.warning("homekit %s initial read: %s", alias, exc, exc_info=True)
            pairing.dispatcher_connect(lambda chars, _a=alias: self._emit(_a, chars))
            await pairing.subscribe(watch)
        log.info("homekit %s connected: %d characteristics watched", alias, len(watch))

        # Hold the connection open; aiohomekit pushes events + auto-reconnects.
        while pairing.is_connected or pairing.is_available:
            await asyncio.sleep(RECONCILE_S)
            if watch:
                self._reconcile(alias, await pairing.get_characteristics(watch))
        raise RuntimeError("pairing disconnected")

    def _reconcile(self, alias: str, chars: dict) -> None:
        now = time.time()
        for key, payload in chars.items():
            aid, iid = key if isinstance(key, tuple) else (key.get("aid"), key.get("iid"))
            r = self._readings.get((alias, aid, iid))
            if r is None or not isinstance(payload, dict) or payload.get("status"):
                continue
            actual = r.decode(payload.get("value"))
            k = (r.entity_id, r.capability)
            if actual is None or k not in self._last or self._last[k] == actual:
                continue
            log.warning("homekit %s: %s %s is %r on the accessory but %r here for %.0f s — event missed, corrected",
                        alias, r.name, r.capability, actual, self._last[k], now - self._last_at.get(k, now))
        self._emit(alias, chars)

    def _build_entities(self, alias: str, accessories: list) -> None:
        from aiohomekit.model.characteristics import CharacteristicsTypes as Cc

        name_uuid = Cc.NAME
        acc_name = self._pairings.get(alias, {}).get("name") or alias
        for acc in accessories:
            aid = acc.get("aid")
            for svc in acc.get("services", []):
                chars = svc.get("characteristics", [])
                svc_name = next(
                    (c.get("value") for c in chars if normalize_type(c.get("type", "")) == name_uuid and c.get("value")),
                    None,
                )
                mapped = [
                    (c, READ_MAP[normalize_type(c.get("type", ""))])
                    for c in chars
                    if normalize_type(c.get("type", "")) in READ_MAP and "pr" in (c.get("perms") or [])
                ]
                if not mapped:
                    continue
                label = svc_name or f"{acc_name} {aid}-{svc.get('iid')}"
                entity_id = f"{NAMESPACE}:{slug(alias)}:{slug(label)}"
                if any(not diag for _c, (_cap, _dec, diag) in mapped):
                    self._curated.add(entity_id)
                for c, (cap, decode, _diag) in mapped:
                    self._readings[(alias, aid, c["iid"])] = _Reading(entity_id, label, cap, decode, _diag)
                # Command targets: writable target char in the same service.
                for c in chars:
                    nt = normalize_type(c.get("type", ""))
                    for cap, target in WRITE_MAP.items():
                        if nt == target and "pw" in (c.get("perms") or []):
                            self._writable[(entity_id, cap)] = (alias, aid, c["iid"])

    def _emit(self, alias: str, chars: dict) -> None:
        if self._bus is None:
            return
        ts = time.time_ns()
        # Friendly accessory name for the card header (not the alias slug).
        acc_name = (self._pairings.get(alias) or {}).get("name") or alias
        for key, payload in chars.items():
            aid, iid = key if isinstance(key, tuple) else (key.get("aid"), key.get("iid"))
            r = self._readings.get((alias, aid, iid))
            if r is None:
                continue
            # A HAP error read carries a non-zero "status" (and no usable "value").
            # Skip it loudly-ish rather than decoding a missing value into a false
            # state (e.g. lock=unlocked). None-safe decoders are the backstop.
            if isinstance(payload, dict):
                if payload.get("status"):
                    log.warning("homekit %s: characteristic %s/%s read error status=%s",
                                alias, aid, iid, payload.get("status"))
                    continue
                raw = payload.get("value")
            else:
                raw = payload
            value = r.decode(raw)
            if value is None or self._last.get((r.entity_id, r.capability)) == value:
                continue
            self._last[(r.entity_id, r.capability)] = value
            self._last_at[(r.entity_id, r.capability)] = time.time()
            spawn(
                self._bus.publish_state(
                    StateUpdate(
                        entity_id=r.entity_id,
                        capability=r.capability,
                        value=value,
                        adapter=NAMESPACE,
                        ts_ns=ts,
                        unit=unit_for(r.capability),
                        name=r.name,
                        # Group all of an accessory's services into one device card
                        # (the accessory alias = the entity_id's middle segment),
                        # named by the accessory's friendly name, not the alias slug.
                        device=slug(alias),
                        device_name=acc_name,
                        diagnostic=r.entity_id not in self._curated,
                    )
                )
            )

    async def handle_command(self, command: Command) -> None:
        if self._bus is None:
            raise CommandRejected("not started")
        target = self._writable.get((command.entity_id, command.capability))
        if target is None:
            raise CommandRejected(f"no writable target for {command.capability}")
        alias, aid, iid = target
        # HAP has no atomic toggle — resolve it from the last known on_off state.
        # encode_command maps a bare "toggle" to `command == "turn_on"` → False,
        # so an unresolved toggle always turned the accessory OFF.
        cmd = command.command
        if command.capability == "on_off" and cmd == "toggle":
            cur = self._last.get((command.entity_id, "on_off"))
            cmd = "turn_off" if cur is True else "turn_on"
        try:
            validate_command(command.capability, command.command)
            value = encode_command(command.capability, cmd, command.args)
        except (CapabilityError, KeyError, ValueError) as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        pairing = self._handles.get(alias)
        if pairing is None:
            raise CommandRejected("accessory not connected")
        try:
            await pairing.put_characteristics([(aid, iid, value)])
        except Exception as exc:
            raise CommandRejected(f"accessory refused: {exc}") from exc

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for pairing in self._handles.values():
            with contextlib.suppress(Exception):
                await pairing.close()  # type: ignore[attr-defined]
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._controller is not None:
            await self._controller.async_stop()
        if self._zc is not None:
            await self._zc.async_close()
        self._handles.clear()
