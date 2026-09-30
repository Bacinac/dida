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
    set_reachable,
    validate_command,
)

from dida_adapter_harmony.mapping import (
    HUB_ENTITY,
    NAMESPACE,
    POWER_OFF,
    REMOTE_KEYS,
    activity_options,
    build_button_map,
    button_entity,
)

log = logging.getLogger("dida.adapter.harmony")

_UNSET = object()  # sentinel: forces a button-map rebuild on the first sync


class HarmonyAdapter:
    """Drives a Harmony Hub via its local API, modelled as ONE `remote` device:

      * a `source` capability whose value is the current activity and whose
        options are the hub's activities (set_source starts an activity / powers
        off) — the single real, readable state;
      * a fixed cluster of momentary `press` buttons (D-pad / transport / volume)
        as sibling entities. Harmony is a blind remote: a key has no readable
        state, so it is write-only. A press is routed, through the CURRENT
        activity's control group, to whichever device owns that key.

    Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._host = ""
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._client = None
        self._connected = False  # tracks the live hub link (disconnect callback flips it)
        self._reach: bool | None = None  # last reachability we published (edge-trigger)
        self._by_label: dict[str, int] = {}
        self._options: list[str] = []
        self._name = "Harmony Hub"
        self._buttons: dict[str, tuple[str, str]] = {}  # suffix -> (deviceId, fn)
        self._cur_activity_id: object = _UNSET
        self._last: dict[str, object] = {}

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("harmony", self.broker)
        # Supervise: config is re-read EVERY tick so setting the host in the UI
        # takes effect without a container restart, and an unconfigured adapter
        # doesn't park itself forever.
        while True:
            try:
                await self._cfg.load()
                host = (self._cfg.get("host") or "").strip()
                if host != self._host:
                    # Host changed (set, edited, or cleared) — drop the old client.
                    if self._client is not None:
                        await self._close_client()
                    self._host = host
                if not self._host:
                    self.status.idle("no hub configured")
                else:
                    if self._client is not None and not self._connected:
                        # disconnect callback fired since last tick — drop the dead link
                        await self._close_client()
                    if self._client is None:
                        await self._connect()
                    await self._sync()
                    self.status.ok(f"{self._name} · {len(self._options)} activities")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.error(str(exc) or "poll failed")
                log.warning("harmony: poll failed: %s (reconnecting)", exc, exc_info=True)
                await self._reconnect()
            await asyncio.sleep(self._cfg.int("poll_seconds", 15) if self._cfg else 15)

    async def _close_client(self) -> None:
        try:
            if self._client is not None:
                await self._client.close()
        except Exception:
            log.debug("harmony: client close failed", exc_info=True)
            pass
        self._client = None
        self._connected = False
        self._buttons = {}
        self._cur_activity_id = _UNSET
        self._last.clear()

    async def _connect(self) -> None:
        from aioharmony.const import ClientCallbackType
        from aioharmony.harmonyapi import HarmonyAPI

        self.status.connecting(self._host)
        client = HarmonyAPI(ip_address=self._host)
        # Assign self._client only AFTER connect() succeeds: a failed connect used
        # to leave a half-open non-None client, so the next tick skipped _connect
        # and polled an unconnected object. connect() returns False (not raises) on
        # a refused hub — treat that as a hard failure so we never claim ok() green
        # over a dead link.
        if not await client.connect():
            raise RuntimeError(f"hub at {self._host} refused the connection")
        # Wire the disconnect callback: aioharmony holds a persistent XMPP/websocket
        # link and _sync only reads its CACHED current_activity (no I/O), so a silent
        # hub death would otherwise never surface. On disconnect we flip _connected
        # and the next poll tick rebuilds the client, failing loud if it's really gone.
        client.callbacks = ClientCallbackType(
            connect=None, disconnect=self._on_disconnect,
            new_activity_starting=None, new_activity=None, config_updated=None,
        )
        self._connected = True
        await self._publish_reach(True)
        self._client = client
        self._name = self._client.name or "Harmony Hub"
        self._by_label, self._options = activity_options(self._client.config or {})
        self._cur_activity_id = _UNSET  # first _sync rebuilds the button map
        await self._announce()
        self.status.ok(f"{self._name} · {len(self._options)} activities")
        log.info("harmony adapter up — %s, %d activities", self._name, len(self._options))

    async def _on_disconnect(self, _msg: object = None) -> None:
        """aioharmony fires this when the persistent hub link drops. Mark the adapter
        unhealthy and force the next tick to rebuild, rather than serving a stale
        activity behind a green badge."""
        self._connected = False
        self.status.error("hub disconnected")
        await self._publish_reach(False, "hub disconnected")
        log.warning("harmony: hub disconnected — will reconnect")

    async def _reconnect(self) -> None:
        await self._close_client()
        if not self._host:
            return
        try:
            await self._connect()
        except Exception as exc:
            self.status.error(str(exc) or "reconnect failed")
            await self._publish_reach(False, str(exc) or "reconnect failed")
            log.warning("harmony reconnect failed: %s", exc, exc_info=True)

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """The persistent hub link's verdict, published on a CHANGE only."""
        if self._bus is None or self._reach == ok:
            return
        self._reach = ok
        await set_reachable(self._bus, HUB_ENTITY, NAMESPACE, ok, detail="" if ok else detail)

    async def _announce(self) -> None:
        """Declare the hub (source picker) + every remote key (press buttons),
        all grouped under the one hub device card."""
        assert self._bus is not None
        await self._bus.publish_entity(EntityInfo(
            entity_id=HUB_ENTITY, adapter=NAMESPACE, name=self._name, device_type="remote",
            device=HUB_ENTITY, device_name=self._name, capabilities=["source", "source_options"],
        ))
        for suffix, (_cands, label) in REMOTE_KEYS.items():
            await self._bus.publish_entity(EntityInfo(
                entity_id=button_entity(suffix), adapter=NAMESPACE,
                name=f"{self._name} — {label}", device_type="button",
                device=HUB_ENTITY, device_name=self._name, capabilities=["press"],
            ))

    def _current(self) -> tuple[object, str]:
        """(activity id, label) of the running activity — PowerOff when idle."""
        ca = self._client.current_activity if self._client is not None else None
        if isinstance(ca, (tuple, list)) and len(ca) >= 2:
            return ca[0], str(ca[1])
        return -1, POWER_OFF

    async def _sync(self) -> None:
        """Publish the current activity as `source`; rebuild the key→device map
        when the activity changes (each activity has its own control group)."""
        if self._client is None or self._bus is None:
            return
        act_id, label = self._current()
        if act_id != self._cur_activity_id:
            self._cur_activity_id = act_id
            self._buttons = build_button_map(self._client.config or {}, act_id)
        await self._publish("source", label)
        await self._publish("source_options", json.dumps(self._options, ensure_ascii=False))

    async def _publish(self, cap: str, value: object) -> None:
        if self._last.get(cap) == value:
            return
        self._last[cap] = value
        assert self._bus is not None
        await self._bus.publish_state(StateUpdate(
            entity_id=HUB_ENTITY, capability=cap, value=value,  # type: ignore[arg-type]
            adapter=NAMESPACE, ts_ns=time.time_ns(), name=self._name,
            device=HUB_ENTITY, device_name=self._name,
        ))

    async def handle_command(self, command: Command) -> None:
        eid = command.entity_id
        # The activity picker on the hub, or a remote key press on a sibling button entity.
        activity = eid == HUB_ENTITY and command.capability == "source"
        if not activity and not (eid.startswith(f"{HUB_ENTITY}_") and command.capability == "press"):
            return
        if self._client is None:
            raise CommandRejected("hub not connected")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        if not activity:
            await self._press(eid[len(HUB_ENTITY) + 1:])
        elif command.command == "set_source":
            await self._set_source(str(command.args.get("value")))

    async def _set_source(self, value: str) -> None:
        try:
            if value == POWER_OFF:
                await self._client.power_off()
            else:
                act_id = self._by_label.get(value)
                if act_id is None:
                    raise CommandRejected(f"unknown activity {value!r}")
                await self._client.start_activity(act_id)
            await asyncio.sleep(2)
            await self._sync()
        except CommandRejected:
            raise
        except Exception as exc:
            raise CommandRejected(f"set_source failed: {exc}") from exc

    async def _press(self, suffix: str) -> None:
        target = self._buttons.get(suffix)
        if target is None:
            # A key not mapped in the current activity (or PowerOff) — a universal
            # remote key that just does nothing here. Not an error.
            log.info("harmony: key %r not available in the current activity", suffix)
            return
        dev_id, fn = target
        from aioharmony.const import SendCommandDevice

        try:
            await self._client.send_commands(SendCommandDevice(device=dev_id, command=fn, delay=0))
        except Exception as exc:
            raise CommandRejected(f"press {suffix} failed: {exc}") from exc

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None
