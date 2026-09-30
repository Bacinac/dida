"""DIDA BABA adapter — a THIN MIRROR of BABA's authoritative vision state.

BABA owns the vision truth. It has the frames, the detector, the tracker and the
zone geometry, so IT decides what is true — is a zone occupied, how many people are
there, what class is in view, what a scene reads — INCLUDING every hysteresis,
debounce and cooldown behind those values. DIDA cannot second-guess that from the
outside, and must not try: two tuning surfaces = two sources of truth.

So this adapter DERIVES NOTHING. There is no track state-machine, no motion latch,
no occupancy off-delay, no staleness sweep, no per-class hiding, no class folding,
no value canonicalisation. It mirrors the state BABA pushes onto the DIDA bus
(validated at the boundary, like any adapter) and publishes the camera/zone catalog
from BABA's roster. ALL tuning lives in BABA, where the pixels are.

Three planes:

  1. STATE (`baba.state.<camera_uuid>`) — the authoritative per-camera SNAPSHOT
     BABA pushes on every change. The ONLY source of values; mirrored 1:1. A
     snapshot (not a delta) is self-healing: a dropped message is corrected by the
     next one — which is exactly why DIDA needs no sweeps or TTLs.
  2. ROSTER (`baba.roster` + request/reply `baba.roster.get`) — the inventory:
     every camera + zone up front (not only ones that have fired), each camera's
     stable UUID and display name. Drives the entity catalog + prune.
  3. MEDIA — pixels never touch the bus: each camera carries a `camera` descriptor
     (go2rtc live + BABA's REST archive); the browser pulls frames through DIDA's
     own proxy, which presents the peer key server-side.

Identity is BABA's UUID, NEVER a name — for the camera (`baba:<uuid>`) and one level
down (`baba:<uuid>:zone:<zone-uuid>`, `baba:<uuid>:scene:<region-uuid>`). The snapshot
keys zones and scenes by that UUID and the roster maps it to a name, so a rename in
BABA changes only the display name — the entity, its automations and its floor-plan
placement are untouched.

One adapter, several INSTALLS: a house and a holiday home each run their own BABA,
so the config is a list of locations (`sites`) rather than one set of hosts — the
same shape the frigate adapter settled on. Each location keeps its own NATS
connection, its own go2rtc + archive credentials and its own slug→UUID map (slugs
are per-install and DO collide; the UUID does not). Entities stay flat `baba:<uuid>`
with the location's name in the camera descriptor, so adding a second install
migrates nothing about the first.

DIDA commands BABA in exactly ONE place: the floodlight on a camera that has one.
Turning a lamp on is not vision tuning — whether it is dark outside is a fact about
the house, measured by a sensor DIDA already owns, while the camera's credentials
and its vendor API live in BABA, which therefore stays the only writer to the
device. That command travels over BABA's peer-authenticated HTTP API and NEVER the
bus: the NATS peer is deliberately read-only, because a mirror's credential must be
able to watch and not to drive. Everything else here remains read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from functools import partial
from urllib.parse import urlsplit

import aiohttp
import nats
from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    answer_discover,
    forget_reachable,
    probe_hosts,
    set_reachable,
    subnet_hosts,
    validate_command,
)
from dida_core.baba_sites import BabaSite, incomplete_sites, parse_sites
from dida_core.journal import JournalKind, emit_journal
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.baba")


def _spell_duration(seconds) -> str:
    """"14 h 50 min" — the stay, as a phrase, for the line that reports it."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{total} s"
    h, m = divmod(total // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"

NAMESPACE = "baba"

# BABA's authoritative per-camera state snapshot, one subject per camera UUID.
STATE_SUBJECT = "baba.state.*"
# A doorbell PRESS is momentary, not state — its own subject, per camera UUID.
BELL_SUBJECT = "baba.bell.*"
# Arrivals and departures. Their own subject, not the event stream: BABA files
# ~29500 `object_parked` rows a week and two of these, and one site is behind a
# 0.79 Mbit/s uplink.
PLACE_SUBJECT = "baba.place.*"
PLACE_KINDS: tuple[JournalKind, ...] = ("vehicle_arrived", "vehicle_left")
# Full inventory: pushed on every change + served on request (we may start later).
ROSTER_SUBJECT = "baba.roster"
ROSTER_REQUEST = "baba.roster.get"
# BABA's mirror account may subscribe to replies under this prefix only. The
# default `_INBOX` is shared with BABA's own services, whose replies a mirror must
# neither read nor answer.
INBOX_PREFIX = "_INBOX.dida"
# BABA re-publishes every camera's snapshot each 60 s even when nothing changes, so
# a location silent for longer than two and a half of those has stopped seeing.
SILENCE_S = 150

# Sub-entities that arrive INLINE in the state snapshot rather than in the roster:
# recognised people (`identities`) and pets/vehicles (`object_identities`). They
# self-manage — announced on first sight, set "absent" when a snapshot stops
# listing them — so the roster-driven prune must never treat them as stale. The
# roster does not list them, which means "not in the roster" is always true for
# them and the prune would delete every one on every refresh. `person` was skipped
# by name; `object` was added later and was NOT, so pets and vehicles were dropped
# on each roster refresh and re-announced on the next sighting, losing whatever
# DIDA held for them. Add any future inline kind HERE, not to a second condition.
_INLINE_KINDS = ("person", "object")

# Camera-level fields mirrored straight from the snapshot onto the camera entity.
# BABA emits DIDA's canonical vocabulary (object_class person/vehicle/animal/other/
# none), so there is nothing to translate — the engine validates at the boundary and
# rejects loudly if BABA ever drifts off-contract.
# `light_condition` rides here too: BABA measures how much light the camera itself
# has (frame luma + IR ratio, debounced there) and the field is simply ABSENT when it
# is not asserting a band — which the loop below already honours, and which must NOT
# be read as darkness. It is the only instrument in the house that reaches a camera's
# dark: the weather station's lowest rung is 10 lux and the camera still sees colour
# under it.
_CAM_CAPS = ("motion", "person_count", "object_class", "light_condition")

# The floodlight is its OWN entity beside the camera, like a zone or the doorbell
# button — never a capability on the camera entity. `on_off` there would classify
# the camera as a light (capabilities.classify_device_type), and the wall would lose
# the camera it is named after.
_LIGHT_NAME = "Floodlight"
_LIGHT_SUFFIX = ":light"
# BABA reads the lamp off the DEVICE on every request, so this is how quickly DIDA
# learns about a change made outside it — the vendor's app, or the camera's own
# schedule, which stays in place as the fallback for the nights DIDA is down.
_LIGHT_POLL_S = 60
# A camera model with no white LED answers 404. So does a BABA that does not yet
# serve this endpoint — the two halves of the feature deploy independently, in
# either order — and holding that verdict for the life of the process would leave a
# house whose lamps are simply never announced, with nothing in the log after the
# first minute. So a "no lamp" answer is re-tested on this interval instead. Any
# OTHER failure leaves the verdict open and is retried on the very next pass: a
# camera that was briefly away is not a camera without a lamp.
_NO_LIGHT = 404
_NO_LIGHT_RECHECK_S = 1800
# A camera that fails to answer three times running is rested the same way, unless a
# lamp has actually been read off it — that is a regression and keeps its minute. The
# patio's camera is not a Reolink at all: BABA reaches through to the device and gets
# nothing, which is a 502, not a 404, and it will be a 502 forever.
_LIGHT_FAILS_BEFORE_REST = 3
# …and however often it is retried, it is COMPLAINED about only this often. Three
# cameras that cannot answer would otherwise write four thousand identical lines a
# day into the log, which is not louder than one line, it is quieter.
_LIGHT_COMPLAIN_S = 1800
# A malformed message comes back with every snapshot, several a second per camera:
# one warning per kind of drop a minute says so without burying the log.
_DROP_COMPLAIN_S = 60


def _descriptor_site(value: object) -> str | None:
    """The `site` a stored camera descriptor names (the value is its JSON text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value.get("site") if isinstance(value, dict) else None


# go2rtc (host-net on the BABA box) exposes each camera under its stream name.
# These are the canonical API paths; the UI pulls pixels straight from here.
def _descriptor(go2rtc: str, name: str, api_url: str = "", baba_id: str = "",
                site: str = "") -> str:
    base = go2rtc.rstrip("/")
    desc: dict[str, str] = {
        "stream": name,
        # WebRTC signaling endpoint (go2rtc WHEP/WS) — the primary live path.
        "webrtc": f"{base}/api/ws?src={name}",
        # HLS for clients/browsers without WebRTC.
        "hls": f"{base}/api/stream.m3u8?src={name}",
        # The camera's own H.264/H.265 as fragmented MP4 over one plain HTTP
        # GET — full resolution, no transcode, and it rides any HTTP tunnel.
        "mp4": f"{base}/api/stream.mp4?src={name}",
        # Single JPEG — poster frame / thumbnail.
        "snapshot": f"{base}/api/frame.jpeg?src={name}",
    }
    if api_url and baba_id:
        # The archive plane (BABA REST): event history, per-event thumbnails and
        # recorded clips. `auth: baba` tells DIDA's proxy to present the peer key
        # from this adapter's config server-side — the key itself must NEVER ride
        # in this browser-visible descriptor.
        desc.update(
            baba_id=baba_id,
            # The newest frame the ingestor already decoded for the detector,
            # capped at 960 px. go2rtc's frame.jpeg starts an ffmpeg per request
            # that fails or returns a flat grey frame when a wall asks for
            # every camera at once.
            still=f"{api_url}/cameras/{baba_id}/live.jpg?boxes=false",
            events=f"{api_url}/events",
            thumbs=f"{api_url}/thumbnails",
            clip=f"{api_url}/recordings/cameras/{baba_id}/clip",
            auth="baba",
        )
    if site:
        # Which install this camera belongs to. The wall shows it as a chip, and
        # removing a location resolves its cameras through it.
        desc["site"] = site
    return json.dumps(desc)


def _light_values(payload: object) -> tuple[bool, int | None]:
    """BABA's answer for a floodlight — `{"on": bool, "bright": 0-100}`, read off the
    device on every request. `on` is not optional: without it there is no state to
    mirror, and inventing one would put a switch position on the wall that nobody
    set. `bright` is — a lamp that only switches has no brightness to report."""
    if not isinstance(payload, dict) or not isinstance(payload.get("on"), bool):
        raise RuntimeError(f"light state without `on`: {payload!r}")
    bright = payload.get("bright")
    if isinstance(bright, bool) or not isinstance(bright, (int, float)):
        return bool(payload["on"]), None
    return bool(payload["on"]), max(0, min(100, int(bright)))


class _Site:
    """One BABA install: its config, its live NATS connection, and the catalog it
    has declared. Slug→UUID is per install because go2rtc slugs are only unique
    within one box; everything else is keyed by BABA's UUID, which is not."""

    def __init__(self, cfg: BabaSite) -> None:
        self.key = cfg.key
        self.cfg = cfg
        self.nc = None
        # slug -> UUID of each ENABLED camera: the media plane, where go2rtc carries
        # only those, under their slug.
        self.camera_ids: dict[str, str] = {}
        # Last roster from THIS install. Prune reads the union of all of them, so a
        # location that has not reported yet leaves the set empty and blocks it.
        self.roster: set[str] = set()
        self.live_subs: set[str] = set()
        self.error: str = ""
        # Media plane (go2rtc) failure, kept apart from `error` (the NATS
        # plane): one can be down while the other is fine, and the badge must say which.
        self.media_error: str = ""
        # Last state snapshot from this install; the silence clock starts at connect.
        self.heard = time.monotonic()

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def nats_url(self) -> str:
        return self.cfg.nats_url


class BabaAdapter:
    """Mirrors BABA's pushed vision state onto the DIDA bus. Implements
    `dida_core.Adapter`; read-only — handle_command is a no-op (see module docs)."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self.broker = None  # set by the runner
        self._session = None
        # Identity: BABA's camera UUID — unique across installs, so every catalog
        # below stays flat. Only the slug -> uuid map (the media plane's, since
        # go2rtc knows slugs) is per install: slugs collide between two BABAs.
        self._sites: dict[str, _Site] = {}       # site key -> live location
        self._camera_names: dict[str, str] = {}  # uuid -> authoritative BABA name
        # Identity is the UUID one level down too: the state snapshot keys zones and
        # scenes by THEIR uuid, and the roster maps that uuid -> display name. So a
        # zone/scene rename in BABA is pure display here — it never re-keys.
        self._zone_names: dict[str, str] = {}    # zone uuid -> name
        self._scene_names: dict[str, str] = {}   # scene-region uuid -> name
        # scene-region uuid -> the PLACE it watches (None for a region that
        # stands for itself, like a gate). `scenes` is keyed by region uuid and
        # `parked` by place, and this is the only thing that joins them: two
        # regions — Shed's view and West's — watch the one place, and matching
        # the display name against the parked key worked only while every
        # region happened to be called P1..P4.
        self._scene_places: dict[str, str] = {}
        # region → the camera that declares it: place names repeat across cameras and
        # installs (every parking lot has a P1), so a place resolves per camera.
        self._scene_cams: dict[str, str] = {}
        # Zones BABA's roster declares switched off. BABA stops evaluating them, so
        # the last value it sent would otherwise stand forever.
        self._disabled_zones: set[str] = set()
        self._known_cameras: set[str] = set()    # uuids announced
        self._known_zones: set[str] = set()      # zone entity_ids announced
        self._known_scenes: set[str] = set()     # scene-region entity_ids announced
        self._known_bells: set[str] = set()      # bell entity_ids announced
        # The floodlight: entity ids announced, the state last read off the device
        # (`toggle` has to know what it is toggling), and which cameras have a lamp
        # at all. The state cache is a mirror of the device, never a substitute for
        # it — every command reads the answer back and publishes THAT.
        self._known_lights: set[str] = set()     # light entity_ids announced
        self._light_state: dict[str, tuple[bool, int]] = {}  # camera uuid -> (on, bright)
        self._has_light: dict[str, bool] = {}    # camera uuid -> the device has a lamp
        self._lamp_recheck: dict[str, float] = {}  # camera uuid -> not before this monotonic time
        self._light_fails: dict[str, int] = {}   # camera uuid -> reads that failed in a row
        self._light_said: dict[str, float] = {}  # camera uuid -> when it may be complained about again
        self._drop_said: dict[str, float] = {}   # kind of drop -> when it may be warned about again
        # Persons are NOT roster catalog — they appear inline in the state snapshot
        # (identities: {gid: name}), so they self-manage: announced on first sight,
        # set "absent" when a snapshot no longer lists them, dropped only with their
        # camera. gid is a STABLE re-ID id, so a person keeps one entity per camera.
        self._known_persons: set[str] = set()    # person entity_ids announced
        self._person_names: dict[str, str] = {}  # gid -> name (last inline value, for absent pubs)
        # Pets/vehicles mirror the person model exactly, but from the parallel
        # `object_identities` map (kept separate from `identities` so people-logic
        # never sees a car as a person). Same self-managing lifecycle.
        self._known_objects: set[str] = set()    # pet/vehicle entity_ids announced
        self._object_names: dict[str, str] = {}  # gid -> name (last inline value)
        self._last: dict[tuple[str, str], object] = {}  # (entity, cap) -> last published
        self._reach: dict[str, bool] = {}  # camera uuid -> reachability last asserted

    # ------------------------------------------------------------------ config
    def _incomplete_sites(self) -> list[str]:
        """Names of configured locations that can never connect because they have no
        NATS address. `parse_sites` drops them, which used to make an entry added
        from a scan (before the scan could find the peer port) look like it had
        simply not saved — so it got typed in again. Silence is what made it a
        mystery; the card names them instead."""
        return incomplete_sites(self._cfg.get("sites") if self._cfg else None)

    def _configured_sites(self) -> list[BabaSite]:
        """No defaults anywhere. An unset location is a configuration error to SAY,
        not a reason to quietly try someone else's box: the old `127.0.0.1:4222` and
        `198.51.100.200` defaults OUTLIVED the topology that made them true, so a
        missed config pointed the mirror at a corpse while looking configured."""
        return parse_sites(self._cfg.get("sites") if self._cfg else None)

    def _ok_status(self) -> None:
        """One badge for every location: `Kuća: 7 cams · Cabin: 2 cams`. A location
        that is down names itself in the error — with several installs, "disconnected"
        alone leaves you guessing which house went dark."""
        # A location that cannot connect at all is said out loud, whether or not any
        # other location is healthy.
        incomplete = self._incomplete_sites()
        if incomplete:
            self.status.error(
                " · ".join(f"{n}: no NATS address — the location cannot connect"
                           for n in incomplete))
            return
        if not self._sites:
            # An installation with no location yet is onboarding, not broken — that
            # is what `idle` means everywhere else (frigate says the same on its
            # card). A location that IS configured and has no live connection is a
            # fault, and stays loud.
            if self._configured_sites():
                self.status.error("configured location is not connected")
            else:
                self.status.idle("add a BABA location in Settings → Adapters")
            return
        broken = [f"{s.name}: {s.error or s.media_error}"
                  for s in self._sites.values() if s.error or s.media_error]
        if broken:
            self.status.error(" · ".join(broken))
            return
        parts = []
        for s in self._sites.values():
            cams = sum(1 for cid in self._known_cameras if cid in s.roster)
            parts.append(f"{s.name}: {cams} cams")
        self.status.ok(" · ".join(parts))

    def _cam_dev(self, camera_id: str) -> str:
        """`baba:<UUID>` — the stable entity id. BABA's UUID IS the identity."""
        return f"{NAMESPACE}:{camera_id}"


    # ---------------------------------------------------------------- discovery
    # A BABA install names itself: its api answers /version on 8080 with
    # `product: "baba"`. What CANNOT be found is the peer key — a shared secret that
    # exists only inside that install's config — so a scan fills in everything else
    # and leaves that one field to paste.
    API_PORT = 8080
    GO2RTC_PORT = 1984
    NATS_PORT = 4222
    # BABA's peer plane. Plain NATS is usually loopback-only; what an install opens
    # to its network is the WebSocket port, which is what a second box (here) is
    # meant to use — so a scan that only looked at 4222 declared "not exposed" for
    # the very topology BABA ships.
    NATS_WS_PORT = 4223

    async def _on_discover(self, msg) -> None:
        await answer_discover(self._bus, msg, "baba", self._discover)

    @staticmethod
    async def _port_open(ip: str, port: int, timeout_s: float = 0.6) -> bool:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout_s)
        except (OSError, TimeoutError):
            return False
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()
        return True

    async def _identify(self, ip: str) -> dict | None:
        if not await self._port_open(ip, self.API_PORT):
            return None
        assert self._session is not None
        try:
            async with self._session.get(f"http://{ip}:{self.API_PORT}/version",
                                         timeout=aiohttp.ClientTimeout(total=4)) as r:
                about = await r.json() if r.status == 200 else None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        if not isinstance(about, dict) or about.get("product") != "baba":
            return None
        version = str(about.get("version") or "")
        nats_url = ""
        if await self._port_open(ip, self.NATS_PORT):
            nats_url = f"nats://{ip}:{self.NATS_PORT}"
        elif await self._port_open(ip, self.NATS_WS_PORT):
            nats_url = f"ws://{ip}:{self.NATS_WS_PORT}"
        return {"ip": ip, "version": version, "nats_url": nats_url}

    async def _discover(self, subnets: list[str]) -> dict:
        found = await probe_hosts(subnet_hosts(subnets), self._identify)
        configured = {urlsplit(s.nats_url).hostname for s in self._configured_sites()}
        devices = []
        for f in found:
            ip = f["ip"]
            if ip in configured:
                continue
            ver = f" · v{f['version']}" if f["version"] else ""
            note = ("" if f["nats_url"]
                    else " · no reachable NATS port — enter its address yourself")
            devices.append({
                "label": f"BABA at {ip}{ver}{note}",
                "appendJson": {"sites": {
                    "name": f"BABA {ip.rsplit('.', 1)[-1]}",
                    "nats_url": f["nats_url"],
                    "api_url": f"http://{ip}:{self.API_PORT}",
                    "go2rtc": f"http://{ip}:{self.GO2RTC_PORT}",
                }},
            })
        log.info("baba discover: %d install(s) across %s", len(devices), ", ".join(subnets) or "nothing")
        return {"devices": devices}

    # ------------------------------------------------------------------- start
    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        await bus.nc.subscribe("dida.discover.baba", cb=self._on_discover)
        self._cfg = AdapterConfig("baba", self.broker)
        self._session = aiohttp.ClientSession()
        # Load ONCE before anything reads config: the camera refresh loop used to start
        # against an empty config and only the stale `.200` default made that look like
        # an unreachable host rather than a race. Poll after, for live Settings edits.
        await self._cfg.load()
        # Reload who we've announced BEFORE subscribing: presence absence is
        # inferred from "in _known_persons, not in this snapshot", and that set
        # is in-memory — so a restart would orphan every person present before
        # it (their badge hangs forever, never cleared). Seed it from the DB; the
        # BABA state heartbeat then delivers each camera's current snapshot and
        # the absent pass clears anyone no longer there.
        await self._rebuild_known_persons()
        await self._rebuild_known_objects()
        spawn(self._cfg.poll_loop(), log=log, name="baba config poll")  # live edits
        spawn(self._camera_refresh_loop(), log=log, name="baba camera refresh")
        spawn(self._light_refresh_loop(), log=log, name="baba light refresh")
        spawn(self._silence_loop(), log=log, name="baba silence watch")

        # Supervise (presence/mqtt pattern): an edited location reconnects only
        # itself, so a Settings → Adapters change applies live and touching one
        # house never interrupts the mirror of another.
        active: dict[str, tuple] = {}
        while True:
            try:
                await self._cfg.load()
                configured = {c.key: c for c in self._configured_sites()}
                for key in list(self._sites):
                    if key not in configured:
                        await self._drop_site(key)
                        active.pop(key, None)
                for key, cfg in configured.items():
                    site = self._sites.get(key)
                    dead = site is not None and (site.nc is None or site.nc.is_closed)
                    if site is None or active.get(key) != cfg or dead:
                        try:
                            await self._connect_site(_Site(cfg))
                        except Exception as exc:
                            await self._cameras_unreachable(
                                await self._cameras_of(cfg.name),
                                f"{cfg.name}: cannot connect ({exc})")
                            raise
                        # Mark active ONLY after a successful connect — else a failed
                        # first connect (that box down) would leave cfg == active,
                        # dead == False, and reconnect would never be retried (wedge).
                        active[key] = cfg
                if not configured:
                    # Say it plainly on the card. Guessing a URL here is how a stale
                    # default keeps an unconfigured adapter looking merely "offline".
                    self.status.idle("add a BABA location in Settings → Adapters")
                else:
                    self._ok_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                active.clear()  # a connect failed — force a retry on the next tick
                log.exception("baba: supervise loop error")
                self.status.error(str(exc) or "supervise error")
            await asyncio.sleep(10)

    async def _drop_site(self, key: str) -> None:
        """Stop mirroring a location the config no longer lists. Its entities stay:
        removing a location from the UI deletes them explicitly (and knowingly), so
        a mistyped edit here can never wipe a house's cameras as a side effect."""
        site = self._sites.pop(key, None)
        if site is None:
            return
        if site.nc is not None:
            with contextlib.suppress(Exception):
                await site.nc.close()
        for cid in site.roster:
            self._reach.pop(cid, None)
            forget_reachable(self._cam_dev(cid), NAMESPACE)
        log.info("baba: stopped mirroring %s (no longer configured)", site.name)

    async def _connect_site(self, site: _Site) -> None:
        """(Re)open one location's subscriptions. nats-py owns reconnection
        (infinite), so we only re-run this on an explicit config change."""
        url = site.nats_url
        host = urlsplit(url).hostname or url
        self.status.connecting(f"{site.name} ({host})")
        old = self._sites.get(site.key)
        if old is not None and old.nc is not None:
            with contextlib.suppress(Exception):
                await old.nc.close()

        async def _disconnected() -> None:
            log.warning("baba: NATS disconnected (%s @ %s)", site.name, host)
            site.error = f"{host} disconnected"
            self._ok_status()
            await self._cameras_unreachable(site.roster, f"{site.name}: {host} disconnected")

        async def _reconnected() -> None:
            log.info("baba: NATS reconnected (%s @ %s)", site.name, host)
            site.error = ""
            site.heard = time.monotonic()
            self._ok_status()
            # BABA pushes the roster only on its own connect; ours missed any change
            # made while the link was down.
            await self._request_roster(site)

        site.nc = await nats.connect(
            url,
            user=site.cfg.nats_user or None,
            password=site.cfg.nats_password or None,
            name=f"dida-adapter-baba/{site.key}",
            inbox_prefix=INBOX_PREFIX,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            disconnected_cb=_disconnected,
            reconnected_cb=_reconnected,
        )
        self._sites[site.key] = site
        site.error = ""
        await site.nc.subscribe(ROSTER_SUBJECT, cb=partial(self._on_roster, site))
        # Pull the roster BEFORE subscribing to state, and AWAIT it. BABA publishes it
        # once on its own connect, which we may have missed by starting later;
        # request/reply closes that gap, and live changes keep arriving on
        # ROSTER_SUBJECT. Ordering matters: the roster is the only name source for
        # zones and scenes, so a snapshot landing first used to name entities after
        # their raw UUID. Awaiting here means a snapshot always meets a known catalog.
        await self._request_roster(site)
        await site.nc.subscribe(STATE_SUBJECT, cb=partial(self._on_state, site))
        await site.nc.subscribe(BELL_SUBJECT, cb=partial(self._on_bell, site))
        await site.nc.subscribe(PLACE_SUBJECT, cb=partial(self._on_place, site))
        # Names every subject, so this line stays the answer to "what is this
        # mirror actually reading" — it listed three while subscribing to four.
        log.info("baba: subscribed %s on %s (%s)",
                 " + ".join((STATE_SUBJECT, BELL_SUBJECT, PLACE_SUBJECT, ROSTER_SUBJECT)),
                 host, site.name)
        self._ok_status()

    async def _request_roster(self, site: _Site) -> None:
        import nats.errors

        if site.nc is None:
            return
        try:
            reply = await site.nc.request(ROSTER_REQUEST, b"", timeout=8)
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as exc:
            log.warning("baba: roster request got no reply from %s (%s) — zones and scenes "
                        "stay unnamed and their state is skipped until it publishes one",
                        site.name, exc)
            return
        await self._on_roster(site, reply)

    async def _silence_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for site in list(self._sites.values()):
                quiet = now - site.heard
                if quiet > SILENCE_S:
                    await self._cameras_unreachable(
                        site.roster, f"{site.name}: no state from BABA for {int(quiet)} s")

    async def _cameras_of(self, site_name: str) -> list[str]:
        """The cameras a location reported before, from their descriptors — all
        there is to go on when that location cannot even be connected to."""
        if self.broker is None:
            return []
        rows = await self.broker.call("state", prefix=f"{NAMESPACE}:", capabilities=["camera"])
        return [r["entity_id"].removeprefix(f"{NAMESPACE}:") for r in rows
                if _descriptor_site(r["value"]) == site_name]

    async def _cameras_unreachable(self, cameras, detail: str) -> None:
        """Every camera of a location that stopped talking: its last snapshot is no
        longer anybody's truth, so a zone must not stay occupied on it."""
        fresh = [cid for cid in cameras if self._reach.get(cid) is not False]
        if fresh:
            log.warning("baba: %s — %d cameras unreachable", detail, len(fresh))
        for cid in fresh:
            self._reach[cid] = False
            await set_reachable(self._bus, self._cam_dev(cid), NAMESPACE, False, detail=detail)

    def _dropped(self, kind: str, fmt: str, *args) -> None:
        now = time.monotonic()
        if now >= self._drop_said.get(kind, 0.0):
            self._drop_said[kind] = now + _DROP_COMPLAIN_S
            log.warning(fmt, *args)

    # ------------------------------------------------------------- state plane
    async def _on_state(self, site: _Site, msg) -> None:
        """Mirror BABA's per-camera state snapshot. No interpretation: whatever
        BABA says is true IS the state (it already applied its own hysteresis)."""
        site.heard = time.monotonic()
        try:
            st = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            self._dropped(f"state:{msg.subject}", "baba: undecodable state on %s", msg.subject)
            return
        cid = st.get("camera_id") if isinstance(st, dict) else None
        if not isinstance(cid, str) or not cid:
            self._dropped(f"state:{msg.subject}", "baba: state on %s names no camera", msg.subject)
            return
        name = st.get("camera_name")
        if isinstance(name, str) and name:
            self._camera_names[cid] = name
        dev = self._cam_dev(cid)
        self._register_camera(cid)
        if self._reach.get(cid) is not True:
            self._reach[cid] = True
            await set_reachable(self._bus, dev, NAMESPACE, True)

        for cap in _CAM_CAPS:
            if cap in st:
                self._pub(dev, cap, st[cap], name=self._camera_names.get(cid), device=dev)

        self._mirror_zones(st, cid, dev)
        self._mirror_scenes(st, cid, dev)
        self._mirror_identities(st, cid, dev)
        self._mirror_objects(st, cid, dev)

    def _mirror_zones(self, st: dict, cid: str, dev: str) -> None:
        # `zones` / `scenes` are keyed by the zone/region UUID and the ROSTER is the only
        # thing that maps that to a name — so an id the roster never declared is skipped
        # LOUDLY. It cannot be a startup race (the roster is awaited before subscribing),
        # so it means BABA is publishing a region it does not list: a real inconsistency,
        # and naming the entity after the raw UUID would only hide it on the wall.
        zones = st.get("zones")
        if isinstance(zones, dict):
            for zid, occupied in zones.items():
                if str(zid) not in self._zone_names:
                    log.warning("baba: state has zone %s on %s, roster does not — skipping",
                                zid, self._camera_names.get(cid, cid))
                    continue
                self._register_zone(cid, str(zid))
                self._pub(f"{dev}:zone:{zid}", "occupancy",
                          bool(occupied) and str(zid) not in self._disabled_zones,
                          name=self._zone_names[str(zid)], device=dev)

    def _mirror_scenes(self, st: dict, cid: str, dev: str) -> None:
        # `scenes` only appears once a region has an evaluated state — BABA omits
        # rather than guessing, so a missing key is "not evaluated", not "unknown".
        scenes = st.get("scenes")
        # `parked` names the vehicle standing in a PLACE, and the roster says
        # which place each region watches — until 28.08 it did not, and this
        # matched the region's DISPLAY NAME against the parked key, which held
        # only because every parking region here is called P1..P4. A region
        # with no place (a gate) has no vehicle, which is the same answer. It
        # rides as its own capability on the scene entity, NOT in the display
        # name: names load once while state flows live, and the first attempt —
        # composing "P1 · Marko's car" into the label — sat invisible on every
        # wall that was already open. The scene's state stays present/empty, so
        # automations keyed on it never see a car come and go.
        parked = st.get("parked")
        parked = parked if isinstance(parked, dict) else {}
        if isinstance(scenes, dict):
            for rid, value in scenes.items():
                if str(rid) not in self._scene_names:
                    log.warning("baba: state has scene %s on %s, roster does not — skipping",
                                rid, self._camera_names.get(cid, cid))
                    continue
                self._register_scene(cid, str(rid))
                label = self._scene_names[str(rid)]
                self._pub(f"{dev}:scene:{rid}", "scene_state", str(value),
                          name=label, device=dev)
                # Occupied-but-unnamed is a real state, distinct from empty:
                # BABA's registry holds `unknown` from the moment a car parks
                # until its plate is read minutes later, and mapping that to ""
                # made the UI fall back to its MEMORY of the previous car — a
                # different vehicle shown over an occupied spot. An empty name
                # with a `since` says "something stands here, not yet known";
                # "" keeps meaning "the place is empty".
                place = self._scene_places.get(str(rid))
                pv = parked.get(place) if place else None
                vehicle = (
                    json.dumps({"name": pv.get("name") or "", "since": pv.get("since")})
                    if isinstance(pv, dict)
                    else ""
                )
                self._pub(f"{dev}:scene:{rid}", "parked_vehicle", vehicle,
                          name=label, device=dev)

    def _mirror_identities(self, st: dict, cid: str, dev: str) -> None:
        # `identities` = {gid: name} lists the people PRESENT right now; the parallel
        # `identity_sources` = {gid: "face"|"body"} says how surely each is recognised.
        # Unlike zones (which enumerate every zone with a bool each snapshot), this map
        # lists ONLY the present — so a person who left is detected by ABSENCE from it,
        # and we mirror that as "absent". The name is inline here, not in the roster.
        identities = st.get("identities")
        sources = st.get("identity_sources")
        sources = sources if isinstance(sources, dict) else {}
        # Third parallel map: when each present person's stay began (the open
        # presence episode's start). May lag a fresh appearance by the sustain
        # gate; "" mirrors that honestly rather than inventing a moment.
        sinces = st.get("identity_since")
        sinces = sinces if isinstance(sinces, dict) else {}
        present: set[str] = set()
        if isinstance(identities, dict):
            for gid, pname in identities.items():
                gid = str(gid)
                src = sources.get(gid)
                if src not in ("face", "body"):
                    # BABA lists a present person but no valid source — mirror presence
                    # at the weaker rung rather than drop the sighting or invent "face".
                    self._dropped(f"source:{cid}", "baba: identity %s on %s has source %r — treating as body",
                                  gid, self._camera_names.get(cid, cid), src)
                    src = "body"
                self._person_names[gid] = str(pname) if pname else gid
                eid = f"{dev}:person:{gid}"
                present.add(eid)
                self._register_person(cid, gid)
                self._pub(eid, "identity_presence", src, name=self._person_names[gid], device=dev)
                self._pub(eid, "identity_since", str(sinces.get(gid) or ""),
                          name=self._person_names[gid], device=dev)
        # Anyone previously seen on THIS camera but not in this snapshot → absent.
        for eid in self._known_persons:
            if eid.startswith(f"{dev}:person:") and eid not in present:
                gid = eid.rsplit(":", 1)[-1]
                self._pub(eid, "identity_presence", "absent",
                          name=self._person_names.get(gid), device=dev)
                self._pub(eid, "identity_since", "",
                          name=self._person_names.get(gid), device=dev)

    def _mirror_objects(self, st: dict, cid: str, dev: str) -> None:
        # `object_identities` = {gid: name} lists the PET/VEHICLE subjects present now;
        # `object_identity_kinds` = {gid: "pet"|"vehicle"} says which. Same sparse-map +
        # absence-by-omission contract as `identities`, but a SEPARATE map so a car is
        # never counted as a person. These have no face — BABA matches body vs enrolled
        # reference photos — so the value we mirror is the KIND, not a confidence rung.
        objects = st.get("object_identities")
        okinds = st.get("object_identity_kinds")
        okinds = okinds if isinstance(okinds, dict) else {}
        present_obj: set[str] = set()
        if isinstance(objects, dict):
            for gid, oname in objects.items():
                gid = str(gid)
                kind = okinds.get(gid)
                if kind not in ("pet", "vehicle"):
                    # The value IS the kind; without a valid one there's nothing sound
                    # to publish. BABA always sends the parallel kind, so this is a real
                    # inconsistency — skip loudly rather than invent one.
                    self._dropped(f"kind:{cid}", "baba: object %s on %s has kind %r — skipping",
                                  gid, self._camera_names.get(cid, cid), kind)
                    continue
                self._object_names[gid] = str(oname) if oname else gid
                eid = f"{dev}:object:{gid}"
                present_obj.add(eid)
                self._register_object(cid, gid)
                self._pub(eid, "object_presence", kind, name=self._object_names[gid], device=dev)
        for eid in self._known_objects:
            if eid.startswith(f"{dev}:object:") and eid not in present_obj:
                gid = eid.rsplit(":", 1)[-1]
                self._pub(eid, "object_presence", "absent",
                          name=self._object_names.get(gid), device=dev)

    async def _on_bell(self, site: _Site, msg) -> None:
        """A doorbell press — momentary, so never deduped."""
        try:
            ev = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(ev, dict):
            return
        cid = ev.get("camera_id")
        if not isinstance(cid, str) or not cid:
            return
        # BABA always carries the action; if it ever stops, say so instead of asserting
        # a press it never reported — a doorbell that invents rings is worse than a quiet one.
        action = ev.get("action")
        if not isinstance(action, str) or not action:
            log.warning("baba: bell event on %s carries no action — skipping", msg.subject)
            return
        dev = self._cam_dev(cid)
        self._announce_bell(cid)
        self._pub(f"{dev}:bell", "button", action,
                  name="Doorbell button", device=dev, dedup=False)

    async def _on_place(self, site: _Site, msg) -> None:
        """A car arrived at a place, or left it — onto that place's timeline.

        The scene entity already shows WHO stands there (`parked_vehicle`); this
        is the record that they came and went, which a live state cannot be. The
        panel is gated by the same view rule as everything else, so an arrival
        names nobody the reader could not already see on the entity it is filed
        against.

        BABA picks the witness camera — one that watches the place — so the row
        lands on the view whose footage covers the minute it carries.
        """
        try:
            ev = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return
        if not isinstance(ev, dict):
            return
        cid, place, kind = ev.get("camera_id"), ev.get("place"), ev.get("kind")
        if not all(isinstance(v, str) and v for v in (cid, place, kind)):
            return
        if kind not in PLACE_KINDS:
            log.warning("baba: place event of unknown kind %r from %s — skipping", kind, msg.subject)
            return
        rid = next((r for r, pl in self._scene_places.items() if pl == place
                    and self._scene_cams.get(r) == cid and r in self._scene_names), None)
        if rid is None:
            log.warning("baba: %s for place %s on %s, no region of that camera watches it — skipping",
                        kind, place, self._camera_names.get(cid, cid))
            return
        dev = self._cam_dev(cid)
        who = ev.get("name") or ""
        stood = ev.get("stood_s")
        if kind == "vehicle_left":
            message = (f"{who or 'A vehicle'} left {place}"
                       + (f" after {_spell_duration(stood)}" if stood else ""))
        else:
            message = f"{who or 'A vehicle'} arrived at {place}"
        await emit_journal(
            self._bus, kind, entity_id=f"{dev}:scene:{rid}", device_key=dev,
            source=f"adapter:{NAMESPACE}", severity="notice", message=message,
            data={k: ev.get(k) for k in
                  ("place", "name", "stood_s", "started_at", "ended_at", "evidence")},
        )

    # ------------------------------------------------------------ roster plane
    async def _on_roster(self, site: _Site, msg) -> None:
        try:
            r = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            self._dropped(f"roster:{site.key}", "baba: undecodable roster from %s", site.name)
            return
        if not isinstance(r, dict):
            self._dropped(f"roster:{site.key}", "baba: roster from %s is not an object", site.name)
            return
        try:
            self._handle_roster(site, r)
        except Exception:
            log.exception("baba: roster handler failed (%s)", site.name)

    def _handle_roster(self, site: _Site, r: dict) -> None:
        """Seed EVERY camera + zone from BABA's inventory, so the full set shows up
        front (not only ones that have pushed state yet), and prune whatever BABA no
        longer has. The roster is the authoritative inventory; state carries values."""
        live: set[str] = set()
        live_subs: set[str] = set()  # zone/scene/bell entity_ids a live camera still has
        slugs: dict[str, str] = {}
        for cam in r.get("cameras", []):
            if not isinstance(cam, dict):
                continue
            cid = cam.get("id")
            camera_slug = cam.get("slug")
            if not isinstance(cid, str) or not cid:
                continue  # no UUID → no stable identity; BABA always sends one
            live.add(cid)
            if isinstance(camera_slug, str) and camera_slug and cam.get("enabled") is not False:
                slugs[camera_slug] = cid
            name = cam.get("name")
            if isinstance(name, str) and name:
                self._camera_names[cid] = name
            self._register_camera(cid)
            dev = self._cam_dev(cid)
            if cam.get("doorbell"):
                self._announce_bell(cid)  # up front, so automations can target the ring
                live_subs.add(f"{dev}:bell")
            live_subs |= self._roster_zones(cam, cid, dev)
            live_subs |= self._roster_scenes(cam, cid, dev)
        if live:  # authoritative inventory → prune whatever BABA no longer has
            site.roster, site.live_subs, site.camera_ids = live, live_subs, slugs
            spawn(self._prune_stale(), log=log, name="baba prune stale")

    def _roster_zones(self, cam: dict, cid: str, dev: str) -> set[str]:
        subs: set[str] = set()
        for z in cam.get("zones") or []:
            if not isinstance(z, dict) or not z.get("id"):
                continue
            zid = str(z["id"])
            if z.get("name"):
                self._zone_names[zid] = str(z["name"])
            self._register_zone(cid, zid)
            subs.add(f"{dev}:zone:{zid}")
            if z.get("enabled") is False:
                self._disabled_zones.add(zid)
                self._pub(f"{dev}:zone:{zid}", "occupancy", False,
                          name=self._zone_names.get(zid), device=dev)
            else:
                self._disabled_zones.discard(zid)
        return subs

    def _roster_scenes(self, cam: dict, cid: str, dev: str) -> set[str]:
        """Scene regions are catalog too: BABA declares them (with their own
        operator-defined state vocabulary) before any is evaluated, so the
        automation editor can target a gate before it first reads open."""
        subs: set[str] = set()
        for sc in cam.get("scenes") or []:
            if not isinstance(sc, dict) or not sc.get("id"):
                continue
            rid = str(sc["id"])
            if sc.get("name"):
                self._scene_names[rid] = str(sc["name"])
            place = sc.get("place")
            if place:
                self._scene_places[rid] = str(place)
            else:
                self._scene_places.pop(rid, None)
            self._scene_cams[rid] = cid
            self._register_scene(cid, rid)
            subs.add(f"{dev}:scene:{rid}")
        return subs

    async def _prune_stale(self) -> None:
        """Prune anything BABA no longer has, at BOTH levels:

        * a whole camera whose UUID vanished from the roster (deleted, or renamed
          into a new camera) — drops it with all its sub-entities;
        * a zone/scene/bell a LIVE camera dropped — a DELETED one, now that zones and
          scenes are keyed by their own UUID: renaming one is pure display and no
          longer reads as delete+add.

        Registry + current_state + devices, like the Devices "Remove" action.
        History (ClickHouse, keyed by entity_id) keeps its own copy.

        With several installs the inventory is the UNION of their rosters, and the
        pass runs only once EVERY configured location has delivered one. Otherwise
        the house's roster would read the holiday home's cameras as deleted and drop
        them — along with the room and floor-plan placement DIDA holds for them and
        BABA cannot give back. A location that is down or slow to answer therefore
        blocks pruning entirely; a stale entity is cheap, a wiped one is not."""
        if self.broker is None or not self._sites:
            return
        # CONFIGURED, not connected: the check used to look at the locations that had
        # connected SO FAR, which is trivially satisfied by the first one. On every
        # restart the house connected a beat before the holiday home, its roster read
        # as the whole inventory, and the other location's cameras were pruned as
        # deleted — then re-announced a second later as brand-new entities, without
        # the room they had been given. That is why "assign an area" kept coming back.
        pending = {c.key for c in self._configured_sites()} - set(self._sites)
        if pending:
            log.debug("baba: prune skipped — %s has not connected yet", ", ".join(sorted(pending)))
            return
        if any(not s.roster for s in self._sites.values()):
            log.debug("baba: prune skipped — not every location has reported a roster")
            return
        live: set[str] = set().union(*(s.roster for s in self._sites.values()))
        live_subs: set[str] = set().union(*(s.live_subs for s in self._sites.values()))
        keys = {dk for r in await self.broker.call("entities", own=True)
                if (dk := r["device_key"]) and dk.startswith("baba:")}
        stale = sorted(dk for dk in keys if dk.removeprefix("baba:") not in live)
        for dk in stale:
            # A PLACED entity outlives the prune. Its floor-plan spot and its room
            # are the operator's work, and BABA cannot give either back — so a
            # reversible change on the BABA side (a region switched off for an
            # afternoon) must not spend them. Measured: west P3 was disabled
            # pending a decision, the prune took the entity, and when the region
            # came back a week later it had no place on the plan and read as a
            # device that had never existed. Deleting for real stays the
            # operator's own "Remove"; here we only let go of what nobody placed.
            kept = (await self.broker.call("forget", device_keys=[dk], keep_placed=True))["kept"]
            cid = dk.removeprefix("baba:")
            self._known_cameras.discard(cid)
            self._reach.pop(cid, None)
            forget_reachable(dk, NAMESPACE)
            self._camera_names.pop(cid, None)
            for s in self._sites.values():
                s.camera_ids = {sl: u for sl, u in s.camera_ids.items() if u != cid}
            self._known_zones = {z for z in self._known_zones if not z.startswith(f"{dk}:")}
            self._known_scenes = {s for s in self._known_scenes if not s.startswith(f"{dk}:")}
            self._known_bells = {b for b in self._known_bells if not b.startswith(dk)}
            self._known_lights = {lt for lt in self._known_lights if not lt.startswith(f"{dk}:")}
            self._light_state.pop(cid, None)
            self._has_light.pop(cid, None)
            self._lamp_recheck.pop(cid, None)
            self._light_fails.pop(cid, None)
            self._light_said.pop(cid, None)
            self._known_persons = {p for p in self._known_persons if not p.startswith(f"{dk}:")}
            self._known_objects = {o for o in self._known_objects if not o.startswith(f"{dk}:")}
            self._last = {k: v for k, v in self._last.items() if not k[0].startswith(dk)}
            if kept:
                log.info("baba: pruned stale camera %s — kept %d placed entit%s "
                         "(floor-plan/area placement survives; remove them in the UI)",
                         dk, kept, "y" if kept == 1 else "ies")
            else:
                log.info("baba: pruned stale camera %s (gone from roster)", dk)

        # Level 2: a zone/scene/bell a LIVE camera no longer has (typically renamed
        # in BABA — its name IS its key here, so the old one is left behind).
        subs = [r for r in await self.broker.call("entities", own=True)
                if (r["device_key"] or "").startswith("baba:") and r["entity_id"] != r["device_key"]]
        for r in subs:
            eid, dk = r["entity_id"], r["device_key"]
            if not dk or dk.removeprefix("baba:") not in live:
                continue  # its camera is gone entirely — already dropped above
            if any(f":{kind}:" in eid for kind in _INLINE_KINDS):
                continue  # inline sub-entities are NOT roster catalog — see _INLINE_KINDS
            if eid.endswith(_LIGHT_SUFFIX):
                # Nor is the floodlight: the ROSTER cannot say which camera has a
                # lamp — the device answers that, and DIDA learns it by asking. Left
                # to this pass it would be deleted on every roster refresh and
                # re-announced a minute later, losing the room and the floor-plan
                # place it was given, which is the same wound the inline kinds above
                # were added to close.
                continue
            if eid in live_subs:
                continue
            # Same rule as the camera level: drop the value it can no longer
            # stand behind, but keep a row somebody placed on the plan.
            placed = (await self.broker.call("forget", entity_ids=[eid], keep_placed=True))["kept"] > 0
            # Forgotten either way, so a region switched back on re-announces itself
            # onto the row (and the placement) that was waiting for it.
            self._known_zones.discard(eid)
            self._known_scenes.discard(eid)
            self._known_bells.discard(eid)
            self._last = {k: v for k, v in self._last.items() if k[0] != eid}
            if placed:
                log.info("baba: %s gone from its camera's roster — kept, it is placed "
                         "on the floor plan; remove it in the UI if it is really gone", eid)
            else:
                log.info("baba: pruned stale %s (gone from its camera's roster)", eid)

    # -------------------------------------------------------------- media plane
    async def _camera_refresh_loop(self) -> None:
        """Periodically re-announce every roster camera's live descriptor, so a camera
        added in BABA gets one without a restart, and re-check that the media plane
        still takes our credentials. After a failure the next try comes sooner than
        the full period, or the wall would stay dark for five minutes."""
        while True:
            # This loop starts before the supervise loop has connected anything, so
            # the first pass usually finds no locations. Sleeping the full period on
            # that would leave a fresh install (or any restart) with a camera wall of
            # empty tiles for five minutes and nothing in the log to explain it.
            delay = 300 if self._sites else 20
            for site in list(self._sites.values()):
                try:
                    await self._announce_cameras(site)
                    if site.media_error:
                        site.media_error = ""
                        self._ok_status()
                except asyncio.CancelledError:
                    raise  # let cancellation tear the loop down — never swallow it
                except Exception as exc:
                    # Name the location: with several installs "media plane failed"
                    # alone leaves you guessing whose camera wall went dark.
                    log.warning("baba: go2rtc failed for %s (retrying in 20s): %s",
                                site.name, exc, exc_info=True)
                    # …and say it on the CARD, not only in the log. The badge read
                    # "ok · Cabin: 2 cams" for ten minutes while go2rtc rejected every
                    # request: the state plane was fine, so the count looked healthy,
                    # while nothing on the wall could be watched.
                    site.media_error = str(exc)
                    self._ok_status()
                    delay = 20
            await asyncio.sleep(delay)

    async def _announce_cameras(self, site: _Site) -> None:
        import aiohttp

        go2rtc = site.cfg.go2rtc
        if not go2rtc:
            raise RuntimeError("go2rtc URL is not configured")  # loud, via the loop's log
        headers = site.cfg.media_headers()
        # The camera list comes from the roster, never from go2rtc's /api/streams: that
        # carries every camera's RTSP credentials, and BABA refuses it to a peer. A
        # media path with no `src` passes the same credential check and then answers
        # "stream not found" without opening a camera.
        async with self._session.get(  # type: ignore[union-attr]
            f"{go2rtc.rstrip('/')}/api/stream.mp4", timeout=aiohttp.ClientTimeout(total=8),
            headers=headers,
        ) as resp:
            if resp.status in (401, 403):
                # Loud: the proxy turns a rejected tile into a bare 404, so the wall
                # would sit there unwatchable with nothing saying why.
                what = "peer key" if "X-Peer-Key" in headers else "go2rtc user/password"
                raise RuntimeError(
                    f"go2rtc rejected our credentials ({resp.status}) — "
                    f"check the {what} for {site.name}"
                )
            if resp.status >= 500:
                raise RuntimeError(f"go2rtc answered {resp.status}")
        api_url = site.cfg.api_url
        for slug, cid in site.camera_ids.items():
            dev = self._cam_dev(cid)
            self._pub(dev, "camera", _descriptor(go2rtc, slug, api_url, cid, site.name),
                      name=self._camera_names.get(cid), device=dev)
        self._ok_status()

    # --------------------------------------------------------------- announcing
    def _register_camera(self, camera_id: str) -> None:
        """Announce the camera device so its motion/zone/scene facets group under
        one card. Re-announced when BABA teaches a new display name (entity name is
        last-write-wins, so it tracks BABA).

        An unknown name publishes as None — the engine then leaves whatever it has
        instead of us stamping the UUID over it as if it were a name."""
        if self._bus is None:
            return
        dev = self._cam_dev(camera_id)
        name = self._camera_names.get(camera_id)
        key = (dev, "__name")
        if camera_id in self._known_cameras and self._last.get(key) == name:
            return
        self._known_cameras.add(camera_id)
        self._last[key] = name
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=dev, adapter=NAMESPACE,
            capabilities=["camera", "motion", "person_count", "object_class", "light_condition"],
            name=name, device=dev, device_name=name,
        )), log=log, name=f"announce {dev}")

    def _register_zone(self, camera_id: str, zone_id: str) -> None:
        """A zone is a single-capability (occupancy) entity so the UI shows its NAME
        as the row label (a multi-cap entity would show generic cap labels). Keyed by
        BABA's zone UUID — the name is display only, so a rename re-announces (entity
        name is last-write-wins) instead of churning the entity.

        The roster is the ONLY name source: callers must have seen this zone there, so
        a missing key raises rather than inventing a display name from the UUID."""
        if self._bus is None:
            return
        dev = self._cam_dev(camera_id)
        zid = f"{dev}:zone:{zone_id}"
        name = self._zone_names[zone_id]
        key = (zid, "__name")
        if zid in self._known_zones and self._last.get(key) == name:
            return
        self._known_zones.add(zid)
        self._last[key] = name
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=zid, adapter=NAMESPACE, capabilities=["occupancy"],
            name=name, device=dev, device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {zid}")

    def _register_scene(self, camera_id: str, region_id: str) -> None:
        """A scene region is a single-capability (scene_state) entity, keyed by its
        BABA region UUID. Its state vocabulary is BABA's — operator-defined PER REGION
        (open/closed on a gate, present/empty on a parking spot), not a fixed enum — so
        DIDA stores the label verbatim and localises it for display via Settings →
        Translations. Like a zone, a rename only re-announces the name.

        Named after the REGION alone (`P1`), never `"<camera> · P1"`: the camera is the
        device these group under, exactly like a zone. A composed name only forced every
        reader to split it back apart — and the reader that failed at it printed a UUID.
        The roster is the ONLY name source; a missing key raises rather than inventing."""
        if self._bus is None:
            return
        dev = self._cam_dev(camera_id)
        sid = f"{dev}:scene:{region_id}"
        label = self._scene_names[region_id]
        key = (sid, "__name")
        if sid in self._known_scenes and self._last.get(key) == label:
            return
        self._known_scenes.add(sid)
        self._last[key] = label
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=sid, adapter=NAMESPACE, capabilities=["scene_state", "parked_vehicle"],
            name=label, device=dev, device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {sid}")

    async def _rebuild_known_persons(self) -> None:
        """Reload the person entities we've already announced from the DB, so the
        in-memory presence set survives a restart. Without this, a person present
        before the restart is never in `_known_persons`, so the absent pass never
        clears them and the badge latches on its last value forever. The entity
        table is the source: it already carries the stable id and display name."""
        if self.broker is None:
            return
        try:
            rows = [r for r in await self.broker.call("entities", prefix=f"{NAMESPACE}:")
                    if ":person:" in r["entity_id"]]
        except Exception:
            log.exception("baba: could not reload known persons from DB")
            return
        for r in rows:
            eid = r["entity_id"]
            self._known_persons.add(eid)
            gid = eid.rsplit(":", 1)[-1]
            if r["name"]:
                self._person_names[gid] = r["name"]
        if rows:
            log.info("baba: reloaded %d known person entit(ies) from DB", len(rows))

    def _register_person(self, camera_id: str, gid: str) -> None:
        """A recognised person at a camera: a single-cap (identity_presence) entity,
        keyed by BABA's STABLE re-ID gid so the same person is the same entity every
        time (never re-keyed → never the identity-by-wrong-key bug that cost a restore).
        Named from the INLINE identities value (not the roster, which carries no
        people); a rename only re-announces (last-write-wins), like a zone."""
        if self._bus is None:
            return
        dev = self._cam_dev(camera_id)
        pid = f"{dev}:person:{gid}"
        name = self._person_names.get(gid)
        key = (pid, "__name")
        if pid in self._known_persons and self._last.get(key) == name:
            return
        self._known_persons.add(pid)
        self._last[key] = name
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=pid, adapter=NAMESPACE, capabilities=["identity_presence", "identity_since"],
            name=name, device=dev, device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {pid}")

    async def _rebuild_known_objects(self) -> None:
        """Reload announced pet/vehicle entities from the DB so presence survives a
        restart — same reason (and same shape) as _rebuild_known_persons."""
        if self.broker is None:
            return
        try:
            rows = [r for r in await self.broker.call("entities", prefix=f"{NAMESPACE}:")
                    if ":object:" in r["entity_id"]]
        except Exception:
            log.exception("baba: could not reload known objects from DB")
            return
        for r in rows:
            eid = r["entity_id"]
            self._known_objects.add(eid)
            gid = eid.rsplit(":", 1)[-1]
            if r["name"]:
                self._object_names[gid] = r["name"]
        if rows:
            log.info("baba: reloaded %d known pet/vehicle entit(ies) from DB", len(rows))

    def _register_object(self, camera_id: str, gid: str) -> None:
        """A recognised pet/vehicle at a camera: a single-cap (object_presence) entity,
        keyed by BABA's STABLE re-ID gid. Mirrors _register_person; named from the inline
        object_identities value (the roster carries no pets/vehicles)."""
        if self._bus is None:
            return
        dev = self._cam_dev(camera_id)
        oid = f"{dev}:object:{gid}"
        name = self._object_names.get(gid)
        key = (oid, "__name")
        if oid in self._known_objects and self._last.get(key) == name:
            return
        self._known_objects.add(oid)
        self._last[key] = name
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=oid, adapter=NAMESPACE, capabilities=["object_presence"],
            name=name, device=dev, device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {oid}")

    def _announce_bell(self, camera_id: str) -> None:
        """A camera with a physical doorbell button gets a dedicated single-cap
        `button` entity, grouped under the camera card."""
        dev = self._cam_dev(camera_id)
        bell = f"{dev}:bell"
        if bell in self._known_bells or self._bus is None:
            return
        self._known_bells.add(bell)
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=bell, adapter=NAMESPACE, capabilities=["button"],
            name="Doorbell button", device=dev,
            device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {bell}")

    def _announce_light(self, camera_id: str) -> None:
        """A camera's floodlight: a two-capability (on_off + brightness) entity beside
        the camera, grouped under its card. Announced only once the device has
        answered, so a wall never carries a switch for a lamp that is not there."""
        dev = self._cam_dev(camera_id)
        lamp = f"{dev}{_LIGHT_SUFFIX}"
        if lamp in self._known_lights or self._bus is None:
            return
        self._known_lights.add(lamp)
        spawn(self._bus.publish_entity(EntityInfo(
            entity_id=lamp, adapter=NAMESPACE, capabilities=["on_off", "brightness"],
            name=_LIGHT_NAME, device=dev,
            device_name=self._camera_names.get(camera_id),
        )), log=log, name=f"announce {lamp}")

    def _site_of(self, camera_id: str) -> _Site | None:
        """The install this camera belongs to. Its peer key is the one that opens
        THAT box — presenting another location's is a 401 and a leaked secret."""
        for site in self._sites.values():
            if camera_id in site.roster:
                return site
        return None

    @staticmethod
    def _light_endpoint(site: _Site, camera_id: str) -> tuple[str, dict[str, str]] | None:
        """BABA's light URL for this camera and the header that authenticates us,
        or None when this location has no archive plane configured."""
        if not site.cfg.api_url:
            return None
        return f"{site.cfg.api_url}/cameras/{camera_id}/light", site.cfg.archive_headers()

    async def _read_light(self, site: _Site, camera_id: str) -> tuple[bool, int | None] | None:
        """The lamp as the DEVICE has it (BABA reads through to the camera on every
        request), or None when this camera model has none. Raises on anything else —
        an unreachable camera must not read as a lamp that is off."""
        target = self._light_endpoint(site, camera_id)
        if target is None:
            return None
        url, headers = target
        async with self._session.get(  # type: ignore[union-attr]
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == _NO_LIGHT:
                self._has_light[camera_id] = False
                self._lamp_recheck[camera_id] = time.monotonic() + _NO_LIGHT_RECHECK_S
                return None
            resp.raise_for_status()
            return _light_values(await resp.json(content_type=None))

    async def _light_refresh_loop(self) -> None:
        """Mirror every floodlight off the device, on a poll.

        The lamp is not in the vision snapshot and nothing pushes it: it changes
        under the vendor's app and under the camera's own schedule (kept in place as
        the fallback for the nights DIDA is down), and the only way to see that is to
        ask. A read that fails leaves the last value standing and says so — a mirror
        reports what it last saw rather than inventing an "off" that would make an
        automation turn a lamp on that is already lit."""
        while True:
            # Like the camera loop: this starts before the supervise loop has
            # connected anything, and sleeping the full period on that empty first
            # pass leaves every lamp un-mirrored for a minute after each restart —
            # a minute in which the rule that drives it deliberately does nothing.
            delay = _LIGHT_POLL_S if self._sites else 20
            for site in list(self._sites.values()):
                for camera_id in sorted(site.roster):
                    await self._poll_light(site, camera_id)
            await asyncio.sleep(delay)

    async def _poll_light(self, site: _Site, camera_id: str) -> None:
        """One camera's turn: read its lamp, or record why it could not be read."""
        if time.monotonic() < self._lamp_recheck.get(camera_id, 0.0):
            return  # rested: no lamp here, or nothing has answered for a while
        if self._light_endpoint(site, camera_id) is None:
            return
        try:
            state = await self._read_light(site, camera_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("baba: light state of %s unanswered", camera_id, exc_info=True)
            self._light_unanswered(camera_id, site, exc)
            return
        # It answered: whatever it said before is history, including how loudly.
        self._light_fails.pop(camera_id, None)
        self._light_said.pop(camera_id, None)
        if state is None:
            return
        self._has_light[camera_id] = True
        self._publish_light(camera_id, *state)

    def _light_unanswered(self, camera_id: str, site: _Site, exc: Exception) -> None:
        """A read that failed: said out loud the first time, then rested.

        Loud is a property of the FIRST line, not of the thousandth: a camera that
        cannot answer is one fact, and repeating it every minute buries the one that
        matters. A camera whose lamp DIDA has actually read keeps its minute — that
        is a regression, not a model without a lamp — but still only says so once per
        window."""
        fails = self._light_fails.get(camera_id, 0) + 1
        self._light_fails[camera_id] = fails
        now = time.monotonic()
        if now >= self._light_said.get(camera_id, 0.0):
            log.warning("baba: could not read %s's light at %s (%d in a row): %s",
                        self._camera_names.get(camera_id, camera_id), site.name, fails, exc)
            self._light_said[camera_id] = now + _LIGHT_COMPLAIN_S
        if fails >= _LIGHT_FAILS_BEFORE_REST and not self._has_light.get(camera_id):
            self._lamp_recheck[camera_id] = now + _NO_LIGHT_RECHECK_S

    def _publish_light(self, camera_id: str, on: bool, bright: int | None) -> None:
        """Announce the lamp (first sight) and mirror what the device answered."""
        self._announce_light(camera_id)
        dev = self._cam_dev(camera_id)
        lamp = f"{dev}{_LIGHT_SUFFIX}"
        self._pub(lamp, "on_off", on, name=_LIGHT_NAME, device=dev)
        if bright is not None:
            self._pub(lamp, "brightness", bright, name=_LIGHT_NAME, device=dev)
        known = self._light_state.get(camera_id)
        self._light_state[camera_id] = (on, bright if bright is not None else (known[1] if known else 0))

    async def _drive_light(self, command: Command, camera_id: str) -> None:
        """Turn a camera's floodlight on or off, or set its brightness, through the
        one writer that holds the camera's credentials.

        The answer BABA returns is read off the device after the write, so what DIDA
        publishes is what the camera actually did — not what we asked for."""
        site = self._site_of(camera_id)
        if site is None:
            log.warning("baba: light command for %s, which no location lists", camera_id)
            return
        target = self._light_endpoint(site, camera_id)
        if target is None:
            log.warning("baba: %s has no API address configured — cannot drive its light",
                        site.name)
            return
        url, headers = target
        known = self._light_state.get(camera_id)
        body: dict[str, object]
        if command.capability == "on_off":
            if command.command == "toggle":
                if known is None:
                    # Nothing read yet: a blind toggle is a coin flip on a lamp
                    # somebody can see from the road.
                    log.warning("baba: toggle for %s before its light was ever read", camera_id)
                    return
                body = {"on": not known[0]}
            else:
                body = {"on": command.command == "turn_on"}
        else:
            value = command.args.get("value")
            if not isinstance(value, (int, float)):
                log.warning("baba: set_brightness for %s without a value", camera_id)
                return
            # Brightness alone must not light a dark lamp, nor darken a lit one: the
            # lamp keeps the state it has, and the state it has is the device's.
            body = {"on": bool(known[0]) if known else False, "bright": max(0, min(100, int(value)))}
        async with self._session.post(  # type: ignore[union-attr]
            url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                # Loud: a light that did not come on is the whole point of the rule
                # that asked for it, and BABA answers with the camera's own words.
                detail = (await resp.text())[:200]
                log.warning("baba: %s refused %s on %s (%s): %s", site.name, command.command,
                            self._camera_names.get(camera_id, camera_id), resp.status, detail)
                return
            state = _light_values(await resp.json(content_type=None))
        self._has_light[camera_id] = True
        self._publish_light(camera_id, *state)

    def _pub(self, entity_id: str, capability: str, value, *,
             name: str | None = None, device: str | None = None, dedup: bool = True) -> None:
        if self._bus is None:
            return
        key = (entity_id, capability)
        # Dedup on (value, name), not value alone: the display name is part of
        # what the wall shows, and it changes without the state changing — a
        # car parking onto an already-`present` place relabels P1 to
        # "P1 · Marko's car", and a BABA-side rename used to sit invisible until
        # the next state transition happened to flush it through.
        if dedup and self._last.get(key) == (value, name):
            return
        bus, ts = self._bus, time.time_ns()

        async def _publish() -> None:
            # Cache as "published" ONLY after the publish succeeds, so a failed send
            # is retried by the next snapshot instead of being swallowed by dedup.
            await bus.publish_state(StateUpdate(
                entity_id=entity_id, capability=capability, value=value,
                adapter=NAMESPACE, ts_ns=ts, name=name, device=device,
            ))
            if dedup:
                self._last[key] = (value, name)

        spawn(_publish(), log=log, name=f"publish {entity_id}.{capability}")

    async def handle_command(self, command: Command) -> None:
        """Only a camera's floodlight (`baba:<uuid>:light`) is writable. Everything
        else BABA publishes is the vision truth, and the knobs behind it live in
        BABA, where the pixels are."""
        eid = command.entity_id
        if not eid.endswith(_LIGHT_SUFFIX):
            raise CommandRejected(f"{eid} is not writable from DIDA")
        if self._session is None:
            raise CommandRejected("adapter not started")
        camera_id = eid[len(NAMESPACE) + 1:-len(_LIGHT_SUFFIX)]
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        try:
            await self._drive_light(command, camera_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CommandRejected(f"light failed: {exc}") from exc

    async def stop(self) -> None:
        for site in self._sites.values():
            if site.nc is not None:
                with contextlib.suppress(Exception):
                    await site.nc.close()
        self._sites.clear()
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
