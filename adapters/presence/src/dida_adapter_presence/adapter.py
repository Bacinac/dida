from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit

from dida_core import MAX_ACCURACY_M, AdapterConfig, Bus, Command, StateUpdate, resolve_zone, slug
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.presence")

NAMESPACE = "presence"


class PresenceAdapter:
    """OwnTracks GPS → zone resolution → `presence:*` entities.

    Implements `dida_core.Adapter`. Its "device" is partly the DB (zone
    definitions) like the virtual adapter, and partly an MQTT broker like the
    mqtt adapter — it joins the two.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        # Broker/creds/prefix now come from the DB (Settings → Adapters); set in
        # start() from the merged config.
        self._host = "mosquitto"
        self._port = 1883
        self._username: str | None = None
        self._password: str | None = None
        self._prefix = "owntracks"
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._zones: list[tuple[str, float, float, float]] = []  # (name, lat, lon, radius_m)
        self._last: dict[tuple[str, str], object] = {}  # (entity_id, cap) -> last value

    def _conn_key(self) -> tuple:
        """Connection-relevant config as a comparable tuple — the supervise loop
        reconnects when a UI edit changes it (broker was boot-frozen before)."""
        url = urlsplit(self._cfg.get("mqtt_url") or "mqtt://mosquitto:1883")
        # Unconfigured means DIDA's own broker, which requires auth — so fall back
        # to the credentials the stack generated for it rather than connecting
        # anonymously and being rejected on a loop.
        return (
            url.hostname or "mosquitto",
            url.port or 1883,
            self._cfg.get("username") or os.getenv("DIDA_MQTT_USERNAME") or None,
            self._cfg.get("password") or os.getenv("DIDA_MQTT_PASSWORD") or None,
            self._cfg.get("topic_prefix") or "owntracks",
        )

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("presence", self.broker)
        # Supervise (mqtt/ha pattern): reload config + zones each tick; a changed
        # broker/creds/prefix cancels the consume task and starts a fresh one, so
        # Settings → Adapters edits apply live instead of needing a restart.
        consume_task: asyncio.Task | None = None
        active_key: tuple | None = None
        last_zone_reload = 0.0
        while True:
            try:
                await self._cfg.load()
                now = asyncio.get_running_loop().time()
                if now - last_zone_reload >= self._cfg.int("zone_reload_seconds", 30):
                    await self._reload_zones()
                    last_zone_reload = now
                key = self._conn_key()
                dead = consume_task is not None and consume_task.done()
                if key != active_key or dead:
                    if consume_task is not None:
                        consume_task.cancel()
                        # Await teardown so the old consume loop can't coexist with
                        # (and steal frames from) the fresh one — same fix as mqtt/ha.
                        await asyncio.gather(consume_task, return_exceptions=True)
                    active_key = key
                    self._host, self._port, self._username, self._password, self._prefix = key
                    log.info("presence: (re)connecting to %s:%d, topic %s/+/+, %d zones",
                             self._host, self._port, self._prefix, len(self._zones))
                    consume_task = spawn(self._consume(), log=log, name="presence consume")
            except asyncio.CancelledError:
                if consume_task is not None:
                    consume_task.cancel()
                raise
            except Exception as exc:
                log.exception("presence: supervise loop error")  # full traceback, not a bare warning
                self.status.error(str(exc) or "supervise error")  # fail loud, don't leave a stale badge
            await asyncio.sleep(10)

    async def _reload_zones(self) -> None:
        rows = await self.broker.call("zones")
        self._zones = [(r["name"], r["latitude"], r["longitude"], r["radius_m"]) for r in rows]

    async def _consume(self) -> None:
        import aiomqtt

        while True:
            try:
                self.status.connecting(f"{self._host}:{self._port}")
                async with aiomqtt.Client(
                    hostname=self._host, port=self._port,
                    username=self._username, password=self._password,
                ) as client:
                    await client.subscribe(f"{self._prefix}/#")
                    self.status.ok(f"{self._host}:{self._port} · {len(self._zones)} zones")
                    log.info("presence connected %s:%d, subscribed %s/#", self._host, self._port, self._prefix)
                    async for message in client.messages:
                        await self._handle(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.error(f"{exc}" if str(exc) else "connection lost")
                log.warning("presence: mqtt error (%s); reconnecting in 5s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def _handle(self, topic: str, raw: object) -> None:
        # OwnTracks publishes location reports on owntracks/<user>/<device>.
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self._prefix:
            return
        user = parts[1]
        try:
            payload = json.loads(raw)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            log.debug("presence: undecodable frame on %s", topic)
            return
        if not isinstance(payload, dict) or payload.get("_type") != "location":
            return  # ignore transition/lwt/cmd/waypoints frames
        lat, lon = payload.get("lat"), payload.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return
        # Same accuracy discipline as the HTTP receiver: a fix coarser than the
        # zone scale can't say which zone the phone is in — drop the report and
        # wait for the next proper fix. A merely-coarse one still resolves, with
        # its error circle credited so it can't assert a false "away".
        acc = payload.get("acc")
        acc_m = float(acc) if isinstance(acc, (int, float)) else 0.0
        if acc_m > MAX_ACCURACY_M:
            log.debug("presence: %s fix too coarse (acc %.0f m) — skipped", user, acc_m)
            return

        entity_id = f"{NAMESPACE}:{slug(user, default='person')}"
        name = user[:1].upper() + user[1:] if user else user
        location = resolve_zone(float(lat), float(lon), self._zones, acc_m=acc_m)

        # A presence entity has a primary cap (`location`), so it is curated, not
        # diagnostic. Diagnostic is per-ENTITY and last-write-wins in the engine,
        # so every cap of this entity must carry the same flag — publish all as
        # non-diagnostic (lat/lon/battery render as secondary rows on the card).
        await self._pub(entity_id, "location", location, name)
        await self._pub(entity_id, "latitude", round(float(lat), 6), name)
        await self._pub(entity_id, "longitude", round(float(lon), 6), name)
        batt = payload.get("batt")
        if isinstance(batt, (int, float)):
            await self._pub(entity_id, "battery", float(batt), name)

    async def _pub(self, entity_id: str, capability: str, value, name: str | None) -> None:
        if self._bus is None:
            return
        key = (entity_id, capability)
        if self._last.get(key) == value:
            return  # dedupe unchanged values (OwnTracks re-reports on its own cadence)
        self._last[key] = value
        await self._bus.publish_state(
            StateUpdate(
                entity_id=entity_id, capability=capability, value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=name,
            )
        )

    async def handle_command(self, command: Command) -> None:
        return  # read-only source (all caps are sensors) — nothing to command

    async def stop(self) -> None:
        self._bus = None
