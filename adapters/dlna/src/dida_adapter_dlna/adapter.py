"""DLNA/UPnP adapter — discovers MediaRenderers and drives them as media players.

One DLNA renderer (a TV, an AV receiver, the iFi Zen Stream…) becomes one DIDA
entity carrying the media-player capability group (`media_transport`, `volume`,
`mute`, now-playing metadata). The adapter is the ONLY place that speaks UPnP;
it runs in its own container, so a flaky renderer can't touch the engine.

Design notes:
  * Discovery is SSDP multicast (needs host networking) — zero-config: every
    MediaRenderer on the LAN is adopted automatically, none hard-coded.
  * State is read by *polling* each renderer every few seconds (async_update),
    forwarding only changed values (dedupe) so the bus/history stay quiet.
    Position is published sparsely (on seek / track change / transport change);
    the UI extrapolates the playhead locally between updates.
  * GENA event push is a clean future add-on — polling remains the robust
    baseline (no callback-reachability dependency), which is why it's primary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from xml.sax.saxutils import escape

from dida_core import (
    CODEC,
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    StateUpdate,
    icy_stream_title,
    local_ip,
    radio_probe,
    radio_stations,
    ssdp_msearch,
)
from home_core.tasks import spawn

from dida_adapter_dlna.mapping import NAMESPACE, entity_id, normalise_transport, read_renderer

log = logging.getLogger("dida.adapter.dlna")

# Search targets. The standard renderer device type catches clean DMRs (the iFi);
# the AVTransport *service* type also catches renderers buried as an embedded
# device under a vendor root (e.g. Denon/HEOS), which wouldn't answer the
# device-type search. We then unwrap the embedded MediaRenderer in _add_location.
RENDERER_STS = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
)
_VOL_STEP = 0.05
# How we introduce ourselves to a renderer. A control point is not discoverable —
# it announces nothing — so the User-Agent on its SOAP requests is the ONLY place it
# can carry a name. Send none (the library defaults to bare `Python/x aiohttp/y`) and
# a Samsung TV has nothing to label its "allow this sender?" prompt with, so it just
# numbers the unknown senders it has seen: "device 3". Shaped per the DLNA convention
# (`OS/ver UPnP/1.0 product/ver`) so a renderer that parses it finds a product name.
_UA = "Linux/6 UPnP/1.0 DIDA/1.0"
# Consecutive discovery passes hearing NOTHING before we call the wire dead (~3 min
# at the default 60s interval) — SSDP is UDP, a single lost round means nothing.
_SILENT_PASSES = 3
# Units the engine echoes back for media caps (mirrors the capability specs).
_UNITS = {"volume": "%", "media_duration": "s", "media_position": "s"}


def _ssdp_search(iface_ip: str, timeout: float = 3.0) -> set[str]:
    """Blocking SSDP M-SEARCH for renderers → set of descriptor URLs.
    Run via asyncio.to_thread. Collects the LOCATION of every reply over `timeout`.

    A reply counts only if its ST echoes back one of the renderer targets we asked
    for. Some devices answer ANY M-SEARCH with `ST: upnp:rootdevice` regardless of
    what was requested — the IoT VLAN's power meter does exactly this — and such a
    reply says nothing about renderers: probing its descriptor is wasted work, and
    counting it as "something answered" is what let a wrong-interface bind look alive
    (see _discovery_badge). A renderer that matches echoes the ST it matched; both
    the device-type AND the service-type target are accepted, which is what catches
    renderers embedded under a vendor root.
    """
    locs: set[str] = set()
    wanted = tuple(st.lower().encode() for st in RENDERER_STS)
    for _sender_ip, data in ssdp_msearch(iface_ip, list(RENDERER_STS), timeout):
        st = re.search(rb"\bst:\s*(\S+)", data, re.IGNORECASE)
        if not st or st.group(1).strip().lower() not in wanted:
            continue
        m = re.search(rb"location:\s*(\S+)", data, re.IGNORECASE)
        if m:
            locs.add(m.group(1).decode("ascii", "ignore"))
    return locs


class Renderer:
    """One discovered DMR: its profile handle + last-published snapshot."""

    def __init__(self, udn: str, eid: str, name: str, dmr, location: str) -> None:
        self.udn = udn
        self.entity_id = eid
        self.name = name
        self.dmr = dmr
        self.location = location
        self.last: dict[str, object] = {}          # cap -> last published value (dedupe)
        self.pos_base: tuple[int, float] | None = None  # (position_s, monotonic) the UI extrapolates from
        self.queue: list[dict] = []                 # DIDA-managed playlist (gapless via SetNextAVTransportURI)
        self.qpos: int = -1                         # index of the currently-playing queue track
        self.prev_transport: str | None = None      # for end-of-track detection
        self.switching: bool = False                # True while we change the URI (suppress auto-advance race)
        self.cast_title: str | None = None          # title of what WE cast — detects renderer-initiated switches
        self.radio: dict | None = None              # {url, station} while playing internet radio
        self.icy_task: object | None = None         # bg task polling the stream's ICY now-playing
        self.radio_idle: int = 0                    # consecutive stopped/idle polls while in radio mode


class DlnaAdapter:
    """Bridges DLNA MediaRenderers onto the DIDA bus."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._requester = None
        self._factory = None
        self._renderers: dict[str, Renderer] = {}   # udn -> Renderer
        self._by_eid: dict[str, Renderer] = {}
        self._poll_fails: dict[str, int] = {}       # eid -> consecutive poll failures
        self._poll_tick = 0
        self._heard = False    # has ANY device ever answered our M-SEARCH (see _discovery_badge)
        self._iface_ip = ""    # set in start() from the merged config
        # Renderers owned by a dedicated adapter are skipped here (single source
        # of truth — one entity per player). Denon/Marantz are driven by the HEOS
        # adapter, where volume/transport actually work (their UPnP
        # RenderingControl is a no-op). Configurable (Settings → Adapters);
        # matched against manufacturer + friendly name, case-insensitive.
        self._exclude: tuple[str, ...] = ()

    async def start(self, bus: Bus) -> None:
        from async_upnp_client.aiohttp import AiohttpRequester
        from async_upnp_client.client_factory import UpnpFactory

        self._bus = bus
        self._cfg = AdapterConfig("dlna", self.broker)
        await self._cfg.load()
        self._iface_ip = self._cfg.get("iface_ip") or await local_ip(self.broker)
        self._exclude = tuple(
            tok.strip().lower()
            for tok in (self._cfg.get("exclude_manufacturers") or "denon,marantz,ifi").split(",")
            if tok.strip()
        )
        self._requester = AiohttpRequester(http_headers={"User-Agent": _UA})
        self._factory = UpnpFactory(self._requester, non_strict=True)
        spawn(self._cfg.poll_loop(), log=log, name="dlna config poll")
        spawn(self._discovery_loop(), log=log, name="dlna discovery loop")
        spawn(self._poll_loop(), log=log, name="dlna poll loop")
        self.status.idle("discovering")
        log.info("dlna adapter up — iface=%s, poll=%ss", self._iface_ip, self._cfg.int("poll_seconds", 3))

    # --- discovery ---------------------------------------------------------

    async def _discovery_loop(self) -> None:
        silent = 0  # consecutive passes nothing at all replied to
        while True:
            try:
                locations = await asyncio.to_thread(_ssdp_search, self._iface_ip)
                for loc in locations:
                    await self._add_location(loc)
                silent = 0 if locations else silent + 1
                self._discovery_badge(len(locations), silent)
            except Exception:
                log.exception("discovery pass failed")
            await asyncio.sleep(self._cfg.int("discovery_seconds", 60))

    def _discovery_badge(self, replies: int, silent: int) -> None:
        """The empty-state badge (_update_badge hands it to us): judged on what the
        SEARCH HEARD, never on how many renderers we ended up driving.

        Driving zero renderers is ordinary — the TVs are usually off and the
        iFi/Marantz are excluded on purpose — so it must never raise an alarm; a red
        badge every quiet evening is one nobody reads. The fault worth shouting about
        is having heard NOTHING, EVER: on the right LAN the excluded renderers answer
        regardless, so lifelong silence means the M-SEARCH is leaving by the wrong
        leg. Not hypothetical — this adapter sat bound to the IoT VLAN for three days
        after the host move, discovered nothing, and reported `healthy` throughout,
        because health only ever meant "the process is alive", never "it can see
        anything".

        The alarm is armed on "ever heard", not on a consecutive run, because a single
        pass is NOT trustworthy evidence: replies are UDP and devices rate-limit
        M-SEARCH, so a perfectly healthy LAN does return an empty round (measured —
        the LAN leg answered 3, then 2, then 0 within minutes). One reply, ever,
        disarms this for the life of the process. That deliberately scopes it to the
        failure it is for — a misconfigured interface, which is wrong from boot and
        stays wrong — rather than pretending to detect a wire pulled at 3am.

        `replies` counts only devices that answered AS renderers (_ssdp_search drops
        the rest). That distinction is load-bearing, not pedantry: the IoT VLAN has a
        power meter that answers every M-SEARCH with `upnp:rootdevice`, so counting
        bare replies would have kept this badge green through the exact three-day
        blindness it exists to catch.
        """
        if replies:
            self._heard = True  # one reply, ever, proves the leg is not the problem
        if self._renderers:
            return  # something to drive → _update_badge owns the badge
        if not self._heard and silent >= _SILENT_PASSES:
            self.status.error(f"nothing answers SSDP on {self._iface_ip} — wrong interface?")
        elif replies:
            self.status.idle(f"{replies} UPnP device(s), none to drive")
        else:
            self.status.idle("discovering")

    async def _add_location(self, location: str) -> None:
        if any(r.location == location for r in self._renderers.values()):
            return
        try:
            device = await self._factory.async_create_device(location)
        except Exception:
            log.warning("could not create device from %s", location, exc_info=True)
            return
        # The MediaRenderer may be the root device (iFi) or an embedded device
        # under a vendor root (Denon/HEOS) — scan both.
        candidates = list(getattr(device, "all_devices", None) or [device])
        renderer = next((d for d in candidates if "MediaRenderer" in (d.device_type or "")), None)
        if renderer is None:
            return
        # Skip renderers owned by a dedicated adapter (e.g. Denon/Marantz → HEOS).
        ident = " ".join(
            str(x or "").lower()
            for x in (
                getattr(device, "manufacturer", ""),
                getattr(device, "friendly_name", ""),
                getattr(renderer, "friendly_name", ""),
            )
        )
        match = next((tok for tok in self._exclude if tok in ident), None)
        if match is not None:
            log.info("skipping renderer at %s — '%s' owned by a dedicated adapter", location, match)
            return
        from async_upnp_client.profiles.dlna import DmrDevice

        udn = renderer.udn
        if udn in self._renderers:
            # Known renderer resurfaced at a new SSDP LOCATION (a DHCP lease change).
            # Rebuild the DMR handle from the NEW descriptor: the old one has control/
            # eventing URLs pinned to the dead IP, so only updating the location string
            # would leave it polling the old address forever (it never self-heals).
            r = self._renderers[udn]
            r.dmr = DmrDevice(renderer, None)
            r.location = location
            self._poll_fails[r.entity_id] = 0
            return

        name = renderer.friendly_name or device.friendly_name or "Renderer"
        eid = entity_id(name)
        # Disambiguate the rare case of two renderers with the same name.
        if eid in self._by_eid and self._by_eid[eid].udn != udn:
            eid = f"{eid}_{udn[-4:]}"
        r = Renderer(udn, eid, name, DmrDevice(renderer, None), location)
        self._renderers[udn] = r
        self._by_eid[eid] = r
        log.info("discovered renderer %r → %s at %s", name, eid, location)
        self.status.ok(f"{len(self._renderers)} renderer(s)")
        await self._poll_renderer(r)

    # --- state polling -----------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            self._poll_tick += 1
            for r in list(self._renderers.values()):
                fails = self._poll_fails.get(r.entity_id, 0)
                # A dead renderer costs a full HTTP timeout per pass and delays
                # the healthy ones — back it off to every 10th tick once it's
                # been down a while (still recovers within ~30 s of coming back).
                if fails >= 3 and self._poll_tick % 10 != 0:
                    continue
                try:
                    await self._poll_renderer(r)
                except Exception as exc:
                    fails += 1
                    self._poll_fails[r.entity_id] = fails
                    if fails == 3:
                        # Fail loud ONCE at threshold; mark the player idle so the
                        # UI doesn't show it frozen mid-song forever.
                        log.warning("dlna %s unreachable (3 consecutive polls): %s", r.entity_id, exc)
                        self._stop_icy(r)
                        await self._set(r, "media_transport", "idle")
                    else:
                        log.debug("poll failed for %s", r.entity_id, exc_info=True)
                else:
                    if fails >= 3:
                        log.info("dlna %s reachable again", r.entity_id)
                    self._poll_fails[r.entity_id] = 0
                self._update_badge()
            await asyncio.sleep(self._cfg.int("poll_seconds", 3))

    def _update_badge(self) -> None:
        total = len(self._renderers)
        if not total:
            return  # discovery owns the empty-state badge
        dead = sum(1 for eid, n in self._poll_fails.items() if n >= 3 and eid in self._by_eid)
        live = total - dead
        if live == total:
            self.status.ok(f"{total} renderer(s)")
        elif live:
            self.status.ok(f"{live}/{total} on")
        else:
            # Every renderer unreachable. For DLNA that's almost always just the TVs
            # being OFF (their normal state), not a fault — so a neutral idle badge,
            # never a red error. (A genuine adapter fault — an M-SEARCH nothing at all
            # answers — is raised by _discovery_badge, not by this reachability count.)
            self.status.idle(f"0/{total} on")

    async def _poll_soon(self, r: Renderer) -> None:
        """Re-poll shortly after a command so the UI reflects it without waiting
        a full interval."""
        await asyncio.sleep(0.6)
        try:
            await self._poll_renderer(r)
        except Exception:
            log.debug("post-command poll failed for %s", r.entity_id, exc_info=True)

    async def _poll_renderer(self, r: Renderer) -> None:
        await r.dmr.async_update()
        snap = read_renderer(r.dmr)
        transport = snap.get("media_transport")

        # Scalar caps: forward only changes. While WE drive a queue, our cast
        # metadata (artist/album/art via _apply_meta) is authoritative — the iFi
        # refreshes title/artist for a new track but keeps the PREVIOUS track's
        # album-art URI, so letting snap through here would clobber the right
        # cover with a stale one. Title still comes from snap (needed for the
        # gapless-advance detection below and reliably reported).
        if r.queue:
            owned: tuple[str, ...] = ("media_artist", "media_album", "media_art")
        elif r.radio:
            owned = ("media_title", "media_artist", "media_album", "media_art")  # ICY task owns these
        else:
            owned = ()
        for cap, value in snap.items():
            if cap in owned:
                continue
            if r.last.get(cap) != value:
                r.last[cap] = value
                await self._publish(r, cap, value)

        await self._follow_queue(r, snap.get("media_title"), transport)
        r.prev_transport = transport

        # Radio mode must EXIT once the stream has genuinely stopped (user stopped
        # it on the device / switched source): while r.radio is set the ICY task
        # owns title/artist, so a stale radio flag masks the renderer's real
        # now-playing indefinitely. ~15 s of stopped/idle ends it.
        if r.radio:
            if transport in ("stopped", "idle"):
                r.radio_idle += 1
                if r.radio_idle >= 5:
                    log.info("dlna %s: radio stream stopped — leaving radio mode", r.entity_id)
                    self._stop_icy(r)
            else:
                r.radio_idle = 0

        await self._follow_external(r, snap)
        await self._publish_position(r, transport)

    async def _follow_queue(self, r: Renderer, cur_title, transport) -> None:
        """Gapless queue advance: when the renderer gaplessly rolls onto the track
        we primed as "next", its now-playing title becomes that track's title
        (from the metadata we set). Title-based detection is reliable here —
        current_track_uri is flaky on minimalist renderers (the iFi). Bump the
        pointer, push the new cover, and prime the following track."""
        if not (r.queue and not r.switching and 0 <= r.qpos < len(r.queue) - 1):
            return
        nxt_title = str(r.queue[r.qpos + 1].get("title") or "")
        if cur_title and nxt_title and cur_title == nxt_title:
            r.qpos += 1
            await self._cast_track(r, r.queue[r.qpos])
            await self._prime_next(r)
            await self._publish_queue(r)
        # Fallback: the track ended and the renderer stopped instead of
        # gaplessly rolling to the primed next → advance manually.
        elif transport in ("stopped", "idle") and r.prev_transport in ("playing", "buffering"):
            await self._play_index(r, r.qpos + 1)

    async def _follow_external(self, r: Renderer, snap: dict) -> None:
        """Renderer switched to content WE didn't cast (its own app, a station
        change) → our optimistic metadata is stale; drop the fields the renderer
        doesn't itself report, so a radio stream doesn't keep a previous library
        track's artist/album/cover/quality."""
        snap_title = snap.get("media_title")
        if r.queue or r.radio or not snap_title or snap_title == r.cast_title:
            return
        r.cast_title = snap_title
        for cap in ("media_artist", "media_album", "media_art", "media_quality"):
            # Clear unconditionally (publish "" once) — after a restart our
            # in-memory dedup is empty but the DB may hold a previous run's
            # stale values, so we can't rely on r.last to know to overwrite.
            if cap not in snap and r.last.get(cap) != "":
                r.last[cap] = ""
                await self._publish(r, cap, "")
        # The renderer is playing its own app (Spotify/Tidal Connect, radio) —
        # content DIDA didn't route, so the signal path is the device's own.
        await self._set(r, "media_source", "external")
        await self._set(r, "media_queue", "")
        # Probe the stream the renderer chose itself, for its real quality.
        uri = getattr(r.dmr, "current_track_uri", None) or getattr(r.dmr, "av_transport_uri", None)
        if uri and str(uri).startswith("http"):
            spawn(self._probe_quality(r, str(uri), snap_title), log=log, name="probe quality")

    async def _publish_position(self, r: Renderer, transport) -> None:
        """Position: publish sparsely. The UI extrapolates the playhead from the
        last published (position, time) baseline while transport == playing,
        so we only re-publish on a real jump (seek / track change) or when a
        non-playing position actually moved."""
        pos = getattr(r.dmr, "media_position", None)
        if pos is None:
            return
        pos = int(pos)
        mono = time.monotonic()
        expected: float | None = None
        if r.pos_base is not None and transport == "playing":
            base_pos, base_mono = r.pos_base
            expected = base_pos + (mono - base_mono)
        publish = (
            (expected is None and (r.last.get("media_position") != pos or transport == "playing"))
            or (expected is not None and abs(pos - expected) > 3)
        )
        if publish:
            r.last["media_position"] = pos
            r.pos_base = (pos, mono)
            await self._publish(r, "media_position", pos)

    async def _publish(self, r: Renderer, capability: str, value: object) -> None:
        assert self._bus is not None
        await self._bus.publish_state(
            StateUpdate(
                entity_id=r.entity_id,
                capability=capability,
                value=value,  # type: ignore[arg-type]
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit=_UNITS.get(capability),
                name=r.name,
            )
        )

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        r = self._by_eid.get(command.entity_id)
        if r is None:
            raise CommandRejected("unknown or undiscovered renderer")
        try:
            await self._dispatch(r, command.capability, command.command, dict(command.args))
        except Exception as exc:
            raise CommandRejected(f"device refused: {exc}") from exc
        spawn(self._poll_soon(r), log=log, name="post-command poll")

    async def _dispatch(self, r: Renderer, cap: str, cmd: str, args: dict) -> None:
        dmr = r.dmr
        if cap == "media_transport":
            await self._transport(r, cmd, args)
        elif cap == "volume":
            cur = dmr.volume_level or 0.0
            level = {"set_volume": lambda: _vol01(args.get("value")),
                     "volume_up": lambda: min(1.0, cur + _VOL_STEP),
                     "volume_down": lambda: max(0.0, cur - _VOL_STEP)}.get(cmd)
            if level is not None:
                await dmr.async_set_volume_level(level())
        elif cap == "mute":
            muted = {"mute": True, "unmute": False, "toggle": not bool(dmr.is_volume_muted)}
            if cmd in muted:
                await dmr.async_mute_volume(muted[cmd])

    async def _transport(self, r: Renderer, cmd: str, args: dict) -> None:
        dmr = r.dmr
        if cmd == "play":
            await dmr.async_play()
        elif cmd == "pause":
            await dmr.async_pause()
        elif cmd == "stop":
            r.queue = []  # user stop ends the queue (no auto-advance after)
            r.qpos = -1
            # End radio mode too: without this the ICY task kept opening the
            # upstream stream every 15 s forever and its stale station metadata
            # masked whatever the renderer played next.
            self._stop_icy(r)
            await dmr.async_stop()
        elif cmd in ("next", "previous"):
            step = 1 if cmd == "next" else -1
            if r.queue and 0 <= r.qpos + step < len(r.queue):
                await self._play_index(r, r.qpos + step)
            elif cmd == "next":
                await dmr.async_next()
            else:
                await dmr.async_previous()
        elif cmd == "play_pause":
            if normalise_transport(getattr(dmr, "transport_state", None)) == "playing":
                await dmr.async_pause()
            else:
                await dmr.async_play()
        elif cmd == "play_media":
            await self._play_media(r, args)
        elif cmd == "play_queue":
            await self._play_queue(r, args)
        elif cmd == "play_index" and r.queue:  # jump to a track in the current queue
            await self._play_index(r, max(0, min(int(args.get("index") or 0), len(r.queue) - 1)))

    async def _play_media(self, r: Renderer, args: dict) -> None:
        """Play a single item (radio URL or one library track). Clears any queue."""
        url = args.get("uri") or args.get("url")
        if not url:
            log.warning("play_media without a uri")
            return
        r.queue = []
        r.qpos = -1
        # Internet radio: the renderer only knows the station name; we poll the
        # stream's ICY metadata for the live song. Mark r.radio BEFORE the URI
        # swap so the poll loop yields the metadata caps to us during the cast
        # (the stop/play gap) instead of firing the "external content" branch.
        radio = None
        if _source_of(str(url)) == "radio":
            upstream = str(url)
            radio = {"url": upstream, "station": str(args.get("title") or "")}
            r.radio = radio  # BEFORE the URI swap AND the (slow) probe below, so a poll
            # firing mid-cast yields the metadata caps to us instead of taking the
            # 'external content' branch (which would clobber source/album/art).
            # The renderer fetches the station itself (no relay); ICY + quality read
            # the stream, and the station logo (our cover for radio) comes from the
            # station list. r.radio IS this dict, so the ICY loop sees the logo.
            args, logo = await self._radio_cast_args(args, upstream)
            radio["logo"] = logo
        else:
            self._stop_icy(r)
        await self._set_uri(r, dict(args), play=True, clear_next=True)
        await self._publish_queue(r)
        if radio:
            self._start_icy(r, radio)

    async def _radio_cast_args(self, args: dict, upstream: str) -> tuple[dict, str | None]:
        """Radio cast args + the station's logo (radio carries no per-track art → the
        logo IS the cover), reporting the upstream's true quality. Returns
        (cast_args, logo).

        The station's OWN url is cast, unchanged — the renderer fetches the stream
        itself. This path used to rewrite the uri through an ffmpeg relay of ours,
        but that existed for exactly one renderer: the iFi, whose MPD 0.20 picks its
        decoder from the url's file extension and so choked on the one station that
        serves AAC at a `.mp3` url. The iFi has its own adapter now (volumio, which
        corrects that station's suffix directly — see its mpd_uri), and the renderers
        left here are TVs, which sniff the real codec like the AVR always did. So the
        relay is gone: nothing was left that needed it, and the FLAC it served suits a
        TV worse than the AAC/MP3 the station sends.
        """
        quality, _ext = await radio_probe(upstream)
        out = dict(args, quality=quality)
        logo = None
        if self._bus is not None:
            for st in await radio_stations(self._bus) or []:
                if st.get("url") == upstream:
                    logo = st.get("logo") or None
                    break
        return out, logo

    def _start_icy(self, r: Renderer, radio: dict) -> None:
        if r.icy_task is not None:  # cancel a prior loop WITHOUT clearing r.radio
            r.icy_task.cancel()
        # spawn (not bare create_task): a crash in the ICY loop is logged, not swallowed
        # as an un-retrieved task exception. Keep the ref — _stop_icy cancels it.
        r.icy_task = spawn(self._icy_loop(r, radio), log=log, name="dlna icy stream")

    def _stop_icy(self, r: Renderer) -> None:
        task = r.icy_task
        r.icy_task = None
        r.radio = None
        if task is not None:
            task.cancel()

    async def _icy_loop(self, r: Renderer, radio: dict) -> None:
        """Poll the radio stream's ICY StreamTitle → publish artist/title; keep the
        station name as the 'album' line. The poll-loop's cap forwarding skips
        these caps for radio so we own them (see _poll_renderer)."""
        url, station = str(radio.get("url") or ""), str(radio.get("station") or "")
        await self._set(r, "media_album", station)
        await self._set(r, "media_art", str(radio.get("logo") or ""))  # station logo → cover ("" if none)
        await self._set(r, "media_title", station)   # until the first StreamTitle
        last = None
        warned = False
        while True:
            try:
                meta = await asyncio.to_thread(icy_stream_title, url)
                warned = False
                if meta and meta != last:
                    last = meta
                    log.info("icy %s: %s", r.entity_id, meta)
                    artist, sep, song = meta.partition(" - ")
                    if sep and song.strip():
                        await self._set(r, "media_artist", artist.strip())
                        await self._set(r, "media_title", song.strip())
                    else:
                        await self._set(r, "media_artist", "")
                        await self._set(r, "media_title", meta.strip())
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # metadata for the rest of the station's playtime (it used to).
                if not warned:
                    log.warning("icy %s: %s (retrying)", r.entity_id, exc, exc_info=True)
                    warned = True
                await asyncio.sleep(30)

    async def _play_queue(self, r: Renderer, args: dict) -> None:
        """Play a list of tracks gaplessly (album). `queue` is a JSON array of
        {uri,title,artist,album,art,mime}; `start` is the first index."""
        import json

        try:
            tracks = json.loads(str(args.get("queue") or "[]"))
        except (ValueError, TypeError):
            log.warning("dlna %s: play_queue got undecodable queue JSON", r.entity_id)
            tracks = []
        if not isinstance(tracks, list) or not tracks:
            if args.get("queue"):
                log.warning("dlna %s: play_queue had no playable tracks", r.entity_id)
            return
        self._stop_icy(r)  # a queue cast ends any radio
        r.queue = [t for t in tracks if isinstance(t, dict) and (t.get("uri") or t.get("url"))]
        start = int(args.get("start") or 0)
        await self._play_index(r, max(0, min(start, len(r.queue) - 1)))

    async def _play_index(self, r: Renderer, i: int) -> None:
        """Play queue track i and prime the NEXT one for gapless advance."""
        if not (0 <= i < len(r.queue)):
            return
        r.qpos = i
        # clear_next: a manual jump must drop any primed "next" first, or a
        # gapless renderer (the iFi) ignores the new SetAVTransportURI.
        await self._set_uri(r, r.queue[i], play=True, clear_next=True)
        await self._prime_next(r)
        await self._publish_queue(r)

    async def _publish_queue(self, r: Renderer) -> None:
        """Publish the play queue as a capability so any now-playing UI can show
        it the same way for every source — the adapter owns the real queue."""
        import json

        if r.queue:
            items = [{"title": str(t.get("title") or ""), "artist": str(t.get("artist") or ""),
                      "art": str(t.get("art") or "")} for t in r.queue]
            value = json.dumps({"items": items, "current": r.qpos}, separators=(",", ":"))
        else:
            value = ""
        await self._set(r, "media_queue", value)

    async def _prime_next(self, r: Renderer) -> None:
        nxt = r.qpos + 1
        try:
            if 0 <= nxt < len(r.queue):
                t = r.queue[nxt]
                url = str(t.get("uri") or t.get("url"))
                await r.dmr.async_set_next_transport_uri(url, str(t.get("title") or ""), _meta_for(t))
            else:
                await r.dmr.async_set_next_transport_uri("", "", "")
        except Exception:
            log.debug("set_next failed for %s", r.entity_id, exc_info=True)

    async def _set_uri(self, r: Renderer, t: dict, *, play: bool, clear_next: bool) -> None:
        r.switching = True
        try:
            await self._set_uri_inner(r, t, play=play, clear_next=clear_next)
        finally:
            r.switching = False

    async def _set_uri_inner(self, r: Renderer, t: dict, *, play: bool, clear_next: bool) -> None:
        url = str(t.get("uri") or t.get("url"))
        title = str(t.get("title") or "")
        meta = _meta_for(t)
        if not meta:
            try:
                meta = await r.dmr.construct_play_media_metadata(url, title)
            except Exception:
                log.debug("dlna: play metadata not built for %s", url, exc_info=True)
                meta = ""
        # Stop AND wait for the renderer to actually leave PLAYING — the iFi
        # silently ignores a new SetAVTransportURI while still playing, and
        # async_stop() returns before the transport has really stopped.
        try:
            await r.dmr.async_stop()
            for _ in range(10):  # up to ~2s
                await r.dmr.async_update()
                if normalise_transport(getattr(r.dmr, "transport_state", None)) != "playing":
                    break
                await asyncio.sleep(0.2)
        except Exception:
            log.debug("dlna: stop before a new URI failed", exc_info=True)
            pass
        await r.dmr.async_set_transport_uri(url, title, meta)
        if clear_next:
            with contextlib.suppress(Exception):
                await r.dmr.async_set_next_transport_uri("", "", "")
        if play:
            await r.dmr.async_play()
        # Optimistic metadata: minimalist renderers (the iFi) don't echo
        # artist/album/cover/format back. Publish what we cast, CLEAR what we
        # don't (so a radio stream drops a previous track's leftovers).
        await self._cast_track(r, t)

    async def _cast_track(self, r: Renderer, t: dict) -> None:
        """Publish a track's optimistic metadata; when it carries no quality but
        is an http stream, probe its headers for the real quality. Covers a plain
        radio URL (ICY → 'MP3 192kbps') and a Tidal proxy URL (X-Stream-Quality →
        'FLAC 44.1kHz/16bit'). Used on both a manual set and a gapless advance."""
        await self._apply_meta(r, t)
        url = str(t.get("uri") or t.get("url") or "")
        await self._set(r, "media_source", _source_of(url))  # origin → signal-path view
        if not t.get("quality") and url.startswith("http"):
            spawn(self._probe_quality(r, url, str(t.get("title") or "")), log=log, name="probe quality")

    async def _set(self, r: Renderer, capability: str, value: object) -> None:
        """Publish a capability, de-duplicated against the last value we sent."""
        if r.last.get(capability) != value:
            r.last[capability] = value
            await self._publish(r, capability, value)

    async def _apply_meta(self, r: Renderer, t: dict) -> None:
        """Publish (or clear) the optimistic now-playing metadata for a track."""
        r.cast_title = str(t.get("title") or "") or r.cast_title
        for cap, key in (("media_artist", "artist"), ("media_album", "album"),
                         ("media_art", "art"), ("media_quality", "quality")):
            v = str(t.get(key) or "")
            if r.last.get(cap) != v:
                r.last[cap] = v
                await self._publish(r, cap, v)

    async def _probe_quality(self, r: Renderer, url: str, title: str) -> None:
        q = await _stream_quality(url)
        # Only apply if we're still on the same cast (no newer one raced in).
        if q and r.cast_title == title and r.last.get("media_quality") != q:
            r.last["media_quality"] = q
            await self._publish(r, "media_quality", q)

    async def stop(self) -> None:
        for r in self._renderers.values():
            self._stop_icy(r)
        if self._requester is not None:
            close = getattr(self._requester, "async_close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


def _meta_for(t: dict) -> str:
    """DIDL-Lite for a queue/track dict, or '' when it carries no metadata."""
    artist = str(t.get("artist") or "")
    album = str(t.get("album") or "")
    art = str(t.get("art") or "")
    if not (artist or album or art):
        return ""
    url = str(t.get("uri") or t.get("url") or "")
    return _build_didl(
        url, str(t.get("title") or ""), artist, album, art, str(t.get("mime") or ""),
        sample_rate=_as_int(t.get("sample_rate")), bits=_as_int(t.get("bits")),
        channels=_as_int(t.get("channels")),
    )


def _as_int(v: object) -> int | None:
    try:
        return int(v) if v not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _source_of(url: str) -> str:
    """Classify a cast URL into its content source (drives the signal-path view).
    A record the house put on is recognised by OPUS's stream path; anything else
    http is a stream."""
    if "/api/play/track/" in url:
        return "library"
    if url.startswith("http"):
        return "radio"
    return ""


async def _stream_quality(url: str) -> str:
    """Probe a stream's headers → e.g. 'MP3 192kbps' or 'FLAC 44.1kHz/16bit'
    ('' if unknown). A DIDA proxy (Tidal) states the exact quality of the file it
    produced via `X-Stream-Quality`; trust that. Else infer codec+bitrate from
    ICY/Content-Type (radio). Timeout is generous: a cold Tidal track holds the
    response until the proxy has remuxed the whole FLAC (~5-8s)."""
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with (
            aiohttp.ClientSession(timeout=timeout) as s,
            s.get(url, headers={"Icy-MetaData": "1"}) as resp,
        ):
            authoritative = resp.headers.get("X-Stream-Quality", "").strip()
            if authoritative:
                return authoritative
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            br = (resp.headers.get("icy-br") or "").split(",")[0].strip()
            codec = CODEC.get(ct, ct.rsplit("/", 1)[-1].upper() if ct.startswith("audio/") else "")
            parts = [p for p in (codec, f"{br}kbps" if br.isdigit() else "") if p]
            return " ".join(parts)
    except Exception:
        log.debug("dlna: stream quality of %s unreadable", url, exc_info=True)
        return ""


def _vol01(value: object) -> float:
    """A 0..100 volume arg → a clamped 0..1 float for UPnP RenderingControl."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v / 100.0))


def _build_didl(
    url: str, title: str, artist: str, album: str, art: str, mime: str,
    *, sample_rate: int | None = None, bits: int | None = None, channels: int | None = None,
) -> str:
    """Minimal DIDL-Lite for a music track so the renderer shows full metadata.
    The renderer echoes this back in its TrackMetaData, so our poll-back picks up
    artist/album/cover too — a rich now-playing for a local library file."""
    proto = f"http-get:*:{mime or 'audio/mpeg'}:*"
    parts = [
        f"<dc:title>{escape(title)}</dc:title>",
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>",
    ]
    if artist:
        parts.append(f"<dc:creator>{escape(artist)}</dc:creator>")
        parts.append(f"<upnp:artist>{escape(artist)}</upnp:artist>")
    if album:
        parts.append(f"<upnp:album>{escape(album)}</upnp:album>")
    if art:
        parts.append(f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>")
    res_attrs = f'protocolInfo="{escape(proto)}"'
    if sample_rate:
        res_attrs += f' sampleFrequency="{sample_rate}"'
    if bits:
        res_attrs += f' bitsPerSample="{bits}"'
    if channels:
        res_attrs += f' nrAudioChannels="{channels}"'
    parts.append(f"<res {res_attrs}>{escape(url)}</res>")
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">' + "".join(parts) + "</item></DIDL-Lite>"
    )
