from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from urllib.parse import urlsplit

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    emit_journal,
    set_reachable,
    slug,
    validate_command,
)
from home_core.tasks import spawn

from dida_adapter_mqtt.mapping import (
    DIAGNOSTIC_CAPS,
    Expose,
    build_generic_updates,
    build_state_updates,
    command_to_mqtt,
    device_type_from_exposes,
    facet_meta,
    generic_command,
    parse_exposes,
    shape_networkmap,
)

log = logging.getLogger("dida.adapter.mqtt")

NAMESPACE = "mqtt"

# NATS subjects can't carry spaces/dots/wildcards, but z2m friendly_names can
# ("Kuhinja stropna"). Slug the id for the bus; keep the original friendly_name
# as the display name and to address the `<friendly>/set` topic on commands.


# zigbee2mqtt reports a refused command only on its own log topic, as prose:
#   {"level":"error","message":"z2m: Publish 'set' 'state' to 'Ceiling Upstairs'
#    failed: 'Error: ZCL command 0x60b6…/1 genOnOff.off(…) failed
#    (Data request failed with error: 'MAC_NO_ACK' (0xe9))'"}
# Nothing else says so: the device simply never reports a new state, so DIDA keeps
# showing the last one it knew and the person who pressed the button is told
# nothing at all. Measured on the Cabin installation — three lights, nine presses,
# not one word anywhere in DIDA.
_SET_FAILED = re.compile(r"Publish 'set'.*? to '([^']+)' failed:\s*(.*)", re.S)

# The raw tail is 400+ characters of ZCL internals. What a person needs is which
# device and why, in that order; the rest belongs in the log, not the timeline.
_REASONS = (
    ("MAC_NO_ACK", "device did not respond"),
    ("NO_NETWORK_ROUTE", "no route to the device"),
    ("MAC_CHANNEL_ACCESS_FAILURE", "the radio channel was busy"),
    ("MAC_TRANSACTION_EXPIRED", "the device did not wake in time"),
    ("Timeout", "timed out"),
)


def _failure_reason(tail: str) -> str:
    for token, human in _REASONS:
        if token.lower() in tail.lower():
            return human
    return (tail.strip().strip("'")[:120] or "rejected by zigbee2mqtt")


class MqttAdapter:
    """Bridges an MQTT broker onto the DIDA bus. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        # Broker/creds/prefix now come from the DB (Settings → Adapters); set live
        # by _apply() from the merged config.
        self._host = "mosquitto"
        self._port = 1883
        self._username: str | None = None
        self._password: str | None = None
        self._prefix = "zigbee2mqtt"
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._client = None
        # slug -> original friendly_name (display name + `<friendly>/set` routing)
        self._friendly: dict[str, str] = {}
        self._curated: set[str] = set()  # entity_ids that have a primary (non-diagnostic) cap
        # dev_slug -> ieee_address, learned from bridge/devices. Only the bridge
        # list carries it; a device seen first on a state topic has none until the
        # (retained) list arrives, which it does on every connect.
        self._native: dict[str, str] = {}
        # dev_slug -> {field: Expose}, learned from bridge/devices — types the
        # generic (non-semantic) facets and labels them.
        self._exposes: dict[str, dict[str, Expose]] = {}
        self._announced: set[str] = set()          # generic entity_ids already announced
        self._facet_bool: dict[str, bool] = {}      # feid -> last boolean-facet value (for `toggle`)
        self._dev_caps: dict[str, tuple[str, ...]] = {}  # dev_slug -> last-announced primary cap set
        self._dev_type: dict[str, str | None] = {}  # dev_slug -> z2m physical type (light/switch/cover/lock)
        self._typed: set[str] = set()               # dev_slugs whose main entity was announced WITH a type
        self._active_key: tuple | None = None
        self._consume_task: asyncio.Task | None = None
        # dev_slug -> last reachability we published, so a device going quiet is
        # reported once (z2m re-publishes availability retained + on every change).
        self._reach: dict[str, bool] = {}
        # zigbee2mqtt bridge request/response correlation: z2m echoes the
        # `transaction` we stamp on a request in its response on a sibling topic,
        # which the consume loop routes back to the waiting ctl call.
        self._pending: dict[str, asyncio.Future] = {}
        self._txn = 0
        # The mesh scan is slow and disruptive, and long enough to outlive a tunnel
        # request timeout, so it runs in the background and the UI reads the cached
        # result — a refresh kicks a new scan, a plain read returns what we have.
        self._netmap: dict | None = None
        self._netmap_ts = 0
        self._netmap_err = ""
        self._netmap_task: asyncio.Task | None = None
        # z2m's own availability setting, learned from the retained bridge/info so
        # the UI can show + toggle it without a manual config edit. None = unknown.
        self._z_avail: bool | None = None

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("mqtt", self.broker)
        # Zigbee onboarding + diagnostics ride the same broker: the UI reaches them
        # through this control channel, so a new device is paired and the mesh is
        # mapped from DIDA without ever opening the zigbee2mqtt console.
        await bus.nc.subscribe("dida.mqtt.ctl", cb=self._on_ctl)
        # Supervise: (re)connect whenever the UI changes the broker/creds, or the
        # connection drops. The loop owns the lifecycle; _consume runs as a task.
        while True:
            try:
                await self._cfg.load()
                key = self._conn_key()
                dead = self._consume_task is not None and self._consume_task.done()
                if key != self._active_key or (key is not None and dead):
                    await self._apply(key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("mqtt: supervise loop error")
                self.status.error(str(exc) or "connection failed")
            await asyncio.sleep(10)

    def _conn_key(self) -> tuple | None:
        url = (self._cfg.get("mqtt_url") if self._cfg else "").strip()
        if not url:
            return None
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            return None
        username = (self._cfg.get("username") or None) if self._cfg else None
        password = (self._cfg.get("password") or None) if self._cfg else None
        prefix = (self._cfg.get("topic_prefix") if self._cfg else "") or "zigbee2mqtt"
        return (host, parts.port or 1883, username, password, prefix)

    async def _apply(self, key: tuple | None) -> None:
        import aiomqtt

        if self._consume_task is not None:
            self._consume_task.cancel()
            # Await the teardown so the old consume loop can't briefly coexist
            # with (and steal frames from) the new client. gather(return_exceptions)
            # absorbs the child's CancelledError without masking our own.
            await asyncio.gather(self._consume_task, return_exceptions=True)
            self._consume_task = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None
        if key is None:
            self._active_key = key
            self.status.idle("no broker configured")
            log.info("mqtt: no broker configured — idle (set it in Settings → Adapters)")
            return
        host, port, username, password, prefix = key
        self.status.connecting(f"{host}:{port}")
        client = aiomqtt.Client(
            hostname=host, port=port, username=username, password=password,
        )
        try:
            await client.__aenter__()
            await client.subscribe(f"{prefix}/#")
        except Exception as exc:
            # Do NOT pin _active_key on a failed connect: leaving it unchanged means
            # key != _active_key still holds, so supervise retries next tick instead
            # of wedging forever (the bug that made a broker-down-at-boot permanent).
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            self.status.error(f"connect failed: {exc}" if str(exc) else "connect failed")
            log.warning("mqtt: connect to %s:%d failed: %s (will retry)", host, port, exc, exc_info=True)
            return
        # Connected — commit the new connection state.
        self._client = client
        self._host, self._port, self._username, self._password, self._prefix = key
        self._active_key = key
        self.status.ok(f"{self._host}:{self._port}")
        log.info("mqtt connected %s:%d, subscribed %s/#", self._host, self._port, self._prefix)
        self._consume_task = spawn(self._consume(), log=log, name="mqtt consume loop")

    async def _ingest_devices(self, raw: object) -> None:
        """zigbee2mqtt publishes the full device list (retained) on
        `<prefix>/bridge/devices`. Learn friendly_name -> slug + exposes, and
        ANNOUNCE the full facet catalog (every field the device declares — editable
        controls and read-only sensors) so it appears the instant we (re)connect —
        not only when the device next reports. bridge/devices is retained, so this
        is the refresh: reconnect and the catalog is rebuilt immediately."""
        try:
            devs = json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            log.debug("mqtt: undecodable bridge/devices payload")
            return
        if not isinstance(devs, list) or self._bus is None:
            return
        ts = time.time_ns()
        for d in devs:
            if not isinstance(d, dict):
                continue
            fn = d.get("friendly_name")
            if not isinstance(fn, str) or not fn:
                continue
            ds = slug(fn, default="dev")
            self._friendly[ds] = fn
            # The device's identity in the zigbee network, unchanged by a rename —
            # which is what tells the engine that a device reappearing under a new
            # slug is the SAME one, instead of a duplicate that leaves every rule
            # naming the old id silently dead.
            ieee = d.get("ieee_address")
            if isinstance(ieee, str) and ieee:
                self._native[ds] = ieee
            definition = d.get("definition")
            if not isinstance(definition, dict):
                continue
            exposes = parse_exposes(definition.get("exposes"))
            self._exposes[ds] = exposes
            self._dev_type[ds] = device_type_from_exposes(definition.get("exposes"))
            # If this device already reported state (primary entity announced) but
            # z2m's device list only now tells us its type, re-announce with it.
            if self._dev_type[ds] is not None and ds in self._dev_caps and ds not in self._typed:
                await self._announce_main(ds, self._dev_caps[ds])
            for field, expose in exposes.items():
                meta = facet_meta(field, expose)
                if meta is None:
                    continue
                cap, options_cap, options_val, name, _unit, category = meta
                feid = f"{NAMESPACE}:{ds}:{slug(field, default='f')}"
                if feid in self._announced:
                    continue
                self._announced.add(feid)
                # Primary controls stay visible (diagnostic=False → exposed by
                # default); settings & diagnostics start hidden, opt-in per field.
                diag = category != "control"
                caps = [cap] + ([options_cap] if options_cap else [])
                await self._bus.publish_entity(EntityInfo(
                    entity_id=feid, adapter=NAMESPACE, capabilities=caps,
                    name=name, device=ds, device_name=fn, diagnostic=diag, category=category,
                ))
                if options_cap:  # static control metadata (option list / bounds)
                    await self._bus.publish_state(StateUpdate(
                        entity_id=feid, capability=options_cap, value=options_val,
                        adapter=NAMESPACE, ts_ns=ts, name=name, diagnostic=diag,
                        category=category, device=ds,
                    ))

    async def _ingest_log(self, raw: object) -> None:
        """Turn zigbee2mqtt's own error log into an event on the device it names.

        Only refused commands: everything else on this topic is zigbee2mqtt
        talking about itself, which belongs in its container log and not on a
        device's timeline."""
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(entry, dict) or entry.get("level") != "error":
            return
        m = _SET_FAILED.search(str(entry.get("message") or ""))
        if not m:
            return
        friendly, tail = m.group(1), m.group(2)
        dev_slug = slug(friendly, default="dev")
        entity_id = f"{NAMESPACE}:{dev_slug}"
        reason = _failure_reason(tail)
        log.warning("mqtt: %s refused the command — %s", friendly, reason)
        await emit_journal(
            self._bus, "command_failed", entity_id=entity_id, device_key=dev_slug,
            source=f"adapter:{NAMESPACE}", severity="error",
            message=f"{friendly}: {reason}",
        )

    async def _zreq(self, kind: str, payload: dict, timeout_s: float) -> dict:
        """Issue a zigbee2mqtt bridge request and wait for its matching response.

        z2m answers asynchronously on `bridge/response/<kind>`; we correlate by the
        `transaction` we stamp, so concurrent requests never cross wires and a
        never-arriving answer times out loud instead of hanging the ctl call."""
        if self._client is None:
            raise RuntimeError("broker not connected")
        self._txn += 1
        txn = f"dida{self._txn}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[txn] = fut
        try:
            await self._client.publish(
                f"{self._prefix}/bridge/request/{kind}",
                json.dumps({**payload, "transaction": txn}),
            )
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(txn, None)

    async def _scan_netmap(self, routes: bool) -> None:
        """Run one mesh scan and cache the shaped result. Errors are cached too, so
        a failed scan says why on the next read instead of showing a stale map."""
        try:
            raw = await self._zreq("networkmap", {"type": "raw", "routes": routes}, 180)
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            slug_by_ieee = {ieee: ds for ds, ieee in self._native.items()}
            self._netmap = shape_networkmap(data.get("value"), slug_by_ieee)
            self._netmap_ts = time.time_ns()
            self._netmap_err = ""
        except TimeoutError:
            self._netmap_err = "zigbee2mqtt did not finish the mesh scan in time"
        except (RuntimeError, ValueError) as exc:
            self._netmap_err = str(exc) or "mesh scan failed"

    def _ingest_info(self, raw: object) -> None:
        """zigbee2mqtt publishes its full config (retained) on bridge/info. We keep
        just whether availability is on, so the UI can show + toggle it."""
        try:
            info = json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(info, dict):
            return
        cfg = info.get("config") if isinstance(info.get("config"), dict) else {}
        avail = cfg.get("availability")
        if isinstance(avail, dict):
            self._z_avail = bool(avail.get("enabled"))
        elif isinstance(avail, bool):
            self._z_avail = avail

    def _resolve_response(self, raw: object) -> None:
        try:
            resp = json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(resp, dict):
            return
        fut = self._pending.get(resp.get("transaction"))  # type: ignore[arg-type]
        if fut is not None and not fut.done():
            fut.set_result(resp)

    async def _on_ctl(self, msg) -> None:
        """Control channel (request/reply): Zigbee onboarding + mesh diagnostics.

        permit_join opens/closes pairing; networkmap scans the mesh (slow and
        disruptive — on demand only); rename/remove edit the network. Each maps to
        one z2m bridge request; the reply carries z2m's own status or a loud error."""
        try:
            req = json.loads(msg.data) if msg.data else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        action = req.get("action")
        result: dict = {"error": "unknown action"}
        try:
            if action == "permit_join":
                seconds = int(req.get("seconds", 254)) if req.get("on", True) else 0
                result = await self._zreq("permit_join", {"time": seconds}, 15)
            elif action == "networkmap":
                scanning = self._netmap_task is not None and not self._netmap_task.done()
                if req.get("refresh") and not scanning:
                    self._netmap_task = spawn(
                        self._scan_netmap(bool(req.get("routes"))),
                        log=log, name="zigbee networkmap")
                    scanning = True
                result = {
                    "scanning": scanning,
                    "ts_ns": self._netmap_ts,
                    "map": self._netmap,
                    "error": self._netmap_err,
                }
            elif action == "rename":
                result = await self._zreq(
                    "device/rename", {"from": req["from"], "to": req["to"]}, 15)
            elif action == "remove":
                result = await self._zreq(
                    "device/remove", {"id": req["id"], "force": bool(req.get("force"))}, 30)
            elif action == "availability":
                result = {"enabled": self._z_avail}
            elif action == "set_availability":
                enabled = bool(req.get("enabled"))
                result = await self._zreq(
                    "options", {"options": {"availability": {"enabled": enabled}}}, 20)
                if result.get("status") != "error":
                    self._z_avail = enabled
        except TimeoutError:
            result = {"error": f"{action}: zigbee2mqtt did not respond in time"}
        except (KeyError, ValueError, RuntimeError) as exc:
            result = {"error": str(exc) or f"{action} failed"}
        if msg.reply and self._bus is not None:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())

    async def _ingest_event(self, raw: object) -> None:
        """Turn zigbee2mqtt's join/interview/leave events into device-timeline
        entries so pairing progress is visible live in DIDA, not only in the z2m
        console — the whole point of onboarding from here."""
        try:
            evt = json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(evt, dict) or self._bus is None:
            return
        data = evt.get("data") if isinstance(evt.get("data"), dict) else {}
        friendly = str(data.get("friendly_name") or data.get("ieee_address") or "").strip()
        if not friendly:
            return
        dev_slug = slug(friendly, default="dev")
        entity_id = f"{NAMESPACE}:{dev_slug}"
        etype = evt.get("type")
        kind, severity, message = "pairing", "info", ""
        if etype == "device_joined":
            message = f"{friendly}: joined the network, interviewing…"
        elif etype == "device_interview":
            status = data.get("status")
            if status == "started":
                message = f"{friendly}: interview started"
            elif status == "successful":
                model = ((data.get("definition") or {}).get("model")
                         if isinstance(data.get("definition"), dict) else None)
                message = f"{friendly}: paired{f' — {model}' if model else ''}"
            elif status == "failed":
                kind, severity, message = "pairing_failed", "warning", f"{friendly}: interview failed"
            else:
                return
        elif etype == "device_leave":
            kind, severity, message = "device_left", "warning", f"{friendly}: left the network"
        else:
            return
        await emit_journal(
            self._bus, kind, entity_id=entity_id, device_key=dev_slug,
            source=f"adapter:{NAMESPACE}", severity=severity, message=message,
        )

    async def _ingest_availability(self, friendly: str, raw: object) -> None:
        """zigbee2mqtt publishes <friendly>/availability = online|offline when the
        availability feature is on. This is the device's reachability verdict: a
        light that stops acknowledging (dead battery, lost mains, out of range)
        flips to unavailable here — the one signal that a Zigbee device has gone
        quiet, which otherwise leaves DIDA showing its last-known state forever."""
        if not friendly or self._bus is None:
            return
        try:
            payload = json.loads(raw)  # type: ignore[arg-type]
            state = payload.get("state") if isinstance(payload, dict) else payload
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            state = raw.decode(errors="replace") if isinstance(raw, bytes) else raw  # legacy plain string
        reachable = str(state).strip().lower() == "online"
        dev_slug = slug(friendly, default="dev")
        if self._reach.get(dev_slug) == reachable:
            return
        self._reach[dev_slug] = reachable
        await set_reachable(self._bus, dev_slug, NAMESPACE, reachable,
                            detail="" if reachable else "zigbee2mqtt: unavailable")

    async def _announce_main(self, dev_slug: str, cap_key: tuple[str, ...]) -> None:
        """(Re)announce a device's primary entity — its cap set plus its physical
        type. z2m states light/switch/cover/lock explicitly, so an ON/OFF-only
        light isn't misread as a bare switch by downstream capability sniffing."""
        assert self._bus is not None
        entity_id = f"{NAMESPACE}:{dev_slug}"
        dtype = self._dev_type.get(dev_slug)
        if dtype is not None:
            self._typed.add(dev_slug)
        friendly = self._friendly.get(dev_slug, dev_slug)
        await self._bus.publish_entity(EntityInfo(
            entity_id=entity_id, adapter=NAMESPACE, capabilities=list(cap_key),
            name=friendly, device=dev_slug, device_name=friendly,
            device_type=dtype, diagnostic=entity_id not in self._curated,
            native_key=self._native.get(dev_slug),
        ))

    async def _consume(self) -> None:
        assert self._client is not None and self._bus is not None
        try:
            await self._consume_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status.error(f"connection lost: {exc}" if str(exc) else "connection lost")
            log.warning("mqtt: consume loop ended: %s (will reconnect)", exc, exc_info=True)

    async def _consume_loop(self) -> None:
        async for message in self._client.messages:
            topic = str(message.topic)
            if not topic.startswith(self._prefix + "/"):
                continue
            remainder = topic[len(self._prefix) + 1:]
            if await self._ingest_bridge(remainder, message.payload):
                continue
            # Only device state topics: exactly <prefix>/<friendly>. Skip bridge
            # topics and any remaining sub-topics (/set echoes have a "/").
            if not remainder or remainder == "bridge" or "/" in remainder:
                continue
            try:
                payload = json.loads(message.payload)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict):
                await self._ingest_state(remainder, payload)

    async def _ingest_bridge(self, remainder: str, payload) -> bool:
        """Bridge and availability topics; False for anything else."""
        if remainder == "bridge/devices":
            await self._ingest_devices(payload)
        elif remainder == "bridge/logging":
            await self._ingest_log(payload)
        elif remainder == "bridge/info":
            self._ingest_info(payload)
        elif remainder == "bridge/event":
            await self._ingest_event(payload)
        elif remainder.startswith("bridge/response/"):
            self._resolve_response(payload)
        elif remainder.endswith("/availability"):
            await self._ingest_availability(remainder[: -len("/availability")], payload)
        else:
            return False
        return True

    async def _ingest_state(self, friendly: str, payload: dict) -> None:
        dev_slug = slug(friendly, default="dev")
        self._friendly[dev_slug] = friendly  # also learn names from state topics
        entity_id = f"{NAMESPACE}:{dev_slug}"
        ts = time.time_ns()
        readings = build_state_updates(payload)
        # Diagnostic is per ENTITY: a device is diagnostic only if it has NO
        # primary capability (e.g. a bare link-quality probe), never a plug
        # that also reports on_off/power. Sticky so it can't flip later.
        if any(cap not in DIAGNOSTIC_CAPS for cap, _, _ in readings):
            self._curated.add(entity_id)
        entity_diag = entity_id not in self._curated
        # Announce the primary entity when its (accumulated) cap set grows: sets
        # the device-card header (device_name) and the grouping key so the
        # generic facets below hang under the same card. Union-only, never
        # shrinks — a partial report must not overwrite the fuller cap set.
        seen = set(self._dev_caps.get(dev_slug, ())) | {cap for cap, _, _ in readings}
        cap_key = tuple(sorted(seen))
        grew = self._dev_caps.get(dev_slug) != cap_key
        newly_typed = self._dev_type.get(dev_slug) is not None and dev_slug not in self._typed
        if cap_key and (grew or newly_typed):
            self._dev_caps[dev_slug] = cap_key
            await self._announce_main(dev_slug, cap_key)
        for capability, value, unit in readings:
            await self._bus.publish_state(
                StateUpdate(
                    entity_id=entity_id, capability=capability, value=value,
                    adapter=NAMESPACE, ts_ns=ts, unit=unit, name=friendly,
                    diagnostic=entity_diag, device=dev_slug,
                )
            )
        await self._publish_facets(dev_slug, friendly, payload, ts)

    async def _publish_facets(self, dev_slug: str, friendly: str, payload: dict, ts: int) -> None:
        """Every OTHER field the device reports becomes a single-cap facet
        (mqtt:<device>:<field>) grouped under the device, flagged diagnostic
        (defaults hidden). A WRITABLE field is an editable control carrying
        its *_options companion cap; a read-only one is a sensor."""
        for f in build_generic_updates(payload, self._exposes.get(dev_slug, {})):
            feid = f"{NAMESPACE}:{dev_slug}:{slug(f.field, default='f')}"
            caps = [f.cap] + ([f.options_cap] if f.options_cap else [])
            fdiag = f.category != "control"  # settings/diagnostics hidden by default
            if feid not in self._announced:
                self._announced.add(feid)
                await self._bus.publish_entity(EntityInfo(
                    entity_id=feid, adapter=NAMESPACE, capabilities=caps,
                    name=f.name, device=dev_slug, device_name=friendly,
                    diagnostic=fdiag, category=f.category,
                ))
            if isinstance(f.value, bool):
                self._facet_bool[feid] = f.value  # remember for a later `toggle`
            await self._bus.publish_state(StateUpdate(
                entity_id=feid, capability=f.cap, value=f.value, adapter=NAMESPACE,
                ts_ns=ts, unit=f.unit, name=f.name, diagnostic=fdiag,
                category=f.category, device=dev_slug,
            ))
            if f.options_cap:  # ENUM_OPTIONS / NUMBER_OPTIONS — the control's metadata
                await self._bus.publish_state(StateUpdate(
                    entity_id=feid, capability=f.options_cap, value=f.options_val,
                    adapter=NAMESPACE, ts_ns=ts, name=f.name, diagnostic=fdiag,
                    category=f.category, device=dev_slug,
                ))

    async def handle_command(self, command: Command) -> None:
        if self._client is None:
            raise CommandRejected("broker not connected")
        rest = command.entity_id[len(NAMESPACE) + 1:]  # "<device>" or "<device>:<field>"
        try:
            validate_command(command.capability, command.command)
            if ":" in rest:
                # A facet command sets ONE exposed field on z2m (an editable knob).
                # BOOLEAN advertises `toggle`, but z2m's generic /set has none — so
                # pass the last known value and let generic_command resolve it (a
                # missing state raises → rejected below, never a silent no-op).
                dev_slug, field = rest.split(":", 1)
                expose = self._exposes.get(dev_slug, {}).get(field)
                payload = generic_command(field, command.capability, command.command, command.args, expose,
                                          current=self._facet_bool.get(command.entity_id))
            else:
                dev_slug = rest
                payload = command_to_mqtt(command.capability, command.command, command.args)
        except (CapabilityError, KeyError, ValueError) as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        friendly = self._friendly.get(dev_slug, dev_slug)
        await self._client.publish(f"{self._prefix}/{friendly}/set", json.dumps(payload))

    async def stop(self) -> None:
        if self._consume_task is not None:
            self._consume_task.cancel()
            self._consume_task = None
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
