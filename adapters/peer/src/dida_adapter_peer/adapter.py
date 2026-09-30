from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from datetime import datetime

from dida_core import AdapterConfig, Bus, Command, CommandRejected, EntityInfo, StateUpdate, slug
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.peer")

NAMESPACE = "peer"
RESYNC_DEFAULT = 300
FAIL_THRESHOLD = 2  # consecutive failures before a location goes red (no blip flap)


def parse_sites(raw: str | None) -> list[dict]:
    """Config → the locations to mirror. A malformed entry is dropped rather than
    killing the adapter; `_incomplete_sites` names what was dropped, so a half-typed
    location shows on the badge instead of silently doing nothing."""
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for d in arr:
        if not isinstance(d, dict):
            continue
        api_url = str(d.get("api_url", "")).strip().rstrip("/")
        name = str(d.get("name", "")).strip()
        if not api_url or not name or not str(d.get("username", "")).strip():
            continue
        patterns = d.get("entities")
        out.append({
            "name": name,
            "api_url": api_url,
            "username": str(d.get("username", "")).strip(),
            "password": str(d.get("password", "")),
            "entities": [str(p).strip() for p in patterns if str(p).strip()]
            if isinstance(patterns, list) else [],
        })
    return out


def site_key(name: str) -> str:
    return slug(name, default="site")


def local_id(key: str, remote_id: str, device_key: str | None = None) -> str:
    """Remote entity id → the id it gets HERE.

    Namespaced by location, because both houses run the same adapters and
    `iammeter:meter:power` is a different meter at each. The remote DEVICE folds
    into the middle segment so the LAST segment stays the facet — that is what the
    device grouping and the energy dashboard's per-device dedup read.
    """
    dev = (device_key or "").strip() or remote_id.rsplit(":", 1)[0]
    facet = remote_id[len(dev):].lstrip(":") if remote_id.startswith(dev) else remote_id
    return f"{NAMESPACE}:{key}_{slug(dev)}:{slug(facet, default='state')}"


def device_id(key: str, remote_id: str, device_key: str | None = None) -> str:
    return local_id(key, remote_id, device_key).rsplit(":", 1)[0]


def selected(remote_id: str, patterns: list[str]) -> bool:
    """An empty pattern list mirrors NOTHING. A peer link is a curated bridge, not a
    copy of the other house: the admin lists what crosses over, and an unset list
    reads as "not configured yet" (the badge says so) rather than "send everything"."""
    return any(fnmatch.fnmatchcase(remote_id, p) for p in patterns)


def _ts_ns(updated_at: object) -> int:
    """`/state`'s ISO timestamp → ns. A mirrored value must carry the time the PEER
    measured it; stamping it `now` here would make a stale reading look fresh."""
    if isinstance(updated_at, str):
        try:
            return int(datetime.fromisoformat(updated_at).timestamp() * 1e9)
        except ValueError:
            pass
    return time.time_ns()


class _Site:
    """One peer installation: its config and the live view we hold of it."""

    def __init__(self, cfg: dict) -> None:
        self.key = site_key(cfg["name"])
        self.cfg = cfg
        self.error = ""
        self.fails = 0
        self.mirrored = 0
        self.available = 0
        # remote entity_id -> its catalog row, refreshed on every resync. The event
        # stream carries values only, so this is what turns one into a publish.
        self.catalog: dict[str, dict] = {}
        self.devices: dict[str, str] = {}  # remote device_key -> display name
        self.remote_of: dict[str, str] = {}  # local entity_id -> remote entity_id
        self.headers: dict[str, str] = {}    # the live session, for commands
        # A rejected command is a CONFIGURATION fault (the peer user may not control
        # that entity), not a blip: it is held on the badge until something succeeds,
        # because the only other symptom is a control that silently does nothing.
        self.cmd_error = ""

    @property
    def name(self) -> str:
        return str(self.cfg["name"])

    @property
    def api_url(self) -> str:
        return str(self.cfg["api_url"])

    @property
    def patterns(self) -> list[str]:
        return list(self.cfg["entities"])


class PeerAdapter:
    """Mirrors a REMOTE DIDA installation's entities into this one.

    The link is a logged-in API session, not a bus-to-bus bridge: the peer signs in
    as an ordinary (non-admin) user on the remote's public HTTPS surface, takes a
    catalog + state snapshot, then tails the same `/ws` event stream the dashboard
    uses. That keeps the remote's NATS private, reuses the authentication,
    revocation and per-user visibility that already exist there, and makes the link
    auditable from the remote side as a login like any other.

    Commands travel back the same way, through the peer's own `POST /command`, so
    the far end keeps every boundary it has — the link user is granted control per
    entity there, which is what decides how much of the house this can touch.

    Mirrored readings are NOT archived here: the remote installation stays the
    system of record for its own history, so the engine skips this adapter when
    writing the history firehose. Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None
        self.broker = None
        self._sites: dict[str, _Site] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._announced: set[str] = set()

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        spawn(self._cfg.poll_loop(), log=log, name="peer config poll")
        log.info("peer adapter starting")
        await self._supervise()

    # ------------------------------------------------------------------ status

    def _incomplete_sites(self) -> list[str]:
        """Locations typed in but unusable (no address, no user). `parse_sites` drops
        them, so without this they would vanish from the badge entirely."""
        raw = self._cfg.get("sites") if self._cfg else None
        if not raw:
            return []
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            return ["sites is not valid JSON"]
        if not isinstance(arr, list):
            return ["sites must be a JSON list"]
        keep = {s["name"] for s in parse_sites(raw)}
        return [str(d.get("name", "")).strip() or "(unnamed)"
                for d in arr if isinstance(d, dict) and str(d.get("name", "")).strip() not in keep]

    def _report(self) -> None:
        """One badge for every location — a location that is down names itself, since
        with several houses "disconnected" alone leaves you guessing which went dark."""
        incomplete = self._incomplete_sites()
        if incomplete:
            self.status.error(" · ".join(f"{n}: incomplete location" for n in incomplete))
            return
        if not self._sites:
            self.status.idle("add a DIDA location in Settings → Adapters")
            return
        broken = [f"{s.name}: {s.error or s.cmd_error}"
                  for s in self._sites.values() if s.error or s.cmd_error]
        if broken:
            self.status.error(" · ".join(broken))
            return
        unselected = [s.name for s in self._sites.values() if not s.patterns]
        if unselected:
            self.status.idle(" · ".join(f"{n}: no entities selected" for n in unselected))
            return
        self.status.ok(" · ".join(
            f"{s.name}: {s.mirrored}/{s.available}" for s in self._sites.values()))

    # ------------------------------------------------------------- supervision

    async def _supervise(self) -> None:
        """Reconcile running site tasks with the configured locations, forever. A
        location removed from the config has its task cancelled AND its mirrored
        entities dropped — a retired house must not linger as fresh-looking readings."""
        while True:
            configured = {site_key(c["name"]): c for c in parse_sites(
                self._cfg.get("sites") if self._cfg else None)}
            for key in list(self._sites):
                if key not in configured:
                    self._stop_site(key)
                    self._sites.pop(key, None)
                    await self._forget(key)
            for key, cfg in configured.items():
                site = self._sites.get(key)
                if site is not None and site.cfg == cfg:
                    continue
                self._stop_site(key)
                site = _Site(cfg)
                self._sites[key] = site
                self._tasks[key] = spawn(self._run_site(site), log=log, name=f"peer site {key}")
            self._report()
            await asyncio.sleep(5)

    def _stop_site(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _forget(self, key: str) -> None:
        """Drop a removed location's entities. Their last values would otherwise sit
        in the UI forever, indistinguishable from live ones."""
        prefix = f"{NAMESPACE}:{key}_"
        self._announced = {e for e in self._announced if not e.startswith(prefix)}
        if self.broker is None:
            return
        await self.broker.call("forget", prefix=prefix)
        log.info("peer: dropped mirrored entities of removed location %s", key)

    # ----------------------------------------------------------------- one site

    async def _run_site(self, site: _Site) -> None:
        backoff = 1
        while True:
            if not site.patterns:
                site.error = ""
                site.fails = 0
                self._report()
                await asyncio.sleep(10)
                continue
            try:
                await self._cycle(site)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                site.fails += 1
                if site.fails >= FAIL_THRESHOLD:
                    site.error = str(exc) or "unreachable"
                self._report()
                log.warning("peer %s: %s (retry in %ds)", site.name, exc, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _cycle(self, site: _Site) -> None:
        """Log in, take a snapshot, then tail the event stream until the resync is due."""
        import aiohttp

        headers = await self._login(site)
        site.headers = headers
        await self._snapshot(site, headers)
        site.error = ""
        site.fails = 0
        site.cmd_error = ""
        self._report()

        ws_url = site.api_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/ws"
        resync = self._cfg.int("resync_seconds", RESYNC_DEFAULT) if self._cfg else RESYNC_DEFAULT
        deadline = time.monotonic() + max(30, resync)
        async with self._session.ws_connect(  # type: ignore[union-attr]
            ws_url, headers=headers, heartbeat=30
        ) as ws:
            while True:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    # Periodic resync: the event stream carries VALUES only, so a
                    # rename, a new entity, or one that stopped reporting is visible
                    # only in a fresh snapshot.
                    return
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                except TimeoutError:
                    return
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self._on_event(site, msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                  aiohttp.WSMsgType.CLOSING):
                    raise RuntimeError("event stream closed")

    async def _login(self, site: _Site) -> dict[str, str]:
        """Sign in and return the Cookie header to carry. The session is held in
        memory only and re-established on every reconnect — that is what makes
        revoking the peer user at the far end actually stop the mirror."""
        import aiohttp

        async with self._session.post(  # type: ignore[union-attr]
            f"{site.api_url}/auth/login",
            json={"username": site.cfg["username"], "password": site.cfg["password"]},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 401:
                raise RuntimeError("login rejected — check the peer user and password")
            resp.raise_for_status()
            cookies = {k: v.value for k, v in resp.cookies.items()}
        if not cookies:
            raise RuntimeError("login returned no session cookie")
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}

    async def _get(self, site: _Site, path: str, headers: dict[str, str]) -> object:
        import aiohttp

        async with self._session.get(  # type: ignore[union-attr]
            f"{site.api_url}{path}", headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _snapshot(self, site: _Site, headers: dict[str, str]) -> None:
        entities = await self._get(site, "/entities", headers)
        states = await self._get(site, "/state", headers)
        if not isinstance(entities, list) or not isinstance(states, list):
            raise RuntimeError("unexpected snapshot payload")
        devices = await self._get(site, "/devices", headers)

        site.devices = {
            str(d.get("device_key")): str(d.get("label") or d.get("name") or d.get("device_key"))
            for d in devices if isinstance(d, dict) and d.get("device_key")
        } if isinstance(devices, list) else {}

        site.available = len(entities)
        site.catalog = {
            rid: e for e in entities if isinstance(e, dict)
            and (rid := str(e.get("entity_id", ""))) and selected(rid, site.patterns)
        }
        site.mirrored = len(site.catalog)
        site.remote_of = {
            local_id(site.key, rid, e.get("device_key")): rid for rid, e in site.catalog.items()
        }

        for rid, e in site.catalog.items():
            await self._announce(site, rid, e)
        for s in states:
            if not isinstance(s, dict):
                continue
            e = site.catalog.get(str(s.get("entity_id", "")))
            if e is not None:
                self._publish(site, e, str(s.get("capability", "")), s.get("value"),
                              s.get("unit"), _ts_ns(s.get("updated_at")))

    async def _announce(self, site: _Site, rid: str, e: dict) -> None:
        eid = local_id(site.key, rid, e.get("device_key"))
        if eid in self._announced:
            return
        self._announced.add(eid)
        caps = e.get("capabilities")
        remote_dev = str(e.get("device_key") or rid.rsplit(":", 1)[0])
        await self._bus.publish_entity(EntityInfo(  # type: ignore[union-attr]
            entity_id=eid, adapter=NAMESPACE,
            capabilities=[str(c) for c in caps] if isinstance(caps, list) else [],
            name=e.get("label") or e.get("name"),
            device=device_id(site.key, rid, e.get("device_key")),
            device_name=f"{site.name} · {site.devices.get(remote_dev, remote_dev)}",
            device_type=e.get("device_type"),
            site=site.name,
            diagnostic=bool(e.get("diagnostic")),
            category=str(e.get("category") or "control"),
        ))

    def _publish(self, site: _Site, e: dict, capability: str,
                 value: object, unit: object, ts_ns: int) -> None:
        if not capability or value is None:
            return
        rid = str(e.get("entity_id", ""))
        eid = local_id(site.key, rid, e.get("device_key"))
        spawn(self._bus.publish_state(StateUpdate(  # type: ignore[union-attr]
            entity_id=eid, capability=capability, value=value, adapter=NAMESPACE,
            ts_ns=ts_ns, unit=str(unit) if unit else None,
            name=e.get("label") or e.get("name"),
            diagnostic=bool(e.get("diagnostic")),
            device=device_id(site.key, rid, e.get("device_key")),
        )), log=log, name=f"publish {eid}")

    def _on_event(self, site: _Site, raw: str) -> None:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(ev, dict) or ev.get("type"):
            return  # keepalive pong, not a state event
        e = site.catalog.get(str(ev.get("entity_id", "")))
        if e is None:
            return  # not mirrored, or new since the snapshot — the resync picks it up
        self._publish(site, e, str(ev.get("capability", "")), ev.get("value"),
                      ev.get("unit"), int(ev.get("ts_ns") or time.time_ns()))

    async def handle_command(self, command: Command) -> None:
        """Forward a command to the location that owns the entity.

        It goes through the peer's `POST /command`, so the far end applies its OWN
        boundary: capability validation, the peer user's control rules, and its
        command audit trail. Nothing here can bypass that — the peer decides what
        this link may touch, which is why the link user is granted control per
        entity rather than wholesale.

        The audit at the far end reads `user:<peer user>`: which LINK acted, not
        which person did at this end. Passing our own source through would let one
        installation write another's audit trail, so the two logs are correlated by
        time, not forged identity.
        """
        site = next((s for s in self._sites.values() if command.entity_id in s.remote_of), None)
        if site is None:
            raise CommandRejected(f"{command.entity_id} is not mirrored from any site")
        remote_id = site.remote_of[command.entity_id]
        try:
            await self._post_command(site, remote_id, command)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            site.cmd_error = f"command rejected: {exc}"
            self._report()
            raise CommandRejected(f"{site.name} refused {remote_id}: {exc}") from exc
        site.cmd_error = ""
        self._report()

    async def _post_command(self, site: _Site, remote_id: str, command: Command) -> None:
        import aiohttp

        body = {"entity_id": remote_id, "capability": command.capability,
                "command": command.command, "args": dict(command.args or {})}
        for attempt in (1, 2):
            if not site.headers:
                site.headers = await self._login(site)
            async with self._session.post(  # type: ignore[union-attr]
                f"{site.api_url}/command", json=body, headers=site.headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401 and attempt == 1:
                    site.headers = {}  # session expired or revoked — sign in once more
                    continue
                if resp.status == 403:
                    raise RuntimeError(
                        f"{remote_id}: the peer user may not control this device")
                resp.raise_for_status()
                return

    async def stop(self) -> None:
        for key in list(self._tasks):
            self._stop_site(key)
        if self._session is not None:
            await self._session.close()
            self._session = None
