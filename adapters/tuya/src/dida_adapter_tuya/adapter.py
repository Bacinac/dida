from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    StateUpdate,
    set_reachable,
    slug,
    validate_command,
)
from home_core.tasks import spawn

from dida_adapter_tuya import cloud
from dida_adapter_tuya.mapping import decode, encode, unit_for

log = logging.getLogger("dida.adapter.tuya")

NAMESPACE = "tuya"

# Caps surfaced as diagnostic rather than primary tiles (kept consistent with
# the other adapters). Tuya rarely exposes these, but be ready.
DIAGNOSTIC_CAPS = frozenset({"voltage", "current", "frequency", "power_factor", "signal", "battery"})

# Cloud (Zigbee sub-device) poll cadence — slower than local: cloud calls draw on
# the IoT Core resource-pack quota, and locks/sensors change rarely.
_CLOUD_POLL = 60


def _dp_spec(spec: object) -> tuple[str, list | None, list | None, str | None]:
    """A device's dps entry is either a plain capability string, or a dict
    `{"cap","options","raw","power_dp"}`. For an enum, options/raw map Tuya's
    raw codes (e.g. "level_1") to friendly labels ("Level 1") positionally; a
    composite enum also names a `power_dp` so one selector spans power + level
    (option whose raw is null = "Off" -> power off; any other -> power on +
    that level). Mirrors the HA Off/L1/L2/L3 patio-heater selector."""
    if isinstance(spec, dict):
        return str(spec.get("cap")), spec.get("options"), spec.get("raw"), spec.get("power_dp")
    return str(spec), None, None, None


def _enum_decode(raw_val: object, options: list | None, raw: list | None) -> str:
    """Tuya raw enum code -> friendly option (pass through if unmapped)."""
    s = str(raw_val)
    if raw and options and s in raw:
        return options[raw.index(s)]
    return s


def _enum_encode(option: str, options: list | None, raw: list | None) -> str:
    """Friendly option -> Tuya raw enum code (pass through if unmapped)."""
    if raw and options and option in options:
        return raw[options.index(option)]
    return option


def _parse_devices(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    # A device is usable if it's a cloud sub-device (id + cloud flag) or a local
    # device (id + ip + key). Cloud devices carry no ip/key.
    return [
        d for d in data
        if isinstance(d, dict) and d.get("id")
        and (d.get("cloud") or (d.get("ip") and d.get("key")))
    ]


def _caps_of(dps: dict) -> list[str]:
    """DIDA capabilities a device's dps map exposes — for the UI's found-device badge."""
    caps: list[str] = []
    for spec in (dps or {}).values():
        cap = spec.get("cap") if isinstance(spec, dict) else spec
        if cap and str(cap) not in caps:
            caps.append(str(cap))
    return caps


def _enum_reading(dp_id, dps: dict, options, raw, power_dp) -> object | None:
    if power_dp is not None and not bool(dps.get(str(power_dp))):
        # Composite: "Off" when the power dp is off, else the level.
        return options[0] if options else "Off"
    if str(dp_id) in dps:
        return _enum_decode(dps[str(dp_id)], options, raw)
    return None


def _readings(dev: dict, dps: dict) -> list[tuple[str, object]]:
    readings: list[tuple[str, object]] = []
    for dp_id, spec in dev.get("dps", {}).items():
        cap, options, raw, power_dp = _dp_spec(spec)
        if cap == "enum":
            val = _enum_reading(dp_id, dps, options, raw, power_dp)
            if val is None:
                continue
            readings.append(("enum", val))
            if options:
                readings.append(("enum_options", json.dumps(options, ensure_ascii=False)))
        elif str(dp_id) in dps:
            value = decode(cap, dps[str(dp_id)])
            if value is not None:
                readings.append((cap, value))
    return readings


def _enum_writes(option: str, dp_id, options, raw, power_dp) -> tuple[dict[int, object], object]:
    """set_option: friendly option -> raw code."""
    if options and option not in options:
        raise ValueError(f"{option!r} not in {options}")
    raw_val = raw[options.index(option)] if (options and raw) else _enum_encode(option, options, raw)
    if power_dp is None:
        return {int(dp_id): raw_val}, option
    # composite Off/level: drive power + level together
    if raw_val is None:
        return {int(power_dp): False}, option
    return {int(power_dp): True, int(dp_id): raw_val}, option


class TuyaAdapter:
    """Bridges Tuya Wi-Fi devices onto the DIDA bus via the LOCAL protocol.

    Each device is polled (tinytuya is synchronous, so status runs in an
    executor) and gets an instant publish on command. Fully local — no cloud
    in the runtime path; only the per-device local_key is needed (configured in
    Settings → Adapters (DB)). Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self.broker = None
        self._bus: Bus | None = None
        self._handles: dict[str, object] = {}  # dev_id -> tinytuya.Device
        self._by_slug: dict[str, dict] = {}  # slug(name) -> device dict
        self._tasks: dict[str, asyncio.Task] = {}  # dev_id -> poll task
        self._task_sig: dict[str, str] = {}  # dev_id -> config signature (respawn on change)
        # Per-device lock: tinytuya.Device on a persistent socket is NOT thread-safe.
        # A poll status() and a command set_value() run in the executor; without
        # this lock they can interleave frames on the one socket and read each
        # other's replies (spurious command failures / wrong state).
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[tuple[str, str], object] = {}
        self._curated: set[str] = set()
        # After a command, ignore a poll that would clobber the just-set value:
        # Tuya's status() lags a cycle after set_value, so an immediate poll can
        # read the OLD value. (entity_id, cap) -> monotonic deadline.
        self._cmd_until: dict[tuple[str, str], float] = {}
        # Cloud-onboarding: full device dicts (incl. local keys) from the last
        # fetch, keyed by id. Keys live here server-side; the UI only ever sees a
        # sanitised view. "add" persists the chosen ones to the config blob.
        self._cloud_cache: dict[str, dict] = {}
        self._cloud = None  # shared tinytuya.Cloud for onboarding + cloud-device polling
        self._cloud_key: tuple | None = None  # creds the cached Cloud was built with
        self._seen: dict[str, float] = {}  # dev_id -> monotonic of last successful poll
        self._reach: dict[str, bool] = {}  # dev_id -> last reachability we published (edge-trigger)
        # Dedicated thread pool for the blocking tinytuya socket calls (status /
        # set_value / cloud). An unreachable device blocks its worker for the full
        # tinytuya timeout, so isolate them from the process-wide default executor
        # (asyncio.to_thread) — else a few dead Tuya devices could starve argon2
        # logins and every other off-loop task. Bounded, so it can't explode either.
        self._exec: ThreadPoolExecutor | None = None

    def _read_config_raw(self) -> str:
        """Device JSON from the DB (Settings → Adapters), decrypted."""
        return (self._cfg.get("config") if self._cfg else "").strip()

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._exec = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tuya-io")
        self._cfg = AdapterConfig("tuya", self.broker)
        await bus.nc.subscribe("dida.tuya.ctl", cb=self._on_ctl)
        # Supervise: spawn/respawn/cancel a per-device task as the UI device list
        # changes, so devices add/remove/update without restarting the adapter.
        while True:
            try:
                await self._cfg.load()
                desired = {d["id"]: d for d in _parse_devices(self._read_config_raw())}
                for did, dev in desired.items():
                    sig = json.dumps(dev, sort_keys=True)
                    task = self._tasks.get(did)
                    # Respawn on config change OR if the task died (an exception in
                    # _make_handle before the poll loop leaves it dead forever
                    # otherwise — the fault-isolation promise requires a restart).
                    if self._task_sig.get(did) != sig or (task is not None and task.done()):
                        self._drop_device(did)
                        poll = self._poll_cloud_device if dev.get("cloud") else self._poll_device
                        self._tasks[did] = spawn(poll(dev), log=log, name=f"tuya device {did}")
                        self._task_sig[did] = sig
                        # Grace: count the fresh task as live until its first poll
                        # window passes, so boot doesn't flash a red badge.
                        self._seen.setdefault(did, time.monotonic())
                for did in set(self._tasks) - set(desired):
                    self._drop_device(did)
                if desired:
                    # Badge from REACHABILITY, not the config count: a device is
                    # "live" if its poll succeeded within ~3 poll intervals.
                    poll = self._cfg.int("poll_seconds", 10) if self._cfg else 10
                    horizon = time.monotonic() - max(3 * poll, 3 * _CLOUD_POLL if any(
                        d.get("cloud") for d in desired.values()) else 3 * poll)
                    await self._publish_reach_verdicts(desired, horizon)
                    live = sum(1 for did in desired if self._seen.get(did, 0.0) > horizon)
                    total = len(desired)
                    if live == total:
                        n_cloud = sum(1 for d in desired.values() if d.get("cloud"))
                        self.status.ok(f"{total} device(s)" + (f" · {n_cloud} cloud" if n_cloud else ""))
                    elif live:
                        self.status.ok(f"{live}/{total} device(s) responding")
                    else:
                        self.status.error(f"0/{total} devices responding")
                elif (self._cfg.get("api_id") if self._cfg else ""):
                    self.status.idle("credentials set — fetch devices to add")
                else:
                    self.status.idle("no cloud credentials")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("tuya: supervise loop error")
                self.status.error(str(exc) or "supervise error")
            await asyncio.sleep(10)

    async def _on_ctl(self, msg) -> None:
        """Cloud-onboarding control channel (request/reply):
        - {action:"cloud_devices", subnets:[…]} → pull this account's Tuya devices
          from the cloud (keys + DP maps), locate each on the LAN, cache the full
          dicts, reply with a sanitised list (no keys reach the browser).
        - {action:"add", ids:[…]} → persist the chosen cached devices to config.
        """
        try:
            req = json.loads(msg.data) if msg.data else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        action = req.get("action")
        try:
            if action == "cloud_devices":
                result = await self._cloud_devices(req.get("subnets") or [])
            elif action == "add":
                result = await self._cloud_add([str(i) for i in (req.get("ids") or [])])
            elif action == "remove":
                result = await self._cloud_remove([str(k) for k in (req.get("keys") or [])])
            else:
                result = {"error": "unknown action"}
        except Exception as exc:
            log.warning("tuya ctl %s failed: %s", action, exc, exc_info=True)
            result = {"error": str(exc)}
        if msg.reply and self._bus is not None:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())

    async def _cloud_devices(self, subnets: list[str]) -> dict:
        api_id = (self._cfg.get("api_id") if self._cfg else "") or ""
        api_secret = (self._cfg.get("api_secret") if self._cfg else "") or ""
        region = (self._cfg.get("api_region") if self._cfg else "") or "eu"
        if not api_id or not api_secret:
            return {"error": "no_creds"}
        existing = {d["id"]: d for d in _parse_devices(self._read_config_raw())}
        devices = await cloud.fetch_devices(api_id, api_secret, region, subnets, existing=existing)
        self._cloud_cache = {d["id"]: d for d in devices}
        have = set(existing)
        return {"devices": [{
            "id": d["id"], "name": d["name"], "ip": d["ip"], "version": d["version"],
            "online": d["online"], "caps": _caps_of(d["dps"]), "unmapped": d["unmapped"],
            "has_key": bool(d["key"]), "already": d["id"] in have,
            "cloud": bool(d.get("cloud")),
        } for d in devices]}

    async def _cloud_add(self, ids: list[str]) -> dict:
        by_id = {d["id"]: d for d in _parse_devices(self._read_config_raw())}
        added = 0
        for did in ids:
            dev = self._cloud_cache.get(did)
            if dev is None:
                continue
            if dev.get("cloud"):
                if not dev.get("dps"):
                    continue  # nothing DIDA can map on this sub-device
                by_id[did] = {
                    "id": dev["id"], "name": dev["name"], "cloud": True,
                    "dps": dev["dps"], "key": dev.get("key", ""),
                }
                added += 1
            elif dev.get("ip") and dev.get("key"):
                by_id[did] = {
                    "id": dev["id"], "ip": dev["ip"], "key": dev["key"],
                    "version": dev["version"], "name": dev["name"], "dps": dev["dps"],
                }
                added += 1
        blob = json.dumps(list(by_id.values()), ensure_ascii=False, indent=2)
        await self.broker.call("store", key="config", value=blob)
        await self._cfg.load()  # supervise loop then spawns the new device tasks
        return {"ok": True, "added": added}

    async def _cloud_remove(self, keys: list[str]) -> dict:
        """Remove device(s) from the config + drop their entities and live state.

        The UI's remove-key is the entity's device_key: the bare slug once the
        adapter stamps `device=` on its updates, but the namespaced `tuya:<slug>`
        for entities registered before that (device_key still NULL → the UI groups
        on entity_id). Accept both forms."""
        def _bare(k: str) -> str:
            return k[len(NAMESPACE) + 1:] if k.startswith(f"{NAMESPACE}:") else k

        def _dslug(d: dict) -> str:
            return slug(d.get("name") or d["id"], default="dev")

        kset = {_bare(k) for k in keys}
        devs = _parse_devices(self._read_config_raw())
        removed_slugs = [_dslug(d) for d in devs if _dslug(d) in kset]
        kept = [d for d in devs if _dslug(d) not in kset]
        removed = len(devs) - len(kept)
        if removed == 0:
            # Fail loud: a remove that matched no device is a bug (stale/mismatched
            # key), not a success — surface it instead of a silent {removed: 0}.
            return {"error": f"no tuya device matches {sorted(kset)}"}
        blob = json.dumps(kept, ensure_ascii=False, indent=2)
        await self.broker.call("store", key="config", value=blob)
        await self._cfg.load()  # supervise loop then cancels the dropped device tasks
        # By entity_id too: an entity registered before the adapter stamped
        # `device=` still has a NULL device_key.
        await self.broker.call("forget", device_keys=removed_slugs,
                               entity_ids=[f"{NAMESPACE}:{s}" for s in removed_slugs])
        return {"ok": True, "removed": removed}

    async def _publish_reach_verdicts(self, desired: dict[str, dict], horizon: float) -> None:
        """Per-device reachability from the SAME horizon the badge uses: the
        adapter's own poll socket is the source of the verdict, edge-triggered."""
        for did, dev in desired.items():
            ok = self._seen.get(did, 0.0) > horizon
            if self._reach.get(did) != ok:
                self._reach[did] = ok
                await set_reachable(
                    self._bus, slug(dev.get("name") or did, default="dev"),
                    NAMESPACE, ok, detail="" if ok else "no reply to polls")

    def _lock_for(self, did: str) -> asyncio.Lock:
        lock = self._locks.get(did)
        if lock is None:
            lock = self._locks[did] = asyncio.Lock()
        return lock

    def _drop_device(self, did: str) -> None:
        task = self._tasks.pop(did, None)
        if task is not None:
            task.cancel()
        self._task_sig.pop(did, None)
        self._reach.pop(did, None)
        self._locks.pop(did, None)
        handle = self._handles.pop(did, None)
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()  # type: ignore[attr-defined]
        self._by_slug = {s: d for s, d in self._by_slug.items() if d.get("id") != did}

    def _make_handle(self, dev: dict):
        import tinytuya

        h = tinytuya.Device(dev["id"], dev["ip"], dev["key"])
        h.set_version(float(dev.get("version", 3.3)))
        h.set_socketTimeout(5)
        h.set_socketPersistent(True)
        return h

    async def _poll_device(self, dev: dict) -> None:
        loop = asyncio.get_running_loop()
        dev_slug = slug(dev.get("name") or dev["id"], default="dev")
        self._by_slug[dev_slug] = dev
        self._handles[dev["id"]] = await loop.run_in_executor(self._exec, self._make_handle, dev)
        poll = self._cfg.int("poll_seconds", 10) if self._cfg else 10
        log.info("tuya device %s (%s) polling every %ds", dev.get("name", "?"), dev["ip"], poll)
        while True:
            try:
                handle = self._handles[dev["id"]]
                async with self._lock_for(dev["id"]):
                    data = await loop.run_in_executor(self._exec, handle.status)
                if isinstance(data, dict) and isinstance(data.get("dps"), dict):
                    self._emit(dev, dev_slug, data["dps"])
                elif isinstance(data, dict) and data.get("Error"):
                    log.warning("tuya %s: %s", dev.get("name", dev["ip"]), data.get("Error"))
            except Exception as exc:
                log.warning("tuya %s poll: %s", dev.get("name", dev["ip"]), exc, exc_info=True)
            await asyncio.sleep(self._cfg.int("poll_seconds", 10) if self._cfg else 10)

    def _emit(self, dev: dict, dev_slug: str, dps: dict) -> None:
        if self._bus is None:
            return
        entity_id = f"{NAMESPACE}:{dev_slug}"
        name = dev.get("name") or dev["id"]
        ts = time.time_ns()
        readings = _readings(dev, dps)
        if not readings:
            return
        if any(cap not in DIAGNOSTIC_CAPS for cap, _ in readings):
            self._curated.add(entity_id)
        entity_diag = entity_id not in self._curated
        now = time.monotonic()
        for capability, value in readings:
            key = (entity_id, capability)
            if self._last.get(key) == value:
                continue
            # Don't let a lagging poll overwrite a value we just commanded.
            if now < self._cmd_until.get(key, 0.0) and self._last.get(key) is not None:
                continue
            self._last[key] = value
            spawn(
                self._bus.publish_state(
                    StateUpdate(
                        entity_id=entity_id,
                        capability=capability,
                        value=value,
                        adapter=NAMESPACE,
                        ts_ns=ts,
                        unit=unit_for(capability),
                        name=name,
                        device=dev_slug,
                        device_name=dev.get("name"),
                        diagnostic=entity_diag,
                    )
                ), log=log, name=f"publish {entity_id}/{capability}",
            )
        self._seen[dev["id"]] = now

    async def _ensure_cloud(self):
        """Shared tinytuya.Cloud (auth token auto-refreshes on use). None if no
        creds. Cached BY credentials — editing api_id/secret/region in the UI
        invalidates the client instead of silently keeping the stale one."""
        aid = (self._cfg.get("api_id") if self._cfg else "") or ""
        sec = (self._cfg.get("api_secret") if self._cfg else "") or ""
        reg = (self._cfg.get("api_region") if self._cfg else "") or "eu"
        if not aid or not sec:
            self._cloud = None
            self._cloud_key = None
            return None
        key = (aid, sec, reg)
        if self._cloud is not None and self._cloud_key == key:
            return self._cloud
        import tinytuya
        loop = asyncio.get_running_loop()
        self._cloud = await loop.run_in_executor(
            self._exec, lambda: tinytuya.Cloud(apiRegion=reg, apiKey=aid, apiSecret=sec)
        )
        self._cloud_key = key
        return self._cloud

    async def _poll_cloud_device(self, dev: dict) -> None:
        """Poll a Zigbee sub-device (lock/keypad/sensor) via the Tuya cloud — the
        only way to read it, since it's sleepy and unreachable on the LAN."""
        loop = asyncio.get_running_loop()
        dev_slug = slug(dev.get("name") or dev["id"], default="dev")
        self._by_slug[dev_slug] = dev
        log.info("tuya cloud device %s polling every %ds", dev.get("name", "?"), _CLOUD_POLL)
        while True:
            try:
                c = await self._ensure_cloud()
                if c is not None:
                    st = await loop.run_in_executor(self._exec, c.getstatus, dev["id"])
                    res = st.get("result") if isinstance(st, dict) else None
                    if isinstance(res, list):
                        self._emit_cloud(dev, dev_slug, {x.get("code"): x.get("value")
                                                     for x in res if isinstance(x, dict)})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("tuya cloud %s poll: %s", dev.get("name", dev["id"]), exc, exc_info=True)
            await asyncio.sleep(_CLOUD_POLL)

    def _emit_cloud(self, dev: dict, dev_slug: str, status: dict) -> None:
        if self._bus is None:
            return
        entity_id = f"{NAMESPACE}:{dev_slug}"
        name = dev.get("name") or dev["id"]
        ts = time.time_ns()
        readings = [
            (cap, cloud.decode_cloud(cap, status[code]))
            for code, cap in dev.get("dps", {}).items() if code in status
        ]
        readings = [(c, v) for c, v in readings if v is not None]
        if any(cap not in DIAGNOSTIC_CAPS for cap, _ in readings):
            self._curated.add(entity_id)
        entity_diag = entity_id not in self._curated
        for cap, value in readings:
            key = (entity_id, cap)
            if self._last.get(key) == value:
                continue
            self._last[key] = value
            # diagnostic is per ENTITY (sticky _curated), same as the local path —
            # per-cap OR-ing here made a curated lock's battery flap the card.
            spawn(self._bus.publish_state(StateUpdate(
                entity_id=entity_id, capability=cap, value=value, adapter=NAMESPACE,
                ts_ns=ts, unit=unit_for(cap), name=name, device=dev_slug,
                device_name=dev.get("name"),
                diagnostic=entity_diag)), log=log, name=f"publish {entity_id}/{cap}")
        self._seen[dev["id"]] = time.monotonic()

    async def _cloud_command(self, dev: dict, command: Command) -> None:
        """Send a command to a cloud sub-device. The lock unlocks via the door-lock
        ticket flow; other caps go through the generic cloud sendcommand."""
        cap = command.capability
        if cap == "binary":
            raise CommandRejected("read-only (door contact / alarm)")
        if cap == "lock":
            if command.command == "unlock":
                await self._lock_open(dev)
            else:
                log.info("tuya lock %s: remote lock not supported (auto-locks)", dev.get("name"))
            return
        c = await self._ensure_cloud()
        if c is None:
            raise CommandRejected("no cloud credentials")
        code = next((cd for cd, cc in dev.get("dps", {}).items() if cc == cap), None)
        if code is None:
            raise CommandRejected(f"no cloud code for {cap}")
        value = command.args.get("value")
        try:
            loop = asyncio.get_running_loop()
            r = await loop.run_in_executor(
                self._exec, lambda: c.sendcommand(dev["id"], [{"code": code, "value": value}])
            )
        except Exception as exc:
            raise CommandRejected(f"cloud command failed: {exc}") from exc
        if isinstance(r, dict) and r.get("success") is False:
            raise CommandRejected(f"cloud rejected: {r.get('msg')}")

    async def _lock_open(self, dev: dict) -> None:
        """Remote-unlock a Tuya smart lock via the cloud: request a password-ticket,
        then call the password-free open-door with it. Some locks/plans reject remote
        unlock (security)."""
        c = await self._ensure_cloud()
        if c is None:
            raise CommandRejected("no cloud credentials")
        did = dev["id"]

        def _open() -> dict:
            # Tuya residential-lock remote unlock (verified for jtmspro): a password
            # ticket, then a password-free door-operate under the smart-lock API.
            # cloudrequest with EXPLICIT action='POST' — it builds the URL correctly
            # (_tuyaplatform mangles the door-lock path) and POSTs even with an empty
            # body (a bare post={} is falsy → would fall back to GET → "uri path invalid").
            tk = c.cloudrequest(f"/v1.0/smart-lock/devices/{did}/password-ticket", action="POST", post={})
            ticket = (tk.get("result") or {}) if isinstance(tk, dict) else {}
            tid = ticket.get("ticket_id")
            if not tid:
                return {"success": False, "msg": tk.get("msg") if isinstance(tk, dict) else "no ticket"}
            return c.cloudrequest(
                f"/v1.0/smart-lock/devices/{did}/password-free/door-operate",
                action="POST", post={"ticket_id": tid, "open": True},
            )

        try:
            r = await asyncio.get_running_loop().run_in_executor(self._exec, _open)
        except Exception as exc:
            raise CommandRejected(f"remote unlock error: {exc}") from exc
        if not (isinstance(r, dict) and r.get("success")):
            raise CommandRejected(f"remote unlock failed: {r.get('msg') if isinstance(r, dict) else r}")
        log.info("tuya lock %s: remote unlock OK", dev.get("name"))

    async def handle_command(self, command: Command) -> None:
        if self._bus is None:
            raise CommandRejected("not started")
        dev_slug = command.entity_id[len(NAMESPACE) + 1:]
        dev = self._by_slug.get(dev_slug)
        if dev is None:
            raise CommandRejected("unknown device")
        if dev.get("cloud"):
            await self._cloud_command(dev, command)
            return
        cap = command.capability
        cmd = command.command
        # Tuya has no atomic toggle — resolve from last known state.
        if cap == "on_off" and cmd == "toggle":
            cmd = "turn_off" if self._last.get((command.entity_id, "on_off")) else "turn_on"
        # Reverse the dp map (capability -> dp id + enum spec).
        found = next(((dp, *_dp_spec(spec)) for dp, spec in dev.get("dps", {}).items()
                      if _dp_spec(spec)[0] == cap), None)
        if found is None:
            raise CommandRejected(f"no dp for {cap}")
        dp_id, _cap, options, raw, power_dp = found
        # Resolve the dp write(s) + the value we optimistically publish.
        try:
            validate_command(cap, command.command)
            if cap == "enum":
                writes, pub_val = _enum_writes(str(command.args.get("value")), dp_id, options, raw, power_dp)
            else:
                writes = {int(dp_id): encode(cap, cmd, command.args)}
                pub_val = decode(cap, writes[int(dp_id)])
        except (CapabilityError, KeyError, ValueError) as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        handle = self._handles.get(dev["id"])
        if handle is None:
            raise CommandRejected("device not connected")
        loop = asyncio.get_running_loop()
        try:
            # Same per-device lock as the poll loop: never let a command and a
            # status poll interleave frames on the one persistent socket.
            async with self._lock_for(dev["id"]):
                if len(writes) == 1:
                    (wdp, wval), = writes.items()
                    await loop.run_in_executor(self._exec, handle.set_value, wdp, wval)
                else:
                    await loop.run_in_executor(self._exec, handle.set_multiple_values, writes)
            # Instant local feedback; hold off poll-clobber for a couple cycles
            # while the device's reported status catches up. A cover open/close/stop
            # has no decodable echo value (pub_val None) — skip the optimistic
            # publish instead of shipping value=None into the engine's decoder.
            self._cmd_until[(command.entity_id, cap)] = time.monotonic() + 25
            if pub_val is None:
                return
            self._last[(command.entity_id, cap)] = pub_val
            await self._bus.publish_state(
                StateUpdate(
                    entity_id=command.entity_id,
                    capability=cap,
                    value=pub_val,
                    adapter=NAMESPACE,
                    ts_ns=time.time_ns(),
                    unit=unit_for(cap),
                    name=dev.get("name") or dev["id"],
                    device=dev_slug,
                    device_name=dev.get("name"),
                    diagnostic=command.entity_id not in self._curated,
                )
            )
        except Exception as exc:
            raise CommandRejected(f"{cap} failed: {exc}") from exc

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        # Await the cancellations BEFORE tearing down the executor/handles, else a
        # poll still running in a worker keeps the tinytuya socket open ("Task was
        # destroyed but it is pending" / a dangling socket).
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_sig.clear()
        for h in self._handles.values():
            with contextlib.suppress(Exception):
                h.close()  # type: ignore[attr-defined]
        self._handles.clear()
        if self._exec is not None:
            self._exec.shutdown(wait=False, cancel_futures=True)
            self._exec = None
