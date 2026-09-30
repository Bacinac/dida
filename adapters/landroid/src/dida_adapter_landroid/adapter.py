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
    validate_command,
)

log = logging.getLogger("dida.adapter.landroid")

NAMESPACE = "landroid"

# Worx Landroid status id -> normalised mower state (so automations can match
# on a stable vocabulary instead of raw codes). Ids follow pyworxcloud's
# STATE_TO_DESCRIPTION: a status says what the mower is DOING, so only the two
# blocked states and a fence escape are faults here. The fault itself is a
# separate reading (`mower_error`) — reading it out of the status is what had
# ordinary mowing (id 8) alerting as a breakdown.
MOWER_STATE: dict[int, str] = {
    0: "idle", 1: "docked", 2: "starting", 3: "leaving", 4: "mowing",
    5: "returning", 6: "returning", 7: "mowing", 8: "mowing",
    9: "error", 10: "error", 11: "idle", 12: "mowing", 13: "error",
    30: "returning", 31: "mowing", 32: "mowing", 33: "mowing", 34: "paused",
    103: "mowing", 104: "returning", 110: "mowing", 111: "mowing",
}

# Wheel-motor torque trim (`cfg.tq`), -50..50 %. Its own entity, because the
# meaning of a generic `number` lives in the entity name, not the capability.
TORQUE_ENTITY = f"{NAMESPACE}:mower:torque"
TORQUE_NAME = "Wheel torque"
TORQUE_MIN, TORQUE_MAX = -50, 50
TORQUE_OPTIONS = json.dumps({"min": TORQUE_MIN, "max": TORQUE_MAX, "step": 5, "unit": "%"})


def _state_of(device) -> str:
    st = getattr(device, "status", None)
    state = "idle"
    if isinstance(st, dict):
        sid = st.get("id")
        if isinstance(sid, int) and sid in MOWER_STATE:
            state = MOWER_STATE[sid]
        else:
            desc = str(st.get("description", "")).lower().split(":")[0].strip()
            state = desc or "idle"
    # Worx reports id 0 ("idle") even while the mower SITS ON THE BASE charging
    # (idle only means "stopped" — that can be mid-lawn after a manual stop too).
    # Charging is the disambiguator: it can only charge on the base — and it is
    # its own state ("charging"), distinct from resting fully-charged ("docked").
    if state in ("idle", "docked"):
        bat = getattr(device, "battery", None)
        if isinstance(bat, dict) and bat.get("charging") is True:
            return "charging"
    return state


def _error_of(device) -> str | None:
    """The mower's own fault line (`dat.le`), verbatim. This is the only field that
    says WHY it stopped — a lost boundary loop leaves the status at plain "idle",
    so without this a dead lawn reads exactly like a finished one."""
    err = getattr(device, "error", None)
    if isinstance(err, dict):
        desc = str(err.get("description", "")).strip()
        return desc or None
    return None


def _battery_of(device) -> float | None:
    bat = getattr(device, "battery", None)
    if isinstance(bat, dict) and isinstance(bat.get("percent"), int | float):
        return float(bat["percent"])
    return None


def _torque_of(device) -> float | None:
    """The torque trim, or None on a mower that has no such setting: pyworxcloud
    only sets the attribute when the reported cfg actually carries `tq`."""
    tq = getattr(device, "torque", None)
    return float(tq) if isinstance(tq, int | float) else None


class LandroidAdapter:
    """Worx Landroid mower via the Worx cloud (pyworxcloud).

    The mower has no local protocol — its only uplink is Worx' AWS IoT, so this
    is a genuinely cloud-only adapter. Worx' MQTT (an AWS IoT custom-authorizer
    connection over WebSockets) is notoriously flaky: it frequently hangs up the
    connection before it's ready, a well-documented upstream problem. So we don't
    depend on it. We consume state from TWO event streams pyworxcloud exposes:

      * `DATA_RECEIVED` — real-time MQTT push (when the fragile link is up);
      * `API` — the library's REST refresh that self-reschedules every 5-10 min
        regardless of MQTT, so status keeps flowing even while MQTT is down.

    Either stream feeds `_publish`, so DIDA always shows the freshest reading and
    the badge only goes error when BOTH streams have been silent past the stale
    watchdog. We never tear the connection down on transient errors (that only
    fought the library's own reconnect + token refresh and read as flakiness).

    Control (start/pause/dock) can only go over MQTT, and the library's own retry
    covers a failed initial connect but not a link that drops mid-session — that
    one stays down until the next token rotation. So a down link past the grace
    window gets the session rebuilt here, a command that arrives on a down link
    rebuilds before it gives up, and the badge goes error rather than sitting
    green while nothing can be commanded. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._active_key: tuple | None = None
        self._bus: Bus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cloud = None
        self._name = "mower"      # device display name
        self._serial: str | None = None
        self._entity_id = f"{NAMESPACE}:mower"
        self._last_data = 0.0     # monotonic ts of the last MQTT report
        self._mqtt_down_since: float | None = None
        self._torque_announced = False
        self._session = asyncio.Lock()

    def _conn_key(self) -> tuple | None:
        """Login (email, password, cloud) from the DB, or None when unconfigured."""
        email = (self._cfg.get("email") if self._cfg else "").strip()
        password = (self._cfg.get("password") if self._cfg else "").strip()
        if not email or not password:
            return None
        cloud = (self._cfg.get("cloud") if self._cfg else "") or "worx"
        return (email, password, cloud)

    async def start(self, bus: Bus) -> None:
        from pyworxcloud import WorxCloud

        self._bus = bus
        self._loop = asyncio.get_running_loop()
        self._cfg = AdapterConfig("landroid", self.broker)
        # Supervise: (re)connect when the UI sets/changes login, and keep the
        # badge honest. State itself arrives on its own via the push + REST-refresh
        # callbacks — this loop does no cloud I/O, so it stays cheap.
        while True:
            try:
                await self._cfg.load()
                key = self._conn_key()
                if key != self._active_key:
                    await self._apply(WorxCloud, key)
                if self._cloud is not None and self._serial is not None:
                    # (MQTT_CONNECTION events can't be consumed — pyworxcloud
                    # routes them to the MQTT client's own handler — so we read
                    # mqtt_connected directly.)
                    connected = await self._watch_mqtt()
                    silent = time.monotonic() - self._last_data
                    stale = self._cfg.int("stale_seconds", 1800)
                    down = 0.0 if self._mqtt_down_since is None else time.monotonic() - self._mqtt_down_since
                    if silent > stale:
                        self.status.error(f"no report for {int(silent)} s")
                        log.warning("landroid: no report for %ds (mqtt=%s)", int(silent), connected)
                    elif connected:
                        self.status.ok(self._name)
                    elif down > self._cfg.int("mqtt_recover_seconds", 120):
                        # Status still flows on the REST refresh, but control is
                        # dead and a rebuild has already failed to bring it back.
                        self.status.error(f"control unavailable — Worx MQTT down {int(down)} s")
                    else:
                        self.status.ok(f"{self._name} · cloud poll (MQTT down)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("landroid: supervise loop error: %s", exc, exc_info=True)
                self.status.error(str(exc) or "cloud error")
            await asyncio.sleep(self._cfg.int("poll_seconds", 60) if self._cfg else 60)

    async def _watch_mqtt(self) -> bool:
        """Return whether control is live, rebuilding the session when it isn't.

        pyworxcloud only retries MQTT after a failed *initial* connect or a token
        rotation — a link that drops mid-session stays down until the next token
        refresh, hours later. That is long enough to swallow a whole morning of
        scheduled mowing, so once the link has been down past the grace window we
        tear the session down and dial again ourselves.
        """
        connected = bool(getattr(self._cloud, "mqtt_connected", False))
        if connected:
            self._mqtt_down_since = None
            return True
        if self._mqtt_down_since is None:
            self._mqtt_down_since = time.monotonic()
            return False
        grace = self._cfg.int("mqtt_recover_seconds", 120) if self._cfg else 120
        if time.monotonic() - self._mqtt_down_since <= grace:
            return False
        log.warning("landroid: Worx MQTT down %ds — rebuilding session (control unavailable)",
                    int(time.monotonic() - self._mqtt_down_since))
        await self._rebuild()
        connected = bool(getattr(self._cloud, "mqtt_connected", False))
        if connected:
            self._mqtt_down_since = None
        return connected

    async def _rebuild(self) -> None:
        """Re-authenticate and reconnect with the login we're already using."""
        from pyworxcloud import WorxCloud

        await self._apply(WorxCloud, self._active_key)

    async def _apply(self, WorxCloud, key: tuple | None) -> None:
        from pyworxcloud.events import LandroidEvent

        async with self._session:
            await self._connect(WorxCloud, LandroidEvent, key)

    async def _connect(self, WorxCloud, LandroidEvent, key: tuple | None) -> None:
        if self._cloud is not None:
            with contextlib.suppress(Exception):
                await self._cloud.disconnect()
            self._cloud = None
        self._active_key = key
        if key is None:
            self.status.idle("no Worx login")
            log.info("landroid: no config — idle (set it in Settings → Adapters)")
            return
        email, password, cloud_type = key
        self.status.connecting(cloud_type)
        try:
            cloud = WorxCloud(email, password, cloud_type)
            await cloud.authenticate()
            await cloud.connect()
            # One mower per account here; take the first device.
            name, device = next(iter(cloud.devices.items()))
            self._cloud = cloud
            self._name = name
            self._serial = getattr(device, "serial_number", None)
            # Fixed entity id — NOT derived from the mower's name. One mower per
            # account, so a stable constant id keeps the entity identical when the
            # user renames the mower in the Worx app (rename → display name only,
            # never a new entity + orphan). `name` still drives the display name.
            self._entity_id = f"{NAMESPACE}:mower"
            self._torque_announced = False
            self._last_data = time.monotonic()
            # Register the two state streams ONLY NOW, after the entity id is
            # finalised: connect() fires an API refresh internally, and a callback
            # landing while _entity_id was still the default would publish a
            # phantom `landroid:mower` entity. DATA_RECEIVED = MQTT push (when up),
            # API = REST refresh that self-reschedules every 5-10 min even while
            # MQTT is down. Both fire on pyworxcloud's own event handler.
            cloud.set_callback(LandroidEvent.DATA_RECEIVED, self._on_data)
            cloud.set_callback(LandroidEvent.API, self._on_api)
            connected = bool(getattr(cloud, "mqtt_connected", False))
            self._mqtt_down_since = None if connected else time.monotonic()
            self.status.ok(name if connected else f"{name} · cloud poll (MQTT down)")
            log.info("landroid adapter up — %s (serial %s), mqtt=%s", name, self._serial, connected)
            # connect() already pulled a REST snapshot into `device`; publish it
            # now so state is populated immediately, not on the next callback.
            await self._publish(name, device)
        except Exception as exc:
            self.status.error(str(exc) or "cloud login failed")
            log.warning("landroid: connect failed: %s (will retry)", exc, exc_info=True)
            self._active_key = None  # force a retry on the next tick

    def _on_data(self, name: str, device) -> None:
        """MQTT push report (fires from pyworxcloud's MQTT thread)."""
        self._ingest(name, device, "push")

    def _on_api(self, name: str | None = None, device=None, **_) -> None:
        """REST-refresh report — the resilient path while MQTT is down."""
        if device is not None:
            self._ingest(name, device, "cloud-poll")

    def _ingest(self, name: str | None, device, src: str) -> None:
        """Marshal a report onto our loop and publish it (thread-safe handoff)."""
        self._last_data = time.monotonic()
        if self._loop is None:
            return
        log.debug("landroid: %s report for %s", src, name)
        # run_coroutine_threadsafe returns a concurrent.futures.Future whose
        # exception is NOT surfaced anywhere (unlike an asyncio.Task, which the
        # loop's exception handler logs). Without this callback a publish that
        # raised would vanish silently — the mower would just appear to stop
        # updating. Attach a done-callback that logs it loud.
        fut = asyncio.run_coroutine_threadsafe(self._publish(name, device), self._loop)
        fut.add_done_callback(self._on_publish_done)

    @staticmethod
    def _on_publish_done(fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            log.warning("landroid: publish failed: %s", exc, exc_info=exc)

    async def _publish(self, name: str, device) -> None:
        if device is None or self._bus is None:
            return
        readings: list[tuple[str, object]] = [("mower", _state_of(device))]
        bat = _battery_of(device)
        if bat is not None:
            readings.append(("battery", bat))
        err = _error_of(device)
        if err is not None:
            readings.append(("mower_error", err))
        for cap, value in readings:
            await self._bus.publish_state(StateUpdate(
                entity_id=self._entity_id, capability=cap, value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=name or self._name,
                device=self._entity_id, device_name=name or self._name,
            ))
        await self._publish_torque(name, _torque_of(device))

    async def _publish_torque(self, name: str, torque: float | None) -> None:
        if torque is None or self._bus is None:
            return
        card = name or self._name
        if not self._torque_announced:
            self._torque_announced = True
            await self._bus.publish_entity(EntityInfo(
                entity_id=TORQUE_ENTITY, adapter=NAMESPACE, name=TORQUE_NAME,
                capabilities=["number", "number_options"], category="config",
                device=self._entity_id, device_name=card,
            ))
            # Bounds are a constant, so they ride the announce instead of every
            # report — a value that cannot change has no business in history.
            await self._bus.publish_state(StateUpdate(
                entity_id=TORQUE_ENTITY, capability="number_options", value=TORQUE_OPTIONS,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=TORQUE_NAME,
                category="config", device=self._entity_id, device_name=card,
            ))
        await self._bus.publish_state(StateUpdate(
            entity_id=TORQUE_ENTITY, capability="number", value=torque,
            adapter=NAMESPACE, ts_ns=time.time_ns(), name=TORQUE_NAME, unit="%",
            category="config", device=self._entity_id, device_name=card,
        ))

    async def handle_command(self, command: Command) -> None:
        if command.entity_id not in (self._entity_id, TORQUE_ENTITY):
            raise CommandRejected("unknown mower")
        if self._cloud is None or self._serial is None:
            raise CommandRejected("cloud not connected")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        if command.entity_id == TORQUE_ENTITY:
            await self._set_torque(command)
            return
        method = {"start": "start", "pause": "pause", "dock": "home"}.get(command.command)
        if method is None:
            raise CommandRejected(f"the mower cannot {command.command}")
        if not await self._control_ready(command.command):
            raise CommandRejected("control unavailable, Worx MQTT down")
        try:
            await getattr(self._cloud, method)(self._serial)
            # The mower echoes new state via MQTT push (_on_data); nudge it so the
            # UI reflects the command promptly instead of at the next report.
            await asyncio.sleep(2)
            await self._cloud.update(self._serial)
        except Exception as exc:
            raise CommandRejected(f"cloud refused: {exc}") from exc

    async def _set_torque(self, command: Command) -> None:
        raw = command.args.get("value")
        try:
            value = round(float(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CommandRejected(f"set_value carries no number ({raw!r})") from exc
        if not TORQUE_MIN <= value <= TORQUE_MAX:
            raise CommandRejected(f"torque {value} outside {TORQUE_MIN}..{TORQUE_MAX}")
        if not await self._control_ready("set torque"):
            raise CommandRejected("control unavailable, Worx MQTT down")
        try:
            await self._cloud.set_torque(self._serial, value)
            await asyncio.sleep(2)
            await self._cloud.update(self._serial)
        except Exception as exc:
            raise CommandRejected(f"set torque {value} failed: {exc}") from exc

    async def _control_ready(self, what: str) -> bool:
        """Control only exists over MQTT. A dropped command here is invisible to
        whoever sent it — an automation's morning start would just never happen —
        so revive the link first and only give up once that fails too."""
        if getattr(self._cloud, "mqtt_connected", False):
            return True
        log.warning("landroid: %s arrived with Worx MQTT down — rebuilding session", what)
        await self._rebuild()
        if self._cloud is not None and getattr(self._cloud, "mqtt_connected", False):
            return True
        log.error("landroid: cannot send %s — Worx MQTT still down after reconnect "
                  "(control unavailable, status still polling)", what)
        self.status.error("control unavailable — Worx MQTT down")
        return False

    async def stop(self) -> None:
        if self._cloud is not None:
            with contextlib.suppress(Exception):
                await self._cloud.disconnect()
            self._cloud = None
