from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
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

from dida_adapter_dreame.area import (
    MapError,
    Placement,
    all_maps,
    decode_map,
    pick_map,
    render_entry,
    zone_for,
)
from dida_adapter_dreame.cloud import DreameCloud, DreameCloudError
from dida_adapter_dreame.live import Live, apply_frame, client_id, tls_context
from dida_adapter_dreame.mapping import (
    ACTION_DOCK,
    ACTION_PAUSE,
    ACTION_START,
    CLEANING_MODES,
    EXTRA_PROPS,
    EXTRAS,
    PROP_CLEANING_MODE,
    PROP_SUCTION,
    PROP_WATER_VOLUME,
    PROPS,
    SUCTION_OPTIONS,
    cleaning_mode_name,
    cleaning_mode_packed,
    extra_caps,
    extra_options,
    status_caps,
    suction_id,
    water_volume_id,
)

log = logging.getLogger("dida.adapter.dreame")

NAMESPACE = "dreame"
_MODE_PROP = {"did": "cleaning_mode", "siid": PROP_CLEANING_MODE[0], "piid": PROP_CLEANING_MODE[1]}
# Sending the robot to one spot: a custom start whose status says WHICH kind of job
# and whose properties carry the job itself.
_MAP_PROP = (6, 8)
_ACTION_START_CUSTOM = (4, 1)
_PIID_STATUS = 1
_PIID_CLEANING_PROPERTIES = 10
_STATUS_ZONE_CLEANING = 19
_WATER_HIGH = 3
_LINK_ENTITY = "plan_link"
_ROOMS_ENTITY = "rooms"
# Request/reply: the house asks for the robot's map as a picture it can lay over the
# floor plan. Named for the JOB, not the vendor — any mapping robot answers it.
MAP_SUBJECT = "dida.vacuum.map"
# The same idea for what is happening RIGHT NOW: position, track, area being done.
LIVE_SUBJECT = "dida.vacuum.live"
_CAPS = ["vacuum", "vacuum_mode", "vacuum_mode_options", "battery", "enum", "enum_options"]
# Consecutive failed polls before the badge goes error: one timed-out cloud call
# must not flap the badge, three in a row is a path that is actually down.
_FAILS_BEFORE_ERROR = 3
# How often the bound device record is re-read for the cloud's own online verdict.
_DEVICE_REFRESH = 300.0
# The robot's partition changes only when the house is remapped, but the house has to
# SEE it before anyone can point at a room — so it is fetched on a slow cadence, not
# only when a command happens to arrive.
_ROOMS_REFRESH = 900.0


def _read_token(cloud: DreameCloud) -> str:
    """Off the loop: reading the token may renew it, which is a network round trip."""
    return cloud.token


class DreameAdapter:
    """Dreame robot vacuum through the Dreamehome cloud.

    Registering the robot in the Dreamehome app turns its local API off, so
    there is no LAN path to fall back to: state and commands both ride Dreame's
    account API. A poll reads run-state, battery and suction level; commands are
    MIoT actions (start / pause / return to dock) plus the suction level as the
    entity's `enum`. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._cloud: DreameCloud | None = None
        self._key: tuple | None = None      # (email, password, country) the client was built for
        self._entity_id = f"{NAMESPACE}:vacuum"
        self._name = "Dreame"
        self._model = ""
        self._last: dict[str, object] = {}  # cap -> last published (dedupe)
        self._fails = 0
        self._announced = False
        self._fault: str | None = None      # last reported device fault (log on transitions)
        self._reach: bool | None = None     # last reachability we published (edge-trigger)
        self._devices_at = 0.0
        self._rooms_at = 0.0
        self._last_frame = None    # the map the fit is computed against
        self._map_key = ""         # the map the robot is standing on, by name
        self._map_cache: dict | None = None   # the rendered maps, and the stamp they were drawn from
        self._map_stamp = ""
        self._live = Live()        # what their broker has pushed while it works
        self._live_task = None
        self._packed_mode: int | None = None   # cleaning_mode as stored: a write edits it

    def _conn_key(self) -> tuple | None:
        if self._cfg is None:
            return None
        email = self._cfg.get("email").strip()
        password = self._cfg.get("password").strip()
        if not email or not password:
            return None
        return (email, password, self._cfg.get("country", "eu").strip() or "eu")

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        spawn(self._cfg.poll_loop(), log=log, name="dreame config poll")
        await bus.nc.subscribe(MAP_SUBJECT, cb=self._on_map_request)
        await bus.nc.subscribe(LIVE_SUBJECT, cb=self._on_live_request)
        spawn(self._live_loop(), log=log, name="dreame live frames")
        await self._cfg.load()
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dreame: poll loop error: %s", exc, exc_info=True)
                self.status.error(str(exc) or "poll error")
            await asyncio.sleep(self._cfg.int("poll_seconds", 30) if self._cfg else 30)

    async def _poll(self) -> None:
        key = self._conn_key()
        if key is None:
            self._cloud = None
            self._key = None
            self.status.idle("no account (Settings → Adapters)")
            return
        if key != self._key:
            self._cloud = DreameCloud(*key)
            self._key = key
            self._announced = False
            self._devices_at = 0.0
            self._rooms_at = 0.0
            self._last.clear()
        self._name = (self._cfg.get("name") if self._cfg else "") or "Dreame"
        assert self._cloud is not None
        try:
            await self._refresh_device()
            results = await asyncio.to_thread(
                self._cloud.get_properties, PROPS + EXTRA_PROPS + [_MODE_PROP])
        except DreameCloudError as exc:
            self._fails += 1
            if self._fails >= _FAILS_BEFORE_ERROR:
                self.status.error(f"cloud unreachable: {exc}")
                await self._publish_reach(False, str(exc))
            # Log the descent, then go quiet: a robot away for a week must not
            # cost thousands of log lines a day and bury other adapters.
            if self._fails <= _FAILS_BEFORE_ERROR:
                log.warning("dreame: poll failed (%d in a row): %s", self._fails, exc)
            elif self._fails == _FAILS_BEFORE_ERROR + 1:
                log.warning("dreame: still failing — quiet until the cloud answers again")
            return
        if self._fails:
            log.info("dreame: cloud answering again after %d failed poll(s)", self._fails)
        self._fails = 0
        if not self._announced:
            await self._announce()
        caps, fault = status_caps(results)
        for cap, value in caps.items():
            await self._set(cap, value)
        extras = extra_caps(results)
        for extra in EXTRAS:
            if extra.entity in extras:
                await self._set(extra.capability, extras[extra.entity], extra=extra)
        await self._publish_mode(results)
        await self._publish_link()
        await self._refresh_rooms()
        if fault is not None:
            # A fault is actionable (stuck, dustbin full…): the badge goes error so
            # the Alerts pipeline rings. The transition is LOGGED too — the badge is
            # transient, and a fault that clears before anyone looks leaves no trace.
            if fault != self._fault:
                log.error("dreame: device fault: %s (battery %s)", fault, caps.get("battery"))
            self.status.error(f"{self._name}: {fault}")
        else:
            if self._fault is not None:
                log.info("dreame: device fault cleared (%s)", caps.get("vacuum", "?"))
            self.status.ok(f"{self._name} · {caps.get('vacuum', '?')}")
        self._fault = fault

    async def _refresh_rooms(self) -> None:
        """Keep the room list in front of the house. A map that cannot be read is a
        warning, not a failed poll: everything else about the robot still works."""
        assert self._cloud is not None
        if time.time() - self._rooms_at < _ROOMS_REFRESH:
            return
        self._rooms_at = time.time()
        try:
            cache = await self._maps()
            frame = decode_map(cache["blob"], self._live.charger)
            self._map_key = pick_map(cache["blob"], self._live.charger)[1]
        except (MapError, DreameCloudError) as exc:
            log.warning("dreame: could not read the map: %s", exc)
            return
        self._last_frame = frame
        await self._publish_rooms(frame)

    async def _publish_rooms(self, frame) -> None:
        """The robot's own partition, published so the house can offer it: the rooms
        are what a person points at when they link the two maps."""
        await self._set("text", json.dumps(
            [{"id": r.id, "name": r.name, "m2": r.area_m2} for r in frame.rooms]),
            entity=_ROOMS_ENTITY, entity_name="Rooms")

    async def _publish_link(self) -> None:
        """Whether a tap on the floor plan can mean anything yet. Published as state
        rather than left in a log line, because the button that sends the robot
        somewhere has to know — an action that silently does nothing is worse than a
        missing button."""
        linked = False
        with contextlib.suppress(MapError):
            linked = Placement.load(self._cfg.get("placement") if self._cfg else "",
                                    self._map_key) is not None
        await self._set("boolean", linked, entity=_LINK_ENTITY, entity_name="Plan calibration")

    async def _publish_mode(self, results: list[dict]) -> None:
        """The packed cleaning mode: keep the raw value (a write edits it in place)
        and publish the part of it that names what the robot does to the floor."""
        raw = next((r.get("value") for r in results
                    if r.get("did") == "cleaning_mode" and r.get("code") == 0), None)
        name = cleaning_mode_name(raw)
        if name is None:
            return
        self._packed_mode = int(raw)  # type: ignore[arg-type]
        await self._set("vacuum_mode", name)

    async def _refresh_device(self) -> None:
        """Bind to the account's vacuum and carry the cloud's own online verdict.

        Re-read on a slow cadence: it is the source's answer to "is the robot
        actually connected", which a successful property read alone does not give
        (the cloud answers from its last known values)."""
        assert self._cloud is not None
        if self._cloud.device_id and time.time() - self._devices_at < _DEVICE_REFRESH:
            return
        records = await asyncio.to_thread(self._cloud.devices)
        self._devices_at = time.time()
        vacuums = [r for r in records if str(r.get("model", "")).startswith("dreame.vacuum.")]
        if not vacuums:
            raise DreameCloudError("account has no Dreame vacuum bound")
        record = vacuums[0]
        if len(vacuums) > 1:
            log.warning("dreame: account has %d vacuums, using %r", len(vacuums), record.get("customName"))
        if record.get("did") and str(record["did"]) != self._cloud.device_id:
            self._cloud.bind(record)
            self._model = str(record.get("model") or "")
            self._announced = False
        online = bool(record.get("online", True))
        await self._publish_reach(online, "" if online else "cloud reports the robot offline")

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """Published on a CHANGE only, so the device row and the alert agree with
        the badge instead of re-firing every poll."""
        assert self._cloud is not None
        did = self._cloud.device_id
        if self._bus is None or not did or self._reach == ok:
            return
        self._reach = ok
        await set_reachable(self._bus, did, NAMESPACE, ok, detail="" if ok else detail)

    async def _announce(self) -> None:
        assert self._bus is not None and self._cloud is not None
        await self._bus.publish_entity(EntityInfo(
            entity_id=self._entity_id, adapter=NAMESPACE, name=self._name,
            device=self._cloud.device_id, device_name=self._name, capabilities=_CAPS,
        ))
        await self._set("enum_options", json.dumps(SUCTION_OPTIONS))
        for extra in EXTRAS:
            caps = ["enum", "enum_options"] if extra.capability == "enum" else [extra.capability]
            await self._bus.publish_entity(EntityInfo(
                entity_id=f"{NAMESPACE}:{extra.entity}", adapter=NAMESPACE, name=extra.name,
                device=self._cloud.device_id, device_name=self._name, capabilities=caps,
                diagnostic=extra.diagnostic,
            ))
            if extra.capability == "enum":
                await self._set("enum_options", json.dumps(extra_options(extra)), extra=extra)
        await self._set("vacuum_mode_options", json.dumps(CLEANING_MODES))
        await self._bus.publish_entity(EntityInfo(
            entity_id=f"{NAMESPACE}:{_LINK_ENTITY}", adapter=NAMESPACE, name="Plan calibration",
            device=self._cloud.device_id, device_name=self._name,
            capabilities=["boolean"], diagnostic=True,
        ))
        await self._bus.publish_entity(EntityInfo(
            entity_id=f"{NAMESPACE}:{_ROOMS_ENTITY}", adapter=NAMESPACE, name="Rooms",
            device=self._cloud.device_id, device_name=self._name,
            capabilities=["text"], diagnostic=True,
        ))
        self._announced = True
        log.info("dreame vacuum %r (%s) → %s", self._name, self._model or "?", self._entity_id)

    async def _set(self, capability: str, value: object, *, extra=None,
                   entity: str = "", entity_name: str = "") -> None:
        entity_id = f"{NAMESPACE}:{extra.entity if extra else entity}" if (extra or entity) \
            else self._entity_id
        key = (entity_id, capability)
        if self._last.get(key) == value:
            return
        self._last[key] = value
        assert self._bus is not None and self._cloud is not None
        await self._bus.publish_state(StateUpdate(
            entity_id=entity_id, capability=capability, value=value,  # type: ignore[arg-type]
            adapter=NAMESPACE, ts_ns=time.time_ns(),
            name=(extra.name if extra else (entity_name or self._name)),
            device=self._cloud.device_id, device_name=self._name,
            diagnostic=bool((extra and extra.diagnostic) or entity in (_LINK_ENTITY, _ROOMS_ENTITY)),
            unit=(extra.unit if extra else ("%" if capability == "battery" else None)),
        ))

    async def handle_command(self, command: Command) -> None:
        if command.entity_id == f"{NAMESPACE}:water_volume":
            await self._set_water_volume(command)
            return
        if command.entity_id != self._entity_id:
            return
        if self._cloud is None or not self._cloud.device_id:
            raise CommandRejected("not configured")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        try:
            if command.capability == "vacuum" and command.command == "clean_area":
                await self._place_command(command)
                return
            if command.capability == "vacuum_mode":
                await self._set_cleaning_mode(command)
                return
            if command.capability == "vacuum":
                siid, aiid = {"start": ACTION_START, "pause": ACTION_PAUSE, "dock": ACTION_DOCK}[command.command]
                await asyncio.to_thread(self._cloud.action, siid, aiid)
            elif command.capability == "enum" and command.command == "set_option":
                option = str(command.args.get("value", ""))
                level = suction_id(option)
                if level is None:
                    raise CommandRejected(f"unknown suction option {option!r} (one of {SUCTION_OPTIONS})")
                await asyncio.to_thread(self._cloud.set_property, *PROP_SUCTION, level)
            else:
                raise CommandRejected(f"{command.capability}/{command.command} is not a vacuum command")
        except DreameCloudError as exc:
            self.status.error(f"{command.command} failed: {exc}")
            raise CommandRejected(f"cloud refused: {exc}") from exc
        # Re-poll right away so the UI reflects the new state, not the 30 s tick.
        # The cloud needs a moment to carry the change back from the robot.
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await self._poll()

    async def _place_command(self, command: Command) -> None:
        """Everything that needs the plan and the robot's map in the same sentence:
        remembering where the robot is standing, and sending it to a spot."""
        assert self._cloud is not None and self._cfg is not None
        try:
            corner_a = (float(command.args["x1"]), float(command.args["y1"]))  # type: ignore[index]
            corner_b = (float(command.args["x2"]), float(command.args["y2"]))  # type: ignore[index]
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandRejected("clean_area without an area (x1,y1,x2,y2 on the plan)") from exc
        try:
            cache = await self._maps()
            blob = cache["blob"]
            here = self._live.charger
            frame = decode_map(blob, here)
            self._last_frame = frame
            await self._publish_rooms(frame)
            _entry, key = pick_map(blob, here)
            self._map_key = key
            placement = Placement.load(self._cfg.get("placement"), key)
            if placement is None:
                raise MapError("this map has not been laid over a floor plan yet — show it "
                               "on the plan of that storey and drag it into place")
            zone = zone_for(placement, frame, corner_a, corner_b)
            left, top = frame.origin
            if not (left <= zone[0] and zone[2] <= left + frame.width * frame.grid_mm
                    and top <= zone[1] and zone[3] <= top + frame.height * frame.grid_mm):
                raise MapError("that spot falls outside the robot's map — either it has never "
                               "been there, or the map is not laid over the plan correctly")
            # One pass, the suction the house already uses, and the WETTEST setting:
            # a spot job is asked for because something was spilled, not to tidy up.
            suction = suction_id(str(self._last.get((self._entity_id, "enum"), ""))) or 1
            job = {"areas": [[*zone, 1, suction, _WATER_HIGH]]}
            await asyncio.to_thread(
                self._cloud.action, *_ACTION_START_CUSTOM,
                [{"piid": _PIID_STATUS, "value": _STATUS_ZONE_CLEANING},
                 {"piid": _PIID_CLEANING_PROPERTIES,
                  "value": json.dumps(job, separators=(",", ":"))}])
            self._live.clear_track()
            log.info("dreame: cleaning %.1f×%.1f m drawn on the plan (map %s)",
                     (zone[2] - zone[0]) / 1000, (zone[3] - zone[1]) / 1000, zone)
        except (MapError, DreameCloudError) as exc:
            self.status.error(f"{command.command}: {exc}")
            if isinstance(exc, MapError) and "remapped" in str(exc):
                await self._set("boolean", False, entity=_LINK_ENTITY, entity_name="Plan calibration")
            raise CommandRejected(str(exc)) from exc
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await self._poll()

    async def _live_loop(self) -> None:
        """Stay subscribed to their push broker for as long as the adapter runs.

        Reconnects on its own and never raises into the poll loop: losing the live
        picture must not cost the state, the commands or the map — everything else
        rides the ordinary cloud API."""
        import aiomqtt

        agent = "".join(random.choice("ABCDEF") for _ in range(13))
        while True:
            try:
                if self._cloud is None or not self._cloud.device_id:
                    await asyncio.sleep(10)
                    continue
                cloud = self._cloud
                host, port = cloud.broker
                token = await asyncio.to_thread(_read_token, cloud)
                topic = f"/status/{cloud.device_id}/{cloud.uid}/{cloud.model}/{cloud._country}/"
                async with aiomqtt.Client(
                    hostname=host, port=port, username=cloud.uid, password=token,
                    identifier=client_id(cloud.uid, host, agent),
                    protocol=aiomqtt.ProtocolVersion.V311, clean_session=True,
                    tls_context=tls_context(), timeout=20,
                ) as client:
                    log.info("dreame: listening for live frames on %s", host)
                    await client.subscribe(topic)
                    async for message in client.messages:
                        apply_frame(self._live, bytes(message.payload))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dreame: live frames dropped (%s) — retrying", exc, exc_info=True)
                await asyncio.sleep(15)

    async def _on_live_request(self, msg) -> None:
        """Where it is, where it has been, and what it was sent to do."""
        with contextlib.suppress(Exception):
            await msg.respond(json.dumps({"ok": True, **self._live.as_json()}).encode())

    async def _maps(self) -> dict:
        """Every saved map, drawn — from cache unless the robot changed it.

        Their cloud states the file's checksum in a small property beside it, so
        "has anything changed" costs one call and the answer is usually no. Redrawing
        two storeys on every glance at the plan would be work for nothing: a map
        changes when the house is remapped or a room is renamed, not while you look
        at it."""
        assert self._cloud is not None
        _name, stamp = await asyncio.to_thread(self._cloud.map_stamp, *_MAP_PROP)
        if self._map_cache is not None and stamp and stamp == self._map_stamp:
            return self._map_cache

        blob = await asyncio.to_thread(self._cloud.map_file, *_MAP_PROP)
        maps = []
        for key, entry in await asyncio.to_thread(all_maps, blob):
            png, frame = await asyncio.to_thread(render_entry, entry)
            maps.append({
                "map_id": key, "map_name": entry.get("name") or "",
                "png": base64.b64encode(png).decode(),
                "grid_mm": frame.grid_mm, "width": frame.width, "height": frame.height,
                "origin": list(frame.origin), "charger": list(frame.charger),
                "rooms": [{"id": r.id, "name": r.name, "m2": r.area_m2,
                           "centre": [round(r.centre[0]), round(r.centre[1])]} for r in frame.rooms],
                "_frame": frame,
            })
        self._map_cache = {"blob": blob, "maps": maps}
        self._map_stamp = stamp
        log.info("dreame: map redrawn (%d storey(s), stamp %s)", len(maps), stamp[:8] or "?")
        return self._map_cache

    async def _on_map_request(self, msg) -> None:
        """Hand back the map as a PNG plus the geometry that gives it a scale: the
        picture alone cannot say how many metres a pixel is worth."""
        reply: dict[str, object]
        try:
            if self._cloud is None or not self._cloud.device_id:
                raise MapError("no robot configured")
            cache = await self._maps()
            here = self._live.charger
            _entry, key = pick_map(cache["blob"], here)
            self._map_key = key
            maps = []
            for entry in cache["maps"]:
                if entry["map_id"] == key:
                    self._last_frame = entry["_frame"]
                maps.append({k: v for k, v in entry.items() if k != "_frame"})
            current = next((m for m in maps if m["map_id"] == key), maps[0])
            reply = {"ok": True, "current": key, "maps": maps, **current}
        except (MapError, DreameCloudError) as exc:
            log.warning("dreame: map request failed: %s", exc)
            reply = {"ok": False, "error": str(exc)}
        with contextlib.suppress(Exception):
            await msg.respond(json.dumps(reply).encode())


    async def _set_cleaning_mode(self, command: Command) -> None:
        """Vacuum, mop, or both. The reading is packed, so the write edits the mode
        bits of the value the robot last reported and leaves its other settings alone
        — which is why an unread mode is refused rather than written from zero."""
        if command.command != "set_vacuum_mode":
            raise CommandRejected(f"vacuum_mode has no {command.command}")
        if self._packed_mode is None:
            raise CommandRejected("cleaning mode not read yet, refusing to write one from nothing")
        option = str(command.args.get("value", ""))
        packed = cleaning_mode_packed(self._packed_mode, option)
        if packed is None:
            raise CommandRejected(f"unknown cleaning mode {option!r} (one of {CLEANING_MODES})")
        try:
            await asyncio.to_thread(self._cloud.set_property, *PROP_CLEANING_MODE, packed)
        except DreameCloudError as exc:
            self.status.error(f"cleaning mode failed: {exc}")
            raise CommandRejected(f"cloud refused: {exc}") from exc
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await self._poll()

    async def _set_water_volume(self, command: Command) -> None:
        """The mop's wetness, the one station setting the house steers itself."""
        if self._cloud is None or not self._cloud.device_id:
            raise CommandRejected("not configured")
        if command.capability != "enum" or command.command != "set_option":
            raise CommandRejected(f"water level has no {command.capability}/{command.command}")
        option = str(command.args.get("value", ""))
        level = water_volume_id(option)
        if level is None:
            raise CommandRejected(f"unknown water level {option!r}")
        try:
            await asyncio.to_thread(self._cloud.set_property, *PROP_WATER_VOLUME, level)
        except DreameCloudError as exc:
            self.status.error(f"water level failed: {exc}")
            raise CommandRejected(f"cloud refused: {exc}") from exc
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await self._poll()

    async def stop(self) -> None:
        self._cloud = None
