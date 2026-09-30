from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from dida_core import AdapterConfig, Bus, Command, CommandRejected, StateUpdate
from home_core.tasks import spawn

from dida_adapter_govee.discovery import NAMESPACE, Registry, decode, encode

log = logging.getLogger("dida.adapter.govee")

# The sibling bridge container this adapter drives. Its Govee credentials come
# from GOVEE_ENV_PATH (which we write); its MQTT broker settings are static in
# docker-compose (our mosquitto). We only ever touch the credential env + restart.
BRIDGE_CONTAINER = os.environ.get("GOVEE_BRIDGE_CONTAINER", "dida-govee2mqtt")
ENV_PATH = os.environ.get("GOVEE_ENV_PATH", "/govee/govee.env")
# The filtered socket of docker-proxy-govee: it allows restarting the bridge, nothing else.
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/docker/docker.sock")

# The bridge publishes HA MQTT Discovery to OUR mosquitto; we consume it here.
BROKER_HOST = os.environ.get("DIDA_MQTT_HOST", "mosquitto")
BROKER_PORT = int(os.environ.get("DIDA_MQTT_PORT", "1883"))
BROKER_USER = os.environ.get("DIDA_MQTT_USERNAME", "dida") or None
BROKER_PASS = os.environ.get("DIDA_MQTT_PASSWORD", "") or None
DISCOVERY_PREFIX = "homeassistant"


class GoveeAdapter:
    """Owns the whole Govee path in one adapter, one `govee:` namespace — no Home
    Assistant involved:

    1. Manages the co-located govee2mqtt bridge container: the Settings → Adapters
       form (email / password / API key) is written to the bridge's credential env
       and the container is restarted (over the Docker socket, like netmgr).
    2. Consumes the HA MQTT Discovery the bridge publishes on our mosquitto and
       maps every Govee device to canonical capabilities as `govee:<id>` entities;
       DIDA commands become MQTT publishes back to the bridge.

    Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        # Bridge lifecycle.
        self._cfg: AdapterConfig | None = None
        self._applied: tuple[str, str, str] | None = None  # last creds written
        self._bridge_error: str | None = None  # last bridge cred-apply failure (surfaced on the badge)
        # MQTT ingest.
        self._bus: Bus | None = None
        self._client = None
        self._registry = Registry()
        self._subscribed: set[str] = set()
        self._state: dict[tuple[str, str], object] = {}  # (entity_id, capability) -> value
        self._registry_file = Path(os.environ.get("DIDA_STATE_PATH", "/state")) / "govee_registry.json"

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        spawn(self._cfg.poll_loop(), log=log, name="govee config poll")   # live config edits
        spawn(self._bridge_loop(), log=log, name="govee bridge manager")  # creds → bridge
        log.info("govee adapter starting — bridge %s + HA-discovery ingest on %s:%d",
                 BRIDGE_CONTAINER, BROKER_HOST, BROKER_PORT)
        await self._ingest_loop()

    # --- bridge lifecycle: reconcile the bridge to the configured credentials ---

    async def _bridge_loop(self) -> None:
        """Write the bridge's credential env + restart it whenever the configured
        Govee credentials change. Either mode is enough: account (email+password →
        AWS IoT) and/or API key (HTTP + LAN)."""
        backoff = 15  # grows on repeated cred-apply failures (see the except below)
        while True:
            cfg = self._cfg
            email = (cfg.get("email") if cfg else "").strip()
            password = (cfg.get("password") if cfg else "").strip()
            api_key = (cfg.get("api_key") if cfg else "").strip()
            has_account = bool(email and password)
            if has_account or api_key:
                creds = (email if has_account else "", password if has_account else "", api_key)
                if creds != self._applied:
                    try:
                        self._write_env(email, password, api_key)
                        await self._restart_bridge()
                        self._applied = creds
                        self._bridge_error = None
                        backoff = 15  # recovered — back to the normal cadence
                        log.info("govee: wrote bridge env + restarted %s", BRIDGE_CONTAINER)
                    except Exception as exc:
                        # Surface on the badge (not just logs): a failed cred apply means
                        # the bridge keeps running with STALE creds, so devices silently
                        # stop updating. _ingest_loop folds this into its status.
                        self._bridge_error = f"bridge: {exc}" if str(exc) else "bridge update failed"
                        log.error("govee: bridge update failed: %s", exc, exc_info=exc)
                        # Back off exponentially (cap 5 min). `_applied` stays unset on
                        # failure, so without this we'd rewrite the env + POST another
                        # container restart every 15s forever — hammering the Docker
                        # socket and thrashing a bridge that can't come up.
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 300)
                        continue
            await asyncio.sleep(15)

    @staticmethod
    def _sh_quote(v: str) -> str:
        """Single-quote a value so the bridge entrypoint can safely `source` it."""
        return "'" + v.replace("'", "'\\''") + "'"

    def _write_env(self, email: str, password: str, api_key: str) -> None:
        """Render the bridge's credential env (sourced with set -a). Only write the
        creds that are set — an empty GOVEE_EMAIL makes govee2mqtt attempt (and
        fail) an account login."""
        q = self._sh_quote
        lines: list[str] = []
        if email and password:
            lines += [f"GOVEE_EMAIL={q(email)}", f"GOVEE_PASSWORD={q(password)}"]
        if api_key:
            lines.append(f"GOVEE_API_KEY={q(api_key)}")
        os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
        tmp = f"{ENV_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, ENV_PATH)  # atomic swap

    async def _restart_bridge(self) -> None:
        import aiohttp

        conn = aiohttp.UnixConnector(path=DOCKER_SOCK)
        async with (
            aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=15)) as s,
            s.request("POST", f"http://docker/containers/{BRIDGE_CONTAINER}/restart") as r,
        ):
            if r.status not in (204, 304):
                raise RuntimeError(f"restart {BRIDGE_CONTAINER}: HTTP {r.status} {(await r.text())[:120]}")

    # --- HA-discovery ingest: govee2mqtt → govee: entities ---

    async def _ingest_loop(self) -> None:
        """Connect to our mosquitto and stream the bridge's HA discovery + state.
        Reconnects on drop; the device count is this adapter's fail-loud status."""
        import aiomqtt

        while True:
            try:
                self.status.connecting(f"{BROKER_HOST}:{BROKER_PORT}")
                async with aiomqtt.Client(
                    hostname=BROKER_HOST, port=BROKER_PORT,
                    username=BROKER_USER, password=BROKER_PASS,
                ) as client:
                    self._client = client
                    self._subscribed.clear()
                    # Discovery tree (retained on mosquitto) + restored state topics.
                    await client.subscribe(f"{DISCOVERY_PREFIX}/#")
                    self._restore_registry()
                    for topic in self._registry.state_topics():
                        await client.subscribe(topic)
                        self._subscribed.add(topic)
                    n = len(self._registry.entities)
                    if self._bridge_error:
                        self.status.error(self._bridge_error)  # bridge cred apply failed → devices go stale
                    else:
                        self.status.ok(f"{n} device(s)" if n else "connected — waiting for devices")
                    log.info("govee ingest connected %s:%d, %d entity(ies) restored",
                             BROKER_HOST, BROKER_PORT, n)
                    await self._consume(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.error(f"broker: {exc}" if str(exc) else "broker connection failed")
                log.warning("govee: ingest connection lost: %s (retry in 10s)", exc, exc_info=True)
            finally:
                self._client = None
            await asyncio.sleep(10)

    async def _consume(self, client) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            try:
                if topic.startswith(f"{DISCOVERY_PREFIX}/") and topic.endswith("/config"):
                    await self._on_config(topic, message.payload)
                else:
                    await self._on_state(topic, message.payload)
            except Exception:
                log.exception("error handling %s", topic)

    async def _on_config(self, topic: str, payload: bytes) -> None:
        if not payload:
            return  # empty config = a "remove"; ignore for v1
        parts = topic.split("/")
        if len(parts) < 4:
            return
        component, object_id = parts[1], parts[-2]
        try:
            cfg = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(cfg, dict):
            return
        ent, changed = self._registry.add_config(component, object_id, cfg)
        if ent is None or not changed:
            return  # unsupported, or an identical re-announcement
        for topic_ in self._registry.state_topics():
            if topic_ not in self._subscribed and self._client is not None:
                await self._client.subscribe(topic_)
                self._subscribed.add(topic_)
        self._save_registry()
        self.status.ok(f"{len(self._registry.entities)} device(s)")
        log.info("discovered %s (%s) — %d capability(ies)", ent.entity_id, component, len(ent.states))

    async def _on_state(self, topic: str, payload: bytes) -> None:
        bindings = self._registry.by_topic.get(topic)
        if not bindings:
            return
        ts = time.time_ns()
        for entity_id, binding in bindings:
            value = decode(binding, payload)
            if value is None:
                continue
            self._state[(entity_id, binding.capability)] = value
            ent = self._registry.entities.get(entity_id)
            await self._bus.publish_state(StateUpdate(  # type: ignore[union-attr]
                entity_id=entity_id, capability=binding.capability, value=value,
                adapter=NAMESPACE, ts_ns=ts, unit=binding.unit,
                name=ent.name if ent else None,
            ))

    async def handle_command(self, command: Command) -> None:
        if self._client is None:
            raise CommandRejected("broker not connected")
        ent = self._registry.entities.get(command.entity_id)
        if ent is None:
            raise CommandRejected("unknown entity")
        cap, cmd = command.capability, command.command
        if cap == "open_close" and cmd == "set_position":
            spec = ent.commands.get("__set_position__")
        else:
            spec = ent.commands.get(cap)
        if spec is None:
            raise CommandRejected(f"no command mapping for {cap}")
        if cap == "on_off" and cmd == "toggle":  # resolve from last-known state
            cur = self._state.get((command.entity_id, "on_off"))
            cmd = "turn_off" if cur is True else "turn_on"
        out = encode(spec, cmd, dict(command.args))
        if out is None:
            raise CommandRejected(f"uncodable {cap}/{command.command}")
        out_topic, out_payload = out
        await self._client.publish(out_topic, out_payload)

    # --- registry persistence (survives a reconnect without a re-announce) ---

    def _restore_registry(self) -> None:
        try:
            if self._registry_file.exists():
                self._registry.load(json.loads(self._registry_file.read_text()))
        except Exception:
            log.exception("failed to restore registry from %s; starting empty", self._registry_file)

    def _save_registry(self) -> None:
        try:
            tmp = self._registry_file.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._registry.snapshot()))
            tmp.replace(self._registry_file)
        except OSError:
            log.exception("failed to persist registry to %s", self._registry_file)

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None
