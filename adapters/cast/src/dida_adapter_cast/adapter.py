"""Google Cast adapter — Chromecast / Android TV / Nest via pychromecast.

Each Cast device becomes one DIDA media-player entity (now-playing + transport +
volume). pychromecast is THREADED (mDNS discovery + a socket thread per device),
so status callbacks fire off the asyncio loop — we marshal them back onto it with
call_soon_threadsafe (same trick as the HEOS adapter), and run the blocking cc.*
command calls in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from datetime import UTC, datetime

from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    host_setting,
    local_ip,
    set_reachable,
)
from home_core.tasks import spawn

from dida_adapter_cast.mapping import NAMESPACE, entity_id, read_media

# Every cast device carries the same canonical media surface; screens add the
# media_display marker the wall-panel picker filters on.
_BASE_CAPS = [
    "media_transport", "media_title", "media_artist", "media_album", "media_art",
    "media_duration", "media_position", "volume", "mute",
]

log = logging.getLogger("dida.adapter.cast")

_UNITS = {"volume": "%", "media_duration": "s", "media_position": "s"}

# Wall-panel watchdog. DashCast's public app id: while it holds the receiver the
# device is never `is_idle`, whatever the page inside is actually showing — so a
# dead page (error page, crashed tab) is indistinguishable from a healthy panel
# from the Cast protocol alone. The panel's own heartbeat (`panel_seen_at`,
# written by POST /panel/alive) is the only honest liveness signal; going this
# long without one means the screen is no longer ours and we take it back.
_DASHCAST_APP_ID = "84912283"
_PANEL_STALE_S = 300


class CastDevice:
    def __init__(self, uuid, cc, eid: str, name: str, *, is_display: bool = False) -> None:
        self.uuid = uuid
        self.cc = cc
        self.entity_id = eid
        self.name = name
        # The single-entity device's grouping key — the slug the entity id carries.
        self.dev_key = eid.split(":", 1)[1]
        # cast_type "cast" = a screen (Nest Hub / Android TV / Chromecast); "audio"/
        # "group" = a speaker. Only screens can host the wall panel, so we surface
        # this as the media_display capability for the UI to filter on.
        self.is_display = is_display
        self.last: dict[str, object] = {}
        self.pos_base: tuple[int, float] | None = None
        self.dashcast = None  # lazily-registered DashCastController (wall panel)


class _MediaListener:
    def __init__(self, adapter: CastAdapter, dev: CastDevice) -> None:
        self._a = adapter
        self._dev = dev

    def new_media_status(self, status) -> None:  # pychromecast thread
        self._a._on_media(self._dev, status)

    def load_media_failed(self, item, error_code) -> None:
        # An announce/TTS that fails to play is user-visible non-behavior — warn.
        log.warning("cast load_media_failed %s: %s", item, error_code)


class _CastListener:
    def __init__(self, adapter: CastAdapter, dev: CastDevice) -> None:
        self._a = adapter
        self._dev = dev

    def new_cast_status(self, status) -> None:  # pychromecast thread
        self._a._on_cast(self._dev, status)


class CastAdapter:
    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._devices: dict[object, CastDevice] = {}
        self._by_eid: dict[str, CastDevice] = {}
        self._connected: set[object] = set()  # uuids whose socket actually connected
        self._reach: dict[object, bool] = {}  # uuid -> last reachability we published
        self._browser = None
        self._zconf = None
        self.broker = None  # set by the runner
        self._cfg: AdapterConfig | None = None
        self._panel_unreachable = False  # edge-logged so a wedged panel isn't a traceback every cycle

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_discovery)
        self.status.idle("discovering")
        self._cfg = AdapterConfig("cast", self.broker)
        spawn(self._maintenance_loop(), log=log, name="cast maintenance")
        log.info("cast adapter up")

    # --- discovery (threaded) ----------------------------------------------

    def _start_discovery(self) -> None:
        import pychromecast
        import zeroconf

        self._zconf = zeroconf.Zeroconf()
        self._browser = pychromecast.CastBrowser(
            pychromecast.SimpleCastListener(self._on_add, self._on_remove), self._zconf
        )
        self._browser.start_discovery()

    def _on_add(self, uuid, _service) -> None:  # zeroconf thread
        if uuid in self._devices:
            return
        import pychromecast

        info = self._browser.devices.get(uuid) if self._browser else None
        if info is None:
            return
        try:
            cc = pychromecast.get_chromecast_from_cast_info(info, self._zconf)
        except Exception:
            log.exception("could not create cast device for %s", uuid)
            return
        name = info.friendly_name or str(uuid)
        # Finish registration ON THE LOOP: _devices/_by_eid are read (and iterated
        # in stop()) from the loop thread, so they must only ever be mutated there
        # — never from this zeroconf thread. Mirrors _on_media/_on_cast bridging.
        self._sched(self._register(uuid, cc, name, info.model_name, info.cast_type))

    async def _register(self, uuid, cc, name: str, model_name, cast_type=None) -> None:
        if uuid in self._devices:  # a second callback raced us before scheduling
            return
        eid = entity_id(name)
        if eid in self._by_eid and self._by_eid[eid].uuid != uuid:
            eid = f"{eid}_{str(uuid)[:4]}"
        # A screen is anything that ISN'T a speaker: pychromecast tags speakers
        # "audio"/"group"; TVs/Android-TV boxes/Nest Hubs report "cast" (or, for
        # some boxes, a non-standard string). Default-to-screen unless explicitly a
        # speaker, so an oddly-tagged TV box is still offered as a wall-panel target.
        dev = CastDevice(uuid, cc, eid, name, is_display=(cast_type not in ("audio", "group")))
        self._devices[uuid] = dev
        self._by_eid[eid] = dev
        cc.media_controller.register_status_listener(_MediaListener(self, dev))
        cc.register_status_listener(_CastListener(self, dev))
        # Announce the device identity: the uuid as native_key means a rename in
        # the Google Home app MIGRATES the device instead of minting a duplicate,
        # and the device row is what reachability + the card group hang off.
        caps = list(_BASE_CAPS) + (["media_display"] if dev.is_display else [])
        await self._bus.publish_entity(EntityInfo(
            entity_id=eid, adapter=NAMESPACE, name=name, capabilities=caps,
            device=dev.dev_key, device_name=name, native_key=str(uuid),
        ))
        threading.Thread(target=self._connect, args=(dev,), daemon=True).start()
        self._update_badge()
        log.info("discovered cast %r → %s (%s, cast_type=%s, display=%s)",
                 name, eid, model_name, cast_type, dev.is_display)

    def _connect(self, dev: CastDevice) -> None:  # worker thread
        # Badge counts CONNECTED sockets, not merely-discovered devices: a
        # Chromecast that answers mDNS but can't be reached (VLAN/firewall) used
        # to show green with silently-failing commands.
        try:
            dev.cc.wait(timeout=20)
        except Exception as exc:
            log.warning("cast: connect to %s failed: %s", dev.entity_id, exc, exc_info=True)
            self._sched(self._mark_connected(dev, False))
            return
        self._sched(self._mark_connected(dev, True))

    async def _mark_connected(self, dev: CastDevice, ok: bool) -> None:
        (self._connected.add if ok else self._connected.discard)(dev.uuid)
        await self._publish_reach(dev, ok)
        self._update_badge()

    async def _publish_reach(self, dev: CastDevice, ok: bool) -> None:
        """The socket's verdict for ONE device, edge-triggered — reconcile runs
        every ~20 s, so only a real transition may reach the bus."""
        if self._bus is None or self._reach.get(dev.uuid) == ok:
            return
        self._reach[dev.uuid] = ok
        await set_reachable(self._bus, dev.dev_key, NAMESPACE, ok,
                            detail="" if ok else "cast socket not connected")

    def _update_badge(self) -> None:
        total = len(self._devices)
        if not total:
            self.status.idle("discovering")
            return
        k = len(self._connected)
        if k == total:
            self.status.ok(f"{total} device(s)")
        elif k:
            self.status.ok(f"{k}/{total} connected")
        else:
            self.status.connecting(f"0/{total} connected")

    def _on_remove(self, uuid, _service, _cast_info=None) -> None:
        # Keep the entity (it just goes stale/unavailable); a returning device
        # re-uses the same uuid.
        #
        # pychromecast hands the removal callback THREE arguments (uuid, service,
        # cast_info) — taking two raised a TypeError inside the zeroconf thread,
        # which killed the ServiceBrowser outright: after the first device dropped
        # off mDNS, discovery was dead until the adapter restarted, so a device
        # that came back (or a new one) was never picked up. Silent, because the
        # traceback belonged to a zeroconf thread, not to us. Observed in prod
        # 2026-07-23. cast_info defaults so an older pychromecast still fits.
        pass

    # --- maintenance: badge self-heal + wall-panel keep-alive ---------------

    async def _maintenance_loop(self) -> None:
        """One periodic housekeeper on the loop thread:

        1. Reconciles the connected badge from the live socket state. The boot
           `_connect` probe is one-shot, so a device that answers mDNS but only
           connects LATER (a Nest Hub waking up) used to show as failed forever;
           here it self-heals in both directions.
        2. Keeps the configured wall-panel display showing DIDA's /panel. Whenever
           that device falls back to its idle/ambient screen (fresh boot, or the
           Hub dropped the web view) we re-cast the panel. An ACTIVE cast (music,
           video) leaves it alone — the panel only reclaims an otherwise-idle
           screen, so it behaves like a screensaver, not a hijacker.
        """
        while True:
            try:
                if self._cfg is not None:
                    await self._cfg.load()
                await self._reconcile_connected()
                eid = (self._cfg.get("panel_device").strip() if self._cfg else "")
                if eid:
                    await self._ensure_panel(eid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cast maintenance loop")
            interval = self._cfg.int("panel_check_seconds", 20) if self._cfg else 20
            await asyncio.sleep(max(10, interval))

    async def _reconcile_connected(self) -> None:
        changed = False
        for uuid, dev in self._devices.items():
            sock = getattr(dev.cc, "socket_client", None)
            live = bool(getattr(sock, "is_connected", False))
            if live and uuid not in self._connected:
                self._connected.add(uuid)
                changed = True
            elif not live and uuid in self._connected:
                self._connected.discard(uuid)
                changed = True
            await self._publish_reach(dev, live)
        if changed:
            self._update_badge()

    async def _ensure_panel(self, eid: str) -> None:
        dev = self._by_eid.get(eid)
        if dev is None:
            return  # panel device not discovered (yet) — nothing to reassert onto
        try:
            idle = bool(dev.cc.is_idle)
        except Exception:
            log.debug("cast: idle state of the panel unreadable", exc_info=True)
            idle = False
        if not idle:
            # Busy screen: either our panel, someone's music/video — or a DashCast
            # session whose page died. Only the heartbeat can tell the last one
            # apart, so ask it before walking away.
            if await self._panel_is_dead(dev):
                log.warning(
                    "cast: wall panel %s has not checked in for >%ds — dropping DashCast to re-cast",
                    eid, _PANEL_STALE_S,
                )
                # DashCast can't reload a page it loaded top-level (the receiver
                # message has no listener left), so recovery means quitting to the
                # Backdrop; the next cycle sees an idle screen and casts fresh.
                # Restamp first: one recovery attempt per stale window, not one
                # every 20s while the origin is down.
                await self._stamp_panel_seen()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(dev.cc.quit_app)
            return
        url = await self._panel_url()
        if not url:
            return
        # A panel device whose cast receiver is down (a wedged Nest Hub answers
        # ping + mDNS but refuses :8009, and a dead socket makes is_idle look
        # True) is an EXPECTED outage, not a bug: log the transition once, keep
        # retrying quietly, and announce recovery — instead of a full traceback
        # every cycle. pychromecast's own reconnect loop picks the device back
        # up once its receiver returns (e.g. after a power-cycle).
        from pychromecast.error import NotConnected
        try:
            await asyncio.to_thread(self._cast_panel, dev, url)
        except NotConnected as exc:
            if not self._panel_unreachable:
                self._panel_unreachable = True
                log.warning("cast: wall panel %s unreachable (%s) — retrying each cycle", eid, exc)
            return
        if self._panel_unreachable:
            self._panel_unreachable = False
            log.info("cast: wall panel %s reachable again", eid)
        # Start the liveness clock at the cast: until the fresh page beats for
        # itself, "we put it there N seconds ago" is the honest answer, and it is
        # what makes a panel that never comes up (bad build, unreachable origin)
        # get retried instead of silently accepted.
        await self._stamp_panel_seen()
        # Log the base (never the token): which leg the kiosk was pointed at is the
        # first thing to check when a panel comes up blank on a multi-homed host.
        log.info("cast: (re)asserted wall panel on %s via %s", eid, url.split("/panel?", 1)[0])

    async def _panel_is_dead(self, dev: CastDevice) -> bool:
        """True when this display is running DashCast but the page inside stopped
        checking in. Anything else on screen (music, video, a real cast) is not
        ours to reclaim, and an unknown/unreadable heartbeat means "don't act"."""
        try:
            if getattr(dev.cc.status, "app_id", None) != _DASHCAST_APP_ID:
                return False
        except Exception:
            log.debug("cast: panel status unreadable", exc_info=True)
            return False
        if self.broker is None:
            return False
        age = await self.broker.call("setting_age", key="panel_seen_at")
        return age is not None and float(age) > _PANEL_STALE_S

    async def _stamp_panel_seen(self) -> None:
        if self.broker is None:
            return
        await self.broker.call("set_setting", key="panel_seen_at", value=datetime.now(UTC).isoformat())

    async def _panel_url(self) -> str | None:
        # The kiosk pulls its page, its camera MJPEG and its whole photo slideshow
        # from us — over a LAN link that never needs to leave the house. Casting
        # the public URL sent all of that out to Cloudflare and back, which also
        # made the wall panel fail whenever the tunnel did: a reload that raced a
        # deploy pinned a Cloudflare error page on the wall (2026-07-22). local_ip()
        # is the host's declared LAN leg (lan_ip) — the same fact the AV adapters
        # bind SSDP to, stated once. The public address (also a stored
        # setting) covers a host that declares no LAN leg.
        if self.broker is None:
            return None
        token = await self.broker.call("setting", key="panel_token")
        if not token:
            return None
        ip = await local_ip(self.broker)
        if ip and not ip.startswith(("0.", "127.")):
            base = f"http://{ip}:{os.environ.get('DIDA_UI_PORT', '5273').strip() or '5273'}"
        else:
            base = (await host_setting(self.broker, "app_url")).strip()
        if not base:
            return None
        return f"{base.rstrip('/')}/panel?k={token}"

    def _cast_panel(self, dev: CastDevice, url: str) -> None:  # worker thread
        from pychromecast.controllers.dashcast import DashCastController

        if dev.dashcast is None:
            dev.dashcast = DashCastController()
            dev.cc.register_handler(dev.dashcast)
        dev.dashcast.load_url(url, force=True)

    # --- status (bridged onto the loop) ------------------------------------

    def _sched(self, coro) -> None:
        loop = self._loop
        if loop is not None:
            # spawn (not bare ensure_future): keeps a strong ref + logs failures.
            loop.call_soon_threadsafe(lambda: spawn(coro, log=log, name="cast publish"))

    def _on_media(self, dev: CastDevice, status) -> None:
        self._sched(self._publish_media(dev, status))

    def _on_cast(self, dev: CastDevice, status) -> None:
        self._sched(self._publish_cast(dev, status))

    async def _publish_media(self, dev: CastDevice, status) -> None:
        caps = read_media(status)
        transport = caps.get("media_transport")
        for cap, value in caps.items():
            if dev.last.get(cap) != value:
                dev.last[cap] = value
                await self._publish(dev, cap, value)

        pos = getattr(status, "current_time", None)
        if pos is None:
            return
        pos = int(pos)
        mono = time.monotonic()
        expected: float | None = None
        if dev.pos_base is not None and transport == "playing":
            base_pos, base_mono = dev.pos_base
            expected = base_pos + (mono - base_mono)
        publish = (
            (expected is None and (dev.last.get("media_position") != pos or transport == "playing"))
            or (expected is not None and abs(pos - expected) > 3)
        )
        if publish:
            dev.last["media_position"] = pos
            dev.pos_base = (pos, mono)
            await self._publish(dev, "media_position", pos)

    async def _publish_cast(self, dev: CastDevice, status) -> None:
        # A Cast device is a media player even when idle (no media session yet) —
        # the cast-status callback reliably fires on connect, so seed transport.
        if "media_transport" not in dev.last:
            dev.last["media_transport"] = "idle"
            await self._publish(dev, "media_transport", "idle")
            # A screen (Nest Hub / Android TV) advertises media_display so the UI can
            # offer it as a wall-panel target; speakers never report it. Diagnostic:
            # it's a static device property, not a now-playing reading.
            if dev.is_display:
                await self._publish(dev, "media_display", True, category="diagnostic")
        vol = getattr(status, "volume_level", None)
        if vol is not None:
            v = max(0, min(100, round(float(vol) * 100)))
            if dev.last.get("volume") != v:
                dev.last["volume"] = v
                await self._publish(dev, "volume", v)
        muted = getattr(status, "volume_muted", None)
        if muted is not None:
            m = bool(muted)
            if dev.last.get("mute") != m:
                dev.last["mute"] = m
                await self._publish(dev, "mute", m)

    async def _publish(self, dev: CastDevice, capability: str, value: object,
                       *, category: str = "control") -> None:
        assert self._bus is not None
        await self._bus.publish_state(
            StateUpdate(
                entity_id=dev.entity_id,
                capability=capability,
                value=value,  # type: ignore[arg-type]
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit=_UNITS.get(capability),
                name=dev.name,
                category=category,
                device=dev.dev_key,
                device_name=dev.name,
            )
        )

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        dev = self._by_eid.get(command.entity_id)
        if dev is None:
            raise CommandRejected("unknown cast device")
        try:
            await asyncio.to_thread(
                self._dispatch, dev, command.capability, command.command, dict(command.args)
            )
        except Exception as exc:
            raise CommandRejected(f"device refused: {exc}") from exc

    def _dispatch(self, dev: CastDevice, cap: str, cmd: str, args: dict) -> None:  # worker thread
        cc = dev.cc
        if cap == "media_transport":
            self._transport(dev, cmd, args)
        elif cap == "volume":
            if cmd == "set_volume":
                cc.set_volume(max(0, min(100, int(args.get("value", 0)))) / 100)
            elif cmd == "volume_up":
                cc.volume_up()
            elif cmd == "volume_down":
                cc.volume_down()
        elif cap == "mute":
            muted = {"mute": True, "unmute": False, "toggle": not bool(dev.last.get("mute"))}
            if cmd in muted:
                cc.set_volume_muted(muted[cmd])

    @staticmethod
    def _transport(dev: CastDevice, cmd: str, args: dict) -> None:  # worker thread
        mc = dev.cc.media_controller
        if cmd == "play_pause":
            cmd = "pause" if dev.last.get("media_transport") == "playing" else "play"
        simple = {"play": mc.play, "pause": mc.pause, "stop": mc.stop,
                  "next": mc.queue_next, "previous": mc.queue_prev}
        if cmd in simple:
            simple[cmd]()
        elif cmd == "play_media":
            url = args.get("uri") or args.get("url")
            if url:
                mc.play_media(str(url), str(args.get("mime") or "audio/mpeg"))

    async def stop(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.stop_discovery()
        for dev in self._devices.values():
            with contextlib.suppress(Exception):
                dev.cc.disconnect(blocking=False)
        if self._zconf is not None:
            with contextlib.suppress(Exception):
                self._zconf.close()
