"""DIDA Frigate adapter — one adapter, many locations.

Bridges one or more Frigate NVRs (https://frigate.video) onto the DIDA bus.
Frigate is the outside eyes for other locations — the sibling of BABA (which is
the vision at home). Each configured SITE is an independent NVR: usually a remote
view-only Frigate reached over a tunnel, or — when a site also declares an MQTT
broker — a local Frigate whose live events (motion/person/zones/switches) are
consumed too.

Two planes, same split as the `baba` adapter:

  1. Semantics over MQTT (only for a site with a local broker) — DISCOVERY-DRIVEN:
     every topic Frigate publishes is classified by shape + value type into a DIDA
     entity, so the FULL surface is exposed (curated by rename/hide in Settings →
     Adapters, not curated here). `frigate/events` → object_class + zone occupancy;
     `frigate/<cam>/motion` → motion; `frigate/<cam>/<object>` → person_count (person)
     or a `measurement` count (others); `frigate/<cam>/<feature>/state` → a
     controllable `on_off` switch or settable `number` (commands route to `/set`);
     `frigate/stats` → per-camera fps `measurement`s. New Frigate signals appear
     automatically.
  2. Pixels never touch the bus. Each camera entity carries a `camera` capability
     whose value is a JSON stream descriptor; the browser pulls the live view
     (MJPEG/snapshot) through DIDA's own proxy, which logs in to the site
     server-side so the credential never reaches the browser.

Sites are configured in Settings → Adapters as a LIST (name, Frigate URL,
credentials, optional go2rtc/broker). Every camera lives in the single `frigate`
namespace (`frigate:<site>:<cam>`) and carries a `site` label the UI shows as a chip on
each tile, so one cumulative wall holds every location's cameras.

Everything is validated at the DIDA boundary, so Frigate's open label set is
folded into the closed `object_class` choice set before publishing — a novel
label can never wedge the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import ssl
import time
from urllib.parse import urlsplit

import aiohttp
from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    slug,
    validate_command,
)
from dida_core.frigate_sites import camera_key, parse_sites, site_key
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.frigate")

NAMESPACE = "frigate"

# Frigate's open COCO-ish label set → DIDA's closed `object_class` choices. The
# engine rejects anything outside ("person","vehicle","animal","other","none"),
# so fold at the boundary exactly like baba does.
_VEHICLE = {"car", "motorcycle", "motorbike", "bus", "truck", "bicycle"}
_ANIMAL = {"cat", "dog", "bird", "horse", "sheep", "cow", "bear"}


def _object_class(label: str | None) -> str:
    if not label:
        return "other"
    c = label.strip().lower()
    if c == "person":
        return "person"
    if c in _VEHICLE:
        return "vehicle"
    if c in _ANIMAL:
        return "animal"
    return "other"


# Frigate's controllable per-camera features are DISCOVERED, not hardcoded: any
# `<cam>/<feature>/state` topic becomes a controllable entity (on_off for ON/OFF,
# number for a numeric value) whose commands route to `<cam>/<feature>/set`. So a
# new Frigate toggle (birdseye, improve_contrast, ptz_autotracker, a threshold…)
# appears automatically without this adapter curating a fixed list.
_ONOFF = {"ON", "OFF"}

_TLS_SCHEMES = {"mqtts", "ssl", "tls"}


def _as_num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _descriptor(frigate_url: str, go2rtc_url: str, cam: str, site: str = "", *,
                authed: bool = False) -> str:
    """Stream descriptor the UI player consumes. Frigate's latest frame always
    works through whatever proxy fronts `frigate_url`; the live streams (WebRTC,
    HLS, MP4) are added only when a go2rtc URL is known. `site` is the location
    label the UI badges + groups by.

    `auth` is a NON-secret pointer: it tells DIDA's camera proxy this source needs
    a login and that the credentials live in the `frigate` adapter config; the
    proxy matches the request's ORIGIN to a site and logs in server-side. The
    password never travels in the descriptor (which the browser sees)."""
    f = frigate_url.rstrip("/")
    d: dict[str, str] = {
        "stream": cam,
        # Downscaled frame for the polled tile: Frigate's full latest.jpg is ~0.5 MB,
        # which stalls past the proxy's read timeout when a REMOTE NVR's tunnel is
        # slow (→ 502, a broken tile). `height` shrinks it ~10× (a 480 px frame is
        # ~60 KB) so a poll rides even a sluggish tunnel; it's plenty for a grid tile
        # and the polled solo view (true low-latency video is go2rtc WebRTC).
        "snapshot": f"{f}/api/{cam}/latest.jpg?height=480",
        # Recorded-event feed: a NON-secret pointer to this Frigate's events API. The
        # DIDA proxy uses it to surface the last detections' snapshots (tap a person
        # badge → the evidence frame), reusing the same server-side login.
        "events": f"{f}/api/events",
    }
    if go2rtc_url:
        g = go2rtc_url.rstrip("/")
        d["webrtc"] = f"{g}/api/ws?src={cam}"
        d["hls"] = f"{g}/api/stream.m3u8?src={cam}"
        d["mp4"] = f"{g}/api/stream.mp4?src={cam}"
    if site:
        d["site"] = site
    if authed:
        d["auth"] = NAMESPACE
    return json.dumps(d)


def _text(payload: object) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode(errors="replace").strip()
    return str(payload).strip()


class FrigateAdapter:
    """Bridges one or more Frigate NVRs onto the DIDA bus. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        # Per-site HTTP sessions (own cookie jar → own frigate_token) + login flags.
        self._sessions: dict[str, aiohttp.ClientSession] = {}
        self._authed: dict[str, bool] = {}
        # Per-site live MQTT clients (only sites with a local broker) + status.
        self._clients: dict[str, object] = {}
        # Per-site roster status for the aggregate badge: name -> (ok, detail).
        self._site_status: dict[str, tuple[bool, str]] = {}
        # Live translation state (cameras are globally namespaced, so keyed flat).
        self._known_cameras: set[str] = set()
        self._known_zones: set[str] = set()
        self._known_features: set[str] = set()   # "<cslug>:<feature>" announced
        self._known_measures: set[str] = set()   # "<cslug>:<key>" measurement entities announced
        self._feature_cap: dict[str, str] = {}   # "<cslug>:<feature>" -> "on_off" | "number" (command routing)
        self._cam_name: dict[str, str] = {}   # slug -> authoritative Frigate camera name
        self._cam_site: dict[str, str] = {}    # slug -> owning site name (command routing)
        self._camera_zones: dict[str, set[str]] = {}  # camera -> configured zones
        self._events: dict[tuple[str, str], tuple[str, set[str]]] = {}
        self._event_ts: dict[tuple[str, str], int] = {}
        self._last: dict[tuple[str, str], object] = {}  # (entity, cap) -> last published (dedup)

    # ---- config -------------------------------------------------------------

    def _sites(self) -> list[dict]:
        return parse_sites(self._cfg.get("sites") if self._cfg else None)

    def _track_ttl(self) -> int:
        return self._cfg.int("track_ttl_s", 600) if self._cfg else 600

    def _refresh_s(self) -> int:
        return self._cfg.int("config_refresh_s", 300) if self._cfg else 300

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        await self.broker.call("frigate_migrate")
        spawn(self._config_refresh_loop(), log=log, name="frigate config refresh")
        spawn(self._sweep_loop(), log=log, name="frigate stale sweeper")
        await self._mqtt_supervise()

    async def _mqtt_supervise(self) -> None:
        """Maintain one MQTT consume task per site that declares a local broker
        (presence/mqtt supervise pattern): a changed broker/creds/prefix cancels
        that site's task and starts a fresh one; a removed site is torn down.
        Remote view-only sites (no `mqtt_url`) never spawn a task here."""
        tasks: dict[str, asyncio.Task] = {}
        keys: dict[str, tuple] = {}
        try:
            while True:
                try:
                    await self._cfg.load()
                    await self.broker.call("frigate_migrate")
                    want = {s["name"]: self._conn_key(s) for s in self._sites() if s.get("mqtt_url")}
                    # Drop tasks for sites gone or reconfigured.
                    for name in list(tasks):
                        dead = tasks[name].done()
                        if name not in want or want[name] != keys.get(name) or dead:
                            tasks[name].cancel()
                            await asyncio.gather(tasks[name], return_exceptions=True)
                            tasks.pop(name, None)
                            keys.pop(name, None)
                            self._clients.pop(name, None)
                    # Start tasks for new/changed sites.
                    for name, key in want.items():
                        if name not in tasks and key is not None:
                            keys[name] = key
                            tasks[name] = spawn(self._consume(name, key), log=log,
                                                name=f"frigate consume {name}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("frigate: mqtt supervise error")
                    self.status.error(str(exc) or "supervise error")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            for t in tasks.values():
                t.cancel()
            raise

    def _conn_key(self, site: dict) -> tuple | None:
        url = site.get("mqtt_url", "")
        if not url:
            return None
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return None
        tls = parts.scheme in _TLS_SCHEMES or site.get("tls")
        port = parts.port or (8883 if tls else 1883)
        return (host, port, site.get("mqtt_user") or None, site.get("mqtt_password") or None,
                site.get("topic_prefix") or "frigate", bool(tls), bool(site.get("tls_insecure")))

    async def _consume(self, site_name: str, key: tuple) -> None:
        import aiomqtt

        host, port, username, password, prefix, tls, insecure = key
        tls_context: ssl.SSLContext | None = None
        if tls:
            tls_context = ssl.create_default_context()
            if insecure:
                tls_context.check_hostname = False
                tls_context.verify_mode = ssl.CERT_NONE
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=host, port=port, username=username, password=password,
                    tls_context=tls_context, identifier=f"dida-adapter-frigate-{site_key(site_name)}",
                ) as client:
                    self._clients[site_name] = client
                    await client.subscribe(f"{prefix}/#")
                    log.info("frigate[%s]: connected %s:%d, subscribed %s/#", site_name, host, port, prefix)
                    async for message in client.messages:
                        try:
                            await self._handle(site_name, prefix, str(message.topic), message.payload)
                        except Exception:
                            log.exception("frigate[%s]: error handling %s", site_name, message.topic)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("frigate[%s]: mqtt error (%s); reconnecting in 5s", site_name, exc, exc_info=True)
                await asyncio.sleep(5)
            finally:
                self._clients.pop(site_name, None)

    async def stop(self) -> None:
        for sess in self._sessions.values():
            with contextlib.suppress(Exception):
                await sess.close()
        self._sessions.clear()

    # ---- MQTT routing -------------------------------------------------------

    async def _handle(self, site_name: str, prefix: str, topic: str, payload: object) -> None:
        # Discovery-driven: classify EVERY topic Frigate publishes by shape + value
        # type, so the full surface (per-object counts, toggles, thresholds,
        # per-camera fps) becomes DIDA entities — curated (rename / hide) in Settings
        # → Adapters, not curated here. Pixels + write-only siblings are skipped.
        parts = topic.split("/")
        if len(parts) < 2 or parts[0] != prefix:
            return
        rest = parts[1:]
        text = _text(payload)

        # Instance-level: `frigate/<leaf>`
        if len(rest) == 1:
            leaf = rest[0]
            if leaf == "events":
                self._on_event(site_name, payload)
            elif leaf == "stats":
                self._on_stats(site_name, payload)
            # `available` (already the adapter status badge) / `reviews` (event JSON,
            # surfaced via the archive plane, not entity state): not entities.
            return

        cam = rest[0]
        tail = rest[1:]

        # `frigate/<cam>/<leaf>`
        if len(tail) == 1:
            leaf = tail[0]
            if leaf == "motion":
                self._on_motion(site_name, cam, text)
            else:
                n = _as_num(text)
                if n is not None:  # per-object live count (leaf = object name or "all")
                    self._on_count(site_name, cam, leaf, n)
            return

        # `frigate/<cam>/<feature>/<verb>`
        if len(tail) == 2 and tail[1] == "state":
            # A controllable feature Frigate mirrors read (`/state`) + write (`/set`).
            self._on_feature(site_name, cam, tail[0], text)
        # `set` (write sibling), `snapshot` (image), `<object>/active` (redundant with
        # the base count), and deeper topics: not surfaced.

    def _on_event(self, site_name: str, payload: object) -> None:
        try:
            ev = json.loads(payload)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(ev, dict):
            return
        after = ev.get("after") or ev.get("before") or {}
        etype = ev.get("type")
        eid = after.get("id")
        cam = after.get("camera")
        if not eid or not cam:
            return
        self._register_camera(site_name, cam)
        device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
        event_key = (site_name, str(eid))
        ckey = camera_key(site_name, cam)

        if etype in ("new", "update"):
            self._events[event_key] = (ckey, set(after.get("current_zones") or []))
            self._event_ts[event_key] = time.time_ns()
            self._pub(device, "object_class", _object_class(after.get("label")),
                      name=self._camera_name(site_name, cam), device=device)
            self._sync_zones(site_name, cam)
        elif etype == "end":
            self._events.pop(event_key, None)
            self._event_ts.pop(event_key, None)
            self._sync_zones(site_name, cam)
            if not any(c == ckey for c, _z in self._events.values()):
                self._pub(device, "object_class", "none",
                          name=self._camera_name(site_name, cam), device=device)

    def _on_motion(self, site_name: str, cam: str, text: str) -> None:
        self._register_camera(site_name, cam)
        device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
        self._pub(device, "motion", text.upper() == "ON",
                  name=self._camera_name(site_name, cam), device=device)

    def _on_count(self, site_name: str, cam: str, obj: str, n: float) -> None:
        """Live per-object count. `person` is the camera hero's `person_count`;
        every other object (car, dog, …, or `all`) is a read-only `measurement`
        entity grouped under the camera (diagnostic → hidden by default, curatable)."""
        self._register_camera(site_name, cam)
        device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
        if obj == "person":
            self._pub(device, "person_count", max(0, int(n)),
                      name=self._camera_name(site_name, cam), device=device)
            return
        entity = f"{device}:count:{slug(obj)}"
        self._register_measurement(site_name, cam, entity, f"{self._camera_name(site_name, cam)} {obj}")
        self._pub(entity, "measurement", n, name=f"{self._camera_name(site_name, cam)} {obj}", device=device)

    def _on_feature(self, site_name: str, cam: str, feature: str, text: str) -> None:
        """A Frigate feature mirrored read/write: ON/OFF → an `on_off` switch, a
        numeric value → a settable `number` (a threshold). Discovered, so a new
        Frigate toggle needs no code here; commands route to `<cam>/<feature>/set`."""
        self._register_camera(site_name, cam)
        device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
        entity = f"{device}:{slug(feature)}"
        up = text.strip().upper()
        if up in _ONOFF:
            self._register_feature(site_name, cam, feature, "on_off")
            self._pub(entity, "on_off", up == "ON",
                      name=f"{self._camera_name(site_name, cam)} {feature}", device=device)
            return
        n = _as_num(text)
        if n is not None:
            self._register_feature(site_name, cam, feature, "number")
            self._pub(entity, "number", n,
                      name=f"{self._camera_name(site_name, cam)} {feature}", device=device)

    def _on_stats(self, site_name: str, payload: object) -> None:
        """Per-camera processing rates from `frigate/stats` → `measurement` entities
        (diagnostic). Defensive: only numeric leaves under an ALREADY-known camera."""
        try:
            data = json.loads(payload)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        cams = (data or {}).get("cameras") if isinstance(data, dict) else None
        if not isinstance(cams, dict):
            return
        for cam, metrics in cams.items():
            if not isinstance(metrics, dict) or camera_key(site_name, cam) not in self._known_cameras:
                continue
            device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
            for key in ("camera_fps", "detection_fps", "process_fps", "skipped_fps"):
                val = metrics.get(key)
                if not isinstance(val, (int, float)):
                    continue
                entity = f"{device}:fps:{key.replace('_fps', '')}"
                self._register_measurement(site_name, cam, entity, f"{self._camera_name(site_name, cam)} {key}")
                self._pub(entity, "measurement", float(val),
                          name=f"{self._camera_name(site_name, cam)} {key}", device=device)

    def _sync_zones(self, site_name: str, cam: str) -> None:
        """A zone is occupied iff any still-active event lists it in current_zones."""
        occupied: set[str] = set()
        ckey = camera_key(site_name, cam)
        for c, zones in self._events.values():
            if c == ckey:
                occupied |= zones
        device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
        self._camera_zones.setdefault(ckey, set()).update(occupied)
        for zone in self._camera_zones.get(ckey, set()) | occupied:
            self._register_zone(site_name, cam, zone)
            zid = f"{device}:zone:{slug(zone)}"
            self._pub(zid, "occupancy", zone in occupied, name=zone, device=device)

    # ---- HTTP: camera + zone roster from /api/config ------------------------

    async def _config_refresh_loop(self) -> None:
        while True:
            try:
                if self._cfg is not None:
                    await self._cfg.load()
                    await self._refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("frigate: config refresh failed")
            await asyncio.sleep(self._refresh_s())

    async def _refresh_all(self) -> None:
        await self.broker.call("frigate_migrate")
        sites = self._sites()
        if not sites:
            self.status.idle("add a Frigate location in Settings → Adapters")
            return
        await asyncio.gather(*(self._refresh_site(s) for s in sites), return_exceptions=True)
        # Aggregate the per-site outcomes into the single adapter badge (fail loud:
        # any site down → the badge is an error naming which location failed).
        alive = {s["name"] for s in sites}
        for gone in [n for n in self._site_status if n not in alive]:
            self._site_status.pop(gone, None)
        # Tear down a de-configured site's HTTP session + camera-tracking state.
        # Until now only stop() did this, so removing a location in Settings leaked
        # its aiohttp session (open forever) and orphaned its camera/zone/switch
        # bookkeeping. Mirror the MQTT-plane teardown, on the HTTP plane.
        known = set(self._sessions) | set(self._authed) | set(self._cam_site.values())
        for gone in known - alive:
            await self._forget_site(gone)
        parts = [f"{n}: {d}" for n, (ok, d) in sorted(self._site_status.items())]
        if self._site_status and all(ok for ok, _ in self._site_status.values()):
            self.status.ok(" · ".join(parts))
        elif self._site_status:
            self.status.error(" · ".join(parts))

    async def _forget_site(self, name: str) -> None:
        """Release a removed site's HTTP session + prune every camera it owned (and
        that camera's zone/switch bookkeeping), so a de-configured location frees
        its resources instead of leaking them until stop()."""
        sess = self._sessions.pop(name, None)
        if sess is not None:
            with contextlib.suppress(Exception):
                await sess.close()
        self._authed.pop(name, None)
        gone = {cslug for cslug, s in self._cam_site.items() if s == name}
        for cslug in gone:
            self._cam_site.pop(cslug, None)
            self._cam_name.pop(cslug, None)
            self._known_cameras.discard(cslug)
            self._camera_zones.pop(cslug, None)
            self._known_zones = {z for z in self._known_zones if not z.startswith(f"{cslug}:")}
            self._known_features = {f for f in self._known_features if not f.startswith(f"{cslug}:")}
            self._feature_cap = {k: v for k, v in self._feature_cap.items() if not k.startswith(f"{cslug}:")}
            self._known_measures = {m for m in self._known_measures
                                    if not m.startswith(f"{NAMESPACE}:{cslug}:")}
        for event_key, (camera, _zones) in list(self._events.items()):
            if camera in gone:
                self._events.pop(event_key, None)
                self._event_ts.pop(event_key, None)
        log.info("frigate[%s]: site removed — released session + %d camera(s)", name, len(gone))

    async def _refresh_site(self, site: dict) -> None:
        name, base = site["name"], site["url"]
        site_name = name
        try:
            cfg = await self._fetch_config(site)
            cameras = (cfg or {}).get("cameras")
            if not isinstance(cameras, dict):
                self._site_status[name] = (False, "no camera roster")
                return
            go2rtc, authed = site["go2rtc"], bool(site["user"] and site["password"])
            published = 0
            for cam, cam_cfg in cameras.items():
                if not isinstance(cam_cfg, dict) or cam_cfg.get("enabled") is False:
                    continue
                published += 1
                self._register_camera(name, cam, cam_name=cam)
                device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
                self._pub(device, "camera", _descriptor(base, go2rtc, cam, name, authed=authed),
                          name=self._camera_name(site_name, cam), device=device)
                zones = cam_cfg.get("zones")
                if isinstance(zones, dict):
                    self._camera_zones[camera_key(site_name, cam)] = set(zones.keys())
                    for zone in zones:
                        self._register_zone(name, cam, zone)
            self._site_status[name] = (True, f"{published} cam")
        except Exception as exc:
            self._site_status[name] = (False, str(exc) or "unreachable")
            log.warning("frigate[%s]: roster refresh failed: %s", name, exc, exc_info=True)

    def _session(self, site: dict) -> aiohttp.ClientSession:
        name = site["name"]
        sess = self._sessions.get(name)
        if sess is None or sess.closed:
            # unsafe=True: the default cookie jar drops cookies for IP-addressed hosts
            # (RFC 2109), so a Frigate at http://<ip>:8971 would lose its frigate_token
            # after login → a permanent 401 refresh loop.
            sess = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
            self._sessions[name] = sess
        return sess

    async def _login(self, site: dict) -> bool:
        """POST /api/login; the site session's cookie jar stores the frigate_token
        so every subsequent request carries it. Returns success."""
        user, pw = site["user"], site["password"]
        if not (user and pw):
            return False
        try:
            async with self._session(site).post(
                f"{site['url'].rstrip('/')}/api/login", json={"user": user, "password": pw},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    self._authed[site["name"]] = True
                    return True
                log.warning("frigate[%s]: login failed (HTTP %s)", site["name"], resp.status)
                return False
        except Exception as exc:
            log.warning("frigate[%s]: login error: %s", site["name"], exc, exc_info=True)
            return False

    async def _fetch_config(self, site: dict) -> dict | None:
        """GET /api/config, logging in first (and re-logging on a 401) when the site
        has credentials (the authenticated :8971 port)."""
        url = f"{site['url'].rstrip('/')}/api/config"
        auth = bool(site["user"] and site["password"])
        if auth and not self._authed.get(site["name"]):
            await self._login(site)
        async with self._session(site).get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 401 and auth:
                await self._login(site)
                async with self._session(site).get(url, timeout=aiohttp.ClientTimeout(total=10)) as r2:
                    r2.raise_for_status()
                    return await r2.json(content_type=None)
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---- staleness sweep ----------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                self._sweep_stale()
            except Exception:
                log.exception("frigate: stale sweep failed")

    def _sweep_stale(self) -> None:
        """Frigate `end` events are reliable, but a dropped MQTT frame would leak
        an event → zone stuck occupied. Drop events with no update past the TTL."""
        ttl_ns = self._track_ttl() * 1_000_000_000
        now = time.time_ns()
        stale = [eid for eid, ts in self._event_ts.items() if now - ts > ttl_ns]
        cams: set[str] = set()
        for eid in stale:
            entry = self._events.pop(eid, None)
            self._event_ts.pop(eid, None)
            if entry:
                cams.add(entry[0])
        for ckey in cams:
            site_name = self._cam_site[ckey]
            cam = self._cam_name[ckey]
            self._sync_zones(site_name, cam)
            if not any(c == ckey for c, _z in self._events.values()):
                device = f"{NAMESPACE}:{camera_key(site_name, cam)}"
                self._pub(device, "object_class", "none",
                          name=self._camera_name(site_name, cam), device=device)

    # ---- commands (feature switches) ----------------------------------------

    async def handle_command(self, command: Command) -> None:
        parts = command.entity_id.split(":")
        if len(parts) != 4:
            raise CommandRejected(f"{command.entity_id} is read-only")
        cam_slug, feature = f"{parts[1]}:{parts[2]}", parts[3]
        cap = self._feature_cap.get(f"{cam_slug}:{feature}")
        if cap is None or command.capability != cap:
            raise CommandRejected(f"{command.entity_id} has no writable {command.capability}")
        site_name = self._cam_site.get(cam_slug, "")
        client = self._clients.get(site_name)
        if client is None:
            raise CommandRejected(f"site {site_name!r} not connected")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        value = self._feature_set_value(command, cap)
        if value is None:
            raise CommandRejected(f"no /set payload for {cap}/{command.command} {dict(command.args)}")
        cam = self._cam_name.get(cam_slug, cam_slug)
        prefix = next((s.get("topic_prefix") or "frigate"
                       for s in self._sites() if s["name"] == site_name), "frigate")
        await client.publish(f"{prefix}/{cam}/{feature}/set", value)

    def _feature_set_value(self, command: Command, cap: str) -> str | None:
        """The MQTT `/set` payload for a feature command, or None to drop. on_off →
        ON/OFF (toggle reads the last state); number → the numeric value as text."""
        if cap == "on_off":
            if command.command == "turn_on":
                return "ON"
            if command.command == "turn_off":
                return "OFF"
            if command.command == "toggle":
                return "OFF" if self._last.get((command.entity_id, "on_off")) is True else "ON"
            return None
        if cap == "number" and command.command == "set_value":
            n = _as_num(str((command.args or {}).get("value")))
            if n is None:
                return None
            # Frigate wants an int for a count/threshold; keep a real fractional value.
            return str(int(n)) if n.is_integer() else str(n)
        return None

    # ---- entity registration + emit -----------------------------------------

    def _camera_name(self, site_name: str, cam: str) -> str:
        return _prettify(self._cam_name.get(camera_key(site_name, cam)) or cam)

    def _register_camera(self, site_name: str, camera: str, cam_name: str | None = None) -> None:
        cslug = camera_key(site_name, camera)
        learned = bool(cam_name) and self._cam_name.get(cslug) != cam_name
        self._cam_name[cslug] = cam_name or self._cam_name.get(cslug) or camera
        self._cam_site[cslug] = site_name
        if cslug in self._known_cameras and not learned:
            return
        self._known_cameras.add(cslug)
        if self._bus is None:
            return
        device = f"{NAMESPACE}:{cslug}"
        site = next((s for s in self._sites() if s["name"] == site_name), None)
        native = hashlib.sha256(f"{site['url'].rstrip('/')}\0{camera}".encode()).hexdigest() if site else None
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=device, adapter=NAMESPACE,
            capabilities=["camera", "motion", "person_count", "object_class"],
            name=self._camera_name(site_name, camera), device=device,
            device_name=self._cam_name.get(cslug), site=site_name,
            native_key=native,
        )), log=log, name=f"announce {device}")

    def _register_zone(self, site_name: str, camera: str, zone: str) -> None:
        key = f"{camera_key(site_name, camera)}:{slug(zone)}"
        if key in self._known_zones or self._bus is None:
            return
        self._known_zones.add(key)
        device = f"{NAMESPACE}:{camera_key(site_name, camera)}"
        zid = f"{device}:zone:{slug(zone)}"
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=zid, adapter=NAMESPACE, capabilities=["occupancy"],
            name=zone, device=device, device_name=self._cam_name.get(camera_key(site_name, camera)),
            site=site_name,
        )), log=log, name=f"announce {zid}")

    def _register_feature(self, site_name: str, camera: str, feature: str, cap: str) -> None:
        key = f"{camera_key(site_name, camera)}:{slug(feature)}"
        self._feature_cap[key] = cap  # remember cap for command routing (idempotent)
        if key in self._known_features or self._bus is None:
            return
        self._known_features.add(key)
        device = f"{NAMESPACE}:{camera_key(site_name, camera)}"
        entity = f"{device}:{slug(feature)}"
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=entity, adapter=NAMESPACE, capabilities=[cap],
            name=f"{self._camera_name(site_name, camera)} {feature}", device=device,
            device_name=self._cam_name.get(camera_key(site_name, camera)), category="config",
            site=site_name,
        )), log=log, name=f"announce {entity}")

    def _register_measurement(self, site_name: str, camera: str, entity: str, name: str) -> None:
        if entity in self._known_measures or self._bus is None:
            return
        self._known_measures.add(entity)
        device = f"{NAMESPACE}:{camera_key(site_name, camera)}"
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=entity, adapter=NAMESPACE, capabilities=["measurement"],
            name=name, device=device, device_name=self._cam_name.get(camera_key(site_name, camera)),
            diagnostic=True, category="diagnostic", site=site_name,
        )), log=log, name=f"announce {entity}")

    def _pub(self, entity_id: str, capability: str, value, *,
             name: str | None = None, device: str | None = None) -> None:
        if self._bus is None:
            return
        key = (entity_id, capability)
        if self._last.get(key) == value:
            return
        bus, ts = self._bus, time.time_ns()

        async def _publish() -> None:
            # Cache as "published" ONLY after the publish succeeds. Caching before
            # this fire-and-forget send permanently loses the value on a failed
            # publish: the stale sweeper re-emits it but this dedup (already cached)
            # swallows it. Awaiting here, then caching on success, lets a later
            # sweep genuinely retry.
            await bus.publish_state(StateUpdate(
                entity_id=entity_id, capability=capability, value=value,
                adapter=NAMESPACE, ts_ns=ts, name=name, device=device,
            ))
            self._last[key] = value

        spawn(_publish(), log=log, name=f"publish {entity_id}.{capability}")


def _prettify(cam: str) -> str:
    return cam.replace("_", " ").replace("-", " ").strip().title() or cam
