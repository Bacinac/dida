"""HEOS adapter — Denon/Marantz players via the HEOS CLI (TCP 1255).

This is the proper control plane for Denon/Marantz: volume, transport, mute,
now-playing and URL casting all work here (UPnP RenderingControl on these
devices is a no-op — see the DLNA adapter, which deliberately skips them).

A HEOS player maps onto the same media capability group as a DLNA renderer, so
it shows up in the same `/media` UI with no frontend change. State is pushed:
pyheos delivers async HEOS events, so we snapshot a player whenever it signals
a change — no polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time

from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    StateUpdate,
    local_ip,
    set_reachable,
    ssdp_msearch,
)
from home_core.tasks import spawn

from dida_adapter_heos.mapping import NAMESPACE, entity_id, read_player, read_position

log = logging.getLogger("dida.adapter.heos")

RECONNECT_SECONDS = 15.0
ACT_DENON_ST = "urn:schemas-denon-com:device:ACT-Denon:1"
_UNITS = {"volume": "%", "media_duration": "s", "media_position": "s"}


def _discover_heos_host(iface_ip: str, timeout: float = 3.0) -> str | None:
    """SSDP M-SEARCH for a Denon/HEOS device → its IP (one host is enough: HEOS
    `get_players` returns the whole multiroom system from any member)."""
    # Only ACT-Denon devices answer this ST, so the first reply is a HEOS member.
    for sender_ip, data in ssdp_msearch(iface_ip, [ACT_DENON_ST], timeout, stop_on_first=True):
        m = re.search(rb"location:\s*https?://([0-9.]+)", data, re.IGNORECASE)
        return m.group(1).decode() if m else sender_ip
    return None


_SID_RE = re.compile(r"\bs\d{4,}\b")


def _tunein_id(media_id: str, image: str) -> str | None:
    """Extract a TuneIn station id (sNNNN) from a favorite's media_id or logo URL."""
    m = _SID_RE.search(media_id or "") or _SID_RE.search(image or "")
    return m.group(0) if m else None


# Some TuneIn entries resolve to a low-bitrate / metadata-less mount. Prefer a
# known better DIRECT stream for these (higher bitrate + ICY now-playing), so
# the central favorite plays the good stream on DLNA players too, not just HEOS.
_STATION_OVERRIDES: dict[str, str] = {
    "s241195": "https://stream.yammat.fm/radio/8000/yammat.mp3",  # Yammat FM: 320 AAC + metadata (was 192 MP3, "No Name", no StreamTitle)
}


def _resolve_tunein(station_id: str) -> str | None:
    """TuneIn station id → a directly-playable stream URL via the public opml
    endpoint (the same resolver Music Assistant / HEOS use internally). Blocking
    (urllib) — call via asyncio.to_thread."""
    import urllib.request

    tune = f"http://opml.radiotime.com/Tune.ashx?id={station_id}&formats=mp3,aac,ogg,flac"
    try:
        text = urllib.request.urlopen(tune, timeout=6).read().decode("utf-8", "replace")
    except Exception:
        log.debug("heos: TuneIn link not resolved", exc_info=True)
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("http"):
            continue
        if line.lower().rsplit("?", 1)[0].endswith((".pls", ".m3u", ".m3u8")):
            inner = _resolve_playlist(line)
            if inner:
                return inner
            continue
        return line
    return None


def _resolve_playlist(url: str) -> str | None:
    """Follow a .pls/.m3u one level → the first concrete stream URL inside."""
    import urllib.request

    try:
        text = urllib.request.urlopen(url, timeout=6).read().decode("utf-8", "replace")
    except Exception:
        log.debug("heos: playlist not resolved", exc_info=True)
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:  # .pls  File1=http://...
            return line.split("=", 1)[1].strip()
        if line.startswith("http") and not line.lower().endswith((".pls", ".m3u", ".m3u8")):
            return line
    return None


class Tracked:
    """One HEOS player: the pyheos handle + last-published snapshot."""

    def __init__(self, pid: int, eid: str, name: str, player) -> None:
        self.pid = pid
        self.entity_id = eid
        self.name = name
        self.player = player
        # Group with the AVR zones (denon adapter) into one card: both key on the
        # device IP. None if pyheos doesn't expose it (then it stays standalone).
        self.device_key: str | None = getattr(player, "ip_address", None)
        self.last: dict[str, object] = {}
        self.pos_base: tuple[int, float] | None = None
        self.remove_cb = None


class HeosAdapter:
    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._heos = None
        self._tracked: dict[int, Tracked] = {}
        self._by_eid: dict[str, Tracked] = {}
        self._connected = False
        self._stopping = False  # gate the disconnect->reconnect handler during shutdown
        self._reach: bool | None = None  # last reachability we published (edge-trigger)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fav_payload: str | None = None  # cached HEOS favorites JSON
        self._fav_url_cache: dict[str, str] = {}  # TuneIn station id → resolved stream URL
        # Both event names get safe defaults here (before add_on_heos_event can
        # fire) — _connect re-asserts them from pyheos.const once connected.
        self._sources_changed_event = "event/sources_changed"
        self._players_changed_event = "event/players_changed"
        self._cfg: AdapterConfig | None = None
        self._iface_ip = ""  # set in start() from the merged config

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._loop = asyncio.get_running_loop()
        self._cfg = AdapterConfig("heos", self.broker)
        await self._cfg.load()
        self._iface_ip = self._cfg.get("iface_ip") or await local_ip(self.broker)
        spawn(self._cfg.poll_loop(), log=log, name="heos config poll")
        spawn(self._connect_loop(), log=log, name="heos connect loop")
        log.info("heos adapter up — iface=%s", self._iface_ip)

    def _schedule(self, coro) -> None:
        """Run a coroutine on our loop from a (possibly off-loop) pyheos event
        callback. pyheos dispatches callbacks without a running loop in the
        calling context, so create_task() there fails — go through the captured
        loop with call_soon_threadsafe."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: spawn(coro, log=log, name="heos task"))

    # --- connection / discovery -------------------------------------------

    async def _resolve_host(self) -> str | None:
        pinned = (self._cfg.get("host") if self._cfg else "").strip()
        if pinned:
            return pinned
        # Blocking SSDP (up to 3 s) must run OFF the loop — while unconfigured it
        # used to freeze commands/status/health every retry tick.
        return await asyncio.to_thread(_discover_heos_host, self._iface_ip)

    async def _connect_loop(self) -> None:
        while not self._connected:
            host = await self._resolve_host()
            if host:
                try:
                    await self._connect(host)
                    self._connected = True
                    return
                except Exception:
                    log.exception("HEOS connect to %s failed; retrying", host)
                    self.status.error(f"connect failed @ {host}")
            else:
                log.warning("no HEOS device discovered yet; retrying")
                self.status.idle("no device found")
            await asyncio.sleep(RECONNECT_SECONDS)

    async def _connect(self, host: str) -> None:
        from pyheos import Heos, const

        # auto_reconnect rides out device/network blips; all_progress_events=False
        # means we get occasional progress events (we extrapolate the playhead).
        self.status.connecting(host)
        heos = await Heos.create_and_connect(
            host, auto_reconnect=True, all_progress_events=False
        )
        try:
            players = await heos.get_players()
            await heos.register_for_change_events(True)
        except Exception:
            # Tear the half-open client down before the retry mints a new one —
            # each failed attempt used to leak a socket + auto-reconnect task.
            with contextlib.suppress(Exception):
                await heos.disconnect()
            raise
        self._heos = heos
        self._heos.add_on_heos_event(self._on_heos_event)
        # Badge follows pyheos's OWN reconnect cycle — without these callbacks an
        # AVR reboot left the badge green while events were dead for hours.
        self._heos.add_on_connected(self._on_reconnected)
        self._heos.add_on_disconnected(self._on_disconnected)
        for player in players.values():
            self._adopt(player)
        log.info("connected HEOS @ %s — %d player(s)", host, len(players))
        self.status.ok(f"{len(players)} player(s)")
        await self._publish_reach(True)
        self._players_changed_event = const.EVENT_PLAYERS_CHANGED
        self._sources_changed_event = const.EVENT_SOURCES_CHANGED
        self._schedule(self._refresh_favorites())

    def _on_reconnected(self) -> None:
        self.status.ok(f"{len(self._tracked)} player(s)")
        self._schedule(self._publish_reach(True))

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """ONE socket fronts every tracked player, so its verdict applies to each
        of their devices — edge-triggered so pyheos's reconnect cycle can't spam."""
        if self._bus is None or self._reach == ok:
            return
        self._reach = ok
        for tr in list(self._tracked.values()):
            if tr.device_key:
                await set_reachable(self._bus, tr.device_key, NAMESPACE, ok,
                                    detail="" if ok else detail)

    def _on_disconnected(self) -> None:
        # pyheos's own auto_reconnect only ever retries the ORIGINAL ip, so once the
        # AVR takes a new DHCP lease it's stranded (green-then-dead) forever. Relaunch
        # our OWN resolve+connect supervisor so _resolve_host re-runs (SSDP / pinned
        # host) and reconnects wherever the receiver moved — mirroring denon, whose
        # _connect_loop re-resolves on every attempt.
        self.status.error("connection lost (reconnecting)")
        self._schedule(self._publish_reach(False, "connection lost"))
        if self._stopping or not self._connected:
            return  # shutting down, or a reconnect is already in flight
        self._connected = False
        self._schedule(self._reconnect())

    async def _reconnect(self) -> None:
        # Drop the dead client (also stops pyheos hammering the stale ip) and forget
        # the tracked players — their pyheos handles belong to the old connection.
        # _connect re-adopts fresh ones under the same name-derived entity ids.
        old, self._heos = self._heos, None
        if old is not None:
            with contextlib.suppress(Exception):
                await old.disconnect()
        for tr in self._tracked.values():
            if tr.remove_cb is not None:
                with contextlib.suppress(Exception):
                    tr.remove_cb()
        self._tracked.clear()
        self._by_eid.clear()
        await self._connect_loop()

    def _on_heos_event(self, event: str) -> None:
        if event == self._players_changed_event:
            self._schedule(self._reenumerate())
        elif event == self._sources_changed_event:
            self._schedule(self._refresh_favorites())

    async def _refresh_favorites(self) -> None:
        """Pull the account's HEOS favorites (radio presets) and publish them as
        a JSON list on every player, so the UI can render them and play one by
        preset index. Requires the receiver to be signed into a HEOS account."""
        if self._heos is None:
            return
        try:
            favs = await self._heos.get_favorites()
        except Exception:
            log.debug("get_favorites failed", exc_info=True)
            return
        items = []
        for idx, f in sorted(favs.items()):
            name = getattr(f, "name", "") or f"Preset {idx}"
            image = getattr(f, "image_url", "") or ""
            media_id = str(getattr(f, "media_id", "") or "")
            station = _tunein_id(media_id, image)
            # Resolve the station id → a directly-playable stream URL so the
            # favorite is CENTRAL: HEOS plays it by preset, but the iFi / Cast /
            # any DLNA renderer can play the same favorite via this URL. Cache it
            # (favorites change rarely) and resolve off the event loop.
            url = ""
            if station:
                url = _STATION_OVERRIDES.get(station) or self._fav_url_cache.get(station, "")
                if not url:
                    url = await asyncio.to_thread(_resolve_tunein, station) or ""
                    if url:
                        self._fav_url_cache[station] = url
            items.append({
                "preset": int(idx),
                "name": name,
                "image": image,
                "station": station or "",
                "url": url,
            })
        self._fav_payload = json.dumps(items, ensure_ascii=False)
        for tr in self._tracked.values():
            await self._publish(tr, "media_favorites", self._fav_payload)

    async def _reenumerate(self) -> None:
        if self._heos is None:
            return
        try:
            players = await self._heos.get_players(refresh=True)
        except Exception:
            log.exception("HEOS get_players refresh failed")
            return
        for player in players.values():
            if player.player_id not in self._tracked:
                self._adopt(player)

    def _adopt(self, player) -> None:
        pid = player.player_id
        if pid in self._tracked:
            return
        name = player.name or f"HEOS {pid}"
        eid = entity_id(name)
        if eid in self._by_eid and self._by_eid[eid].pid != pid:
            eid = f"{eid}_{pid}"
        tr = Tracked(pid, eid, name, player)
        self._tracked[pid] = tr
        self._by_eid[eid] = tr
        tr.remove_cb = player.add_on_player_event(lambda event, t=tr: self._on_player_event(t))
        log.info("adopted HEOS player %r (pid=%s) → %s", name, pid, eid)
        spawn(self._refresh_and_snapshot(tr), log=log, name="heos player snapshot")
        if self._fav_payload is not None:  # a later-discovered player still gets favorites
            spawn(self._publish(tr, "media_favorites", self._fav_payload), log=log, name="heos favorites publish")

    def _on_player_event(self, tr: Tracked) -> None:
        # pyheos has already updated the player's attributes before dispatching.
        self._schedule(self._snapshot(tr))

    async def _refresh_and_snapshot(self, tr: Tracked) -> None:
        # get_players() doesn't populate live volume/now-playing — pull a fresh
        # read so the first snapshot is accurate, not a default.
        try:
            await tr.player.refresh()
        except Exception:
            log.debug("refresh failed for %s", tr.entity_id, exc_info=True)
        await self._snapshot(tr)

    # --- state -------------------------------------------------------------

    async def _snapshot(self, tr: Tracked) -> None:
        caps = read_player(tr.player)
        transport = caps.get("media_transport")
        for cap, value in caps.items():
            if tr.last.get(cap) != value:
                tr.last[cap] = value
                await self._publish(tr, cap, value)

        pos = read_position(tr.player)
        if pos is None:
            return
        mono = time.monotonic()
        expected: float | None = None
        if tr.pos_base is not None and transport == "playing":
            base_pos, base_mono = tr.pos_base
            expected = base_pos + (mono - base_mono)
        publish = (
            (expected is None and (tr.last.get("media_position") != pos or transport == "playing"))
            or (expected is not None and abs(pos - expected) > 3)
        )
        if publish:
            tr.last["media_position"] = pos
            tr.pos_base = (pos, mono)
            await self._publish(tr, "media_position", pos)

    async def _publish(self, tr: Tracked, capability: str, value: object) -> None:
        assert self._bus is not None
        await self._bus.publish_state(
            StateUpdate(
                entity_id=tr.entity_id,
                capability=capability,
                value=value,  # type: ignore[arg-type]
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit=_UNITS.get(capability),
                name=tr.name,
                device=tr.device_key,
            )
        )

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        tr = self._by_eid.get(command.entity_id)
        if tr is None:
            raise CommandRejected("unknown HEOS player")
        try:
            await self._dispatch(tr.player, command.capability, command.command, dict(command.args))
        except Exception as exc:
            raise CommandRejected(f"player refused: {exc}") from exc

    async def _dispatch(self, player, cap: str, cmd: str, args: dict) -> None:
        if cap == "media_transport":
            if cmd == "play":
                await player.play()
            elif cmd == "pause":
                await player.pause()
            elif cmd == "stop":
                await player.stop()
            elif cmd == "next":
                await player.play_next()
            elif cmd == "previous":
                await player.play_previous()
            elif cmd == "play_pause":
                raw = getattr(getattr(player, "state", None), "value", None)
                await (player.pause() if raw == "play" else player.play())
            elif cmd == "play_media":
                url = args.get("uri") or args.get("url")
                if url:
                    await player.play_url(str(url))
            elif cmd == "play_preset":
                idx = int(args.get("index", 0))
                if idx:
                    await player.play_preset_station(idx)
        # volume/mute intentionally NOT handled — owned by the denon AVR adapter.

    async def stop(self) -> None:
        self._stopping = True  # so the disconnect callback below doesn't relaunch us
        for tr in self._tracked.values():
            if tr.remove_cb is not None:
                with contextlib.suppress(Exception):
                    tr.remove_cb()
        if self._heos is not None:
            with contextlib.suppress(Exception):
                await self._heos.disconnect()
