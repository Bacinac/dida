"""Volumio adapter — drives the iFi Zen Stream via its NATIVE Volumio API.

The Zen Stream runs Volumio. We previously drove it as a generic DLNA renderer,
which fought the device: getState desync, no album-art echo, and the renderer
ignoring SetAVTransportURI mid-play (forcing a stop-and-wait hack). Volumio's own
REST API is fundamentally better — one `getState` gives transport, volume, rich
now-playing (title/artist/album/albumart), real sample-rate/bit-depth, and an
accurate playhead; commands are one HTTP GET; casting is `replaceAndPlay`.

Design:
  * ONE configured host (the Zen's IP) — not SSDP discovery. A Volumio box is a
    known, named appliance, not something to stumble on. So this adapter needs no
    host networking (unlike dlna): it reaches the device and NATS over the bridge.
  * State by polling `getState` every `poll_seconds` (default 2). Volumio's state
    is reliable, so this is simpler and quieter than the DLNA poll — no title-match
    gapless hacks (Volumio's queue is native), no stop-and-wait.
  * Casting DIDA content (library tracks, internet radio) reuses the SAME command
    surface as dlna (`play_media`/`play_queue`/`play_index`) so this is a drop-in
    replacement. Library metadata is published optimistically because Volumio's
    webradio service echoes only the title.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import time

from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    icy_stream_title,
    opus_base,
    radio_probe,
    radio_stations,
    set_reachable,
)
from home_core.tasks import spawn

from dida_adapter_volumio.mapping import (
    NAMESPACE,
    entity_id,
    mpd_uri,
    read_state,
    source_of,
    station_url,
)

log = logging.getLogger("dida.adapter.volumio")

_VOL_STEP = 5  # percent
_STATIONS_TTL = 60.0  # seconds — the station list is small and rarely edited, but the
#                       poll runs every ~2s and must not query the DB each time.
_STALL_AFTER = 20.0  # seconds of frozen MPD elapsed while claiming "play" before the
#                      watchdog declares the stream wedged and re-casts. Long enough to
#                      absorb a slow buffer-fill at cast start; a real wedge is forever.
_UNITS = {"volume": "%", "media_duration": "s", "media_position": "s"}
# The entity's capability catalog (announced via EntityInfo). Volume is omitted:
# the Zen is a bit-perfect fixed-output renderer (mixer=None), so it has none.
_CAPS = [
    "media_transport", "mute", "media_title", "media_artist", "media_album",
    "media_art", "media_quality", "media_source", "media_queue",
    "media_duration", "media_position",
]
# now-playing metadata Volumio's webradio service does NOT echo for DIDA-cast
# library content — we own these optimistically while a DIDA cast is active.
_OWNED = ("media_artist", "media_album", "media_art")


class Player:
    """The one Volumio device: its address + last-published snapshot."""

    def __init__(self, eid: str, name: str, host: str) -> None:
        self.entity_id = eid
        self.name = name
        self.host = host
        self.last: dict[str, object] = {}       # cap -> last published (dedupe)
        self.pos_base: tuple[int, float] | None = None  # (position_s, monotonic) the UI extrapolates from
        self.cast_active = False                # True while DIDA owns the metadata
        self.cast_title = ""                    # title WE cast — detects external switches
        self.queue: list[dict] = []             # DIDA-cast queue (for media_queue view)
        self.radio: dict | None = None          # {url, station, logo} while playing radio
        self.icy_task: object | None = None     # bg task polling the upstream ICY now-playing
        self.radio_idle = 0                     # consecutive stopped polls while in radio mode
        self.radio_switch = 0                   # consecutive off-station polls (debounce announcement blips)
        self.radio_resuming = False             # a self-heal re-entry is in flight (guards double-start)
        self.live_elapsed: float | None = None  # last MPD `status` elapsed sample (decode liveness)
        self.live_at = 0.0                      # monotonic when elapsed last CHANGED (= last proven decode)
        self.stalled = False                    # wedge declared: MPD claims play with elapsed frozen
        self.stopped_at: float | None = None    # monotonic since MPD reports stop while Volumio claims play
        self.recast_at = 0.0                    # monotonic of the last watchdog re-cast (paces the retry)
        self.probe_warned = False               # MPD liveness probe failing (warn once, not per poll)


class VolumioAdapter:
    """Bridges an iFi Zen Stream (Volumio) onto the DIDA bus via the native API."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self.broker = None
        self._session = None
        self._player: Player | None = None
        self._fails = 0
        self._reach: bool | None = None  # last reachability we published (edge-trigger)
        self._stations: dict[str, dict] = {}    # station url -> row (see _station_for)
        self._stations_at = 0.0                 # monotonic of the last snapshot

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._cfg = AdapterConfig("volumio", self.broker)
        await self._cfg.load()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "DIDA"},
        )
        spawn(self._cfg.poll_loop(), log=log, name="volumio config poll")
        host = self._cfg.get("host")
        if not host:
            self.status.error("no host IP (Settings → Adapters)")
            log.warning("volumio adapter: no host configured — idle until set")
        spawn(self._poll_loop(), log=log, name="volumio poll loop")
        self.status.idle("connecting")
        log.info("volumio adapter up — host=%s poll=%ss", host or "(unset)", self._cfg.int("poll_seconds", 2))

    # --- HTTP helpers ------------------------------------------------------

    def _base(self) -> str:
        return f"http://{self._cfg.get('host')}/api/v1"

    async def _get(self, path: str) -> dict | None:
        assert self._session is not None
        async with self._session.get(f"{self._base()}/{path}") as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)

    async def _cmd(self, cmd: str, **params) -> None:
        assert self._session is not None
        q = "&".join([f"cmd={cmd}"] + [f"{k}={v}" for k, v in params.items()])
        async with self._session.get(f"{self._base()}/commands/?{q}") as resp:
            if resp.status != 200:
                log.warning("volumio cmd %s → HTTP %s", cmd, resp.status)

    async def _post(self, path: str, body: dict) -> None:
        assert self._session is not None
        async with self._session.post(f"{self._base()}/{path}", json=body) as resp:
            if resp.status != 200:
                log.warning("volumio POST %s → HTTP %s", path, resp.status)

    async def _mpd(self, p: Player, commands: list[str]) -> None:
        """Speak MPD directly (:6600) — the ONLY way a library queue actually behaves.

        Volumio's own queue cannot do it: its `mpd` service resolves uris against ITS
        library, so our signed HTTP urls are dropped outright, and its `webradio` service
        pushes only the CURRENT item to MPD — so the queue never advances and seek is
        ignored. MPD underneath has no such problem: it takes http urls natively, walks
        its own queue, and seeks them (OPUS serves Range). Measured on the Zen: two
        queued tracks, song 0 → 1 by itself at the first track's end; `seekcur 300` moved
        elapsed 21s → 306s. Volumio does not fight this and its getState mirrors it
        (status/seek/title), so DIDA's polling is unaffected.

        Mounting the library on the renderer instead was rejected: that mount has a
        history of dropping (Ivo, from experience), and a queue that vanishes with a
        mount is worse than one that never needed it.
        """
        reader, writer = await asyncio.open_connection(p.host, 6600)
        try:
            await reader.readline()  # "OK MPD <version>" banner
            for c in commands:
                writer.write((c + "\n").encode())
            await writer.drain()
            # One reply block per command; ACK is MPD saying no — surface it, never
            # let a half-built queue look like a queued album.
            for c in commands:
                while True:
                    line = (await reader.readline()).decode().strip()
                    if line == "OK":
                        break
                    if line.startswith("ACK") or not line:
                        raise RuntimeError(f"MPD refused `{c.split()[0]}`: {line or 'closed'}")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _mpd_arg(s: str) -> str:
        """MPD argument quoting — a url with a space or a quote would split the command."""
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    async def _mpd_status(self, p: Player) -> dict[str, str]:
        """One MPD `status` round-trip → its key/value pairs. Read-only, so it cannot
        wedge Volumio's state ownership the way driving playback behind its back does
        (see _cast_one) — and it is the ONLY truthful decode signal: getState's status
        and seek are Volumio's own bookkeeping, which keeps saying "play" with an
        interpolated playhead long after the underlying stream connection has died."""
        reader, writer = await asyncio.open_connection(p.host, 6600)
        try:
            await reader.readline()  # "OK MPD <version>" banner
            writer.write(b"status\n")
            await writer.drain()
            out: dict[str, str] = {}
            while True:
                line = (await reader.readline()).decode().strip()
                if line == "OK":
                    return out
                if line.startswith("ACK") or not line:
                    raise RuntimeError(f"MPD status failed: {line or 'closed'}")
                key, _, val = line.partition(":")
                out[key.strip()] = val.strip()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # --- state polling -----------------------------------------------------

    async def _ensure_player(self) -> Player | None:
        """Resolve the device name once (getSystemInfo) → stable entity_id. If the
        configured host changed (a Settings → Adapters edit), rebuild: p.host is the
        device grouping key, the album-art URL prefix and the probe_url, so a cached
        Player would keep talking to (and pointing at) the old IP even though _base()
        already reads the new one."""
        host = self._cfg.get("host")
        if self._player is not None:
            if host and host != self._player.host:
                log.info("volumio host changed (%s → %s) — rebuilding player", self._player.host, host)
                self._stop_icy(self._player)
                self._player = None
            else:
                return self._player
        if not host:
            return None
        info = await self._get("getSystemInfo")
        name = str((info or {}).get("name") or self._cfg.get("name") or "iFi")
        eid = entity_id(name)
        self._player = Player(eid, name, host)
        # Announce the entity + its capability catalog so the engine classifies it
        # (device_type "media") and Settings can list/expose its fields. Without
        # this the entity is born from state alone and stays type-less. device=host
        # groups the player and its action buttons into one device card.
        assert self._bus is not None
        await self._bus.publish_entity(EntityInfo(
            entity_id=eid, adapter=NAMESPACE, name=name, device_type="media",
            device=host, device_name=name, capabilities=_CAPS,
        ))
        # Action buttons (press) for what Volumio's REST can't do over HTTP, done
        # over SSH. "Restart audio" also applies the configured MPD buffer.
        for suffix, label in (("restart", f"{name} — Restart audio"), ("reboot", f"{name} — Reboot")):
            await self._bus.publish_entity(EntityInfo(
                entity_id=f"{eid}_{suffix}", adapter=NAMESPACE, name=label,
                device_type="button", device=host, device_name=name, capabilities=["press"],
            ))
        # Self-register this renderer's signal-path config (media_renderer_output):
        # probe_url is ALWAYS our own getState endpoint; the DAC + digital link are
        # optional user config. Owning it here means it self-heals across an entity
        # rename or a DB reset — the DLNA→Volumio switch orphaned a hand-inserted row.
        await self.broker.call(
            "media_output", entity_id=eid, probe_url=f"http://{host}/api/v1/getState",
            dac=self._cfg.get("dac") or None, link=self._cfg.get("dac_link") or None,
        )
        log.info("volumio player %r → %s at %s", name, eid, host)
        return self._player

    async def _poll_loop(self) -> None:
        while True:
            interval = self._cfg.int("poll_seconds", 2)
            try:
                p = await self._ensure_player()
                if p is not None:
                    await self._poll(p)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fails += 1
                if self._fails == 3:
                    log.warning("volumio unreachable (3 polls): %s", exc)
                    if self._player is not None:
                        await self._set(self._player, "media_transport", "idle")
                    self.status.error("device unreachable")
                    await self._publish_reach(False, str(exc) or "no reply to polls")
                else:
                    log.debug("volumio poll failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """The poll's verdict at the SAME threshold as the badge (3 fails), edge-
        triggered — keyed by the host every entity of this player hangs off."""
        host = self._player.host if self._player else ""
        if self._bus is None or not host or self._reach == ok:
            return
        self._reach = ok
        await set_reachable(self._bus, host, NAMESPACE, ok, detail="" if ok else detail)

    async def _poll(self, p: Player) -> None:
        state = await self._get("getState")
        if state is None:
            raise RuntimeError("getState returned non-200")
        if self._fails:
            log.info("volumio reachable again")
        self._fails = 0
        self.status.ok(p.name)
        await self._publish_reach(True)

        snap = read_state(state, p.host)
        uri = str(state.get("uri") or "")
        transport = snap.get("media_transport")

        base = await opus_base(self.broker)
        # Radio: we cast the station's OWN stream url (no relay), so the box echoes that
        # url back and "our radio is on" means the uri IS one of the configured stations
        # — matched positively, not guessed from a url shape.
        station = await self._station_for(uri)
        on_station = station is not None
        # Anything served by OPUS (uri under `base`) IS ours — a record the house put
        # on, whose getState.title may be the URI, which must NOT be mistaken for an
        # external switch (it would then clobber our metadata with the URL).
        ours = bool(uri) and bool(base) and uri.startswith(base)

        await self._drop_external_cast(p, ours, on_station, snap.get("media_title"))
        self._track_radio(p, uri, station, transport)
        if p.radio and transport == "playing":
            await self._radio_watchdog(p)

        queue_owned = await self._queue_metadata(p, state, ours)
        owned = self._owned_caps(p, on_station, queue_owned)
        for cap, value in snap.items():
            if cap in owned:
                continue
            await self._set(p, cap, value)

        # media_source: our cast set it; for external content classify from uri/service.
        # A configured station IS radio whoever started it — source_of cannot tell (a
        # station url has no recognisable shape), so decide that here.
        if not p.cast_active:
            await self._set(p, "media_source",
                            "radio" if on_station else source_of(uri, str(state.get("service") or "")))
        await self._publish_position(p, state.get("seek"), transport)

    async def _drop_external_cast(self, p: Player, ours: bool, on_station: bool, title) -> None:
        """External switch (NON-radio): the device is playing something DIDA didn't
        cast (its own Tidal/Spotify, a track picked on the box). Radio is excluded
        here — a station's getState.title is the live ICY song, which changes with
        every track and never matches our cast title (the station name), so this
        would false-trigger on every song."""
        if p.cast_active and not p.radio and not ours and not on_station and title and title != p.cast_title:
            self._stop_icy(p)
            p.cast_active = False
            p.queue = []
            await self._set(p, "media_queue", "")

    def _track_radio(self, p: Player, uri: str, station, transport) -> None:
        """Radio ends when the user switches the box off our station or the stream
        genuinely stops (~15s / 5 polls)."""
        if not p.radio:
            if station is not None and transport == "playing" and not p.radio_resuming:
                # A station is playing but radio mode was torn down — e.g. a TTS
                # announcement briefly cast its own URL onto the box (uri left the station
                # → 'switched off'), then playback returned but nothing re-armed ICY.
                # Self-heal: rebuild the station context and restart the ICY poll, so the
                # live song returns and the source badge says radio again.
                p.radio_resuming = True
                spawn(self._resume_radio(p, station), log=log, name="volumio radio resume")
            return
        if bool(uri) and station_url(uri) != str(p.radio.get("url") or ""):
            # A one-poll blip (a TTS announcement briefly casting its own URL over
            # the station) must NOT tear radio down — require 2 consecutive polls
            # off our station before declaring a real user switch.
            p.radio_switch += 1
            if p.radio_switch >= 2:
                log.info("volumio %s: switched off the station (%s) — leaving radio",
                         p.entity_id, uri)
                self._stop_icy(p)
                p.cast_active = False
            return
        p.radio_switch = 0
        if transport not in ("stopped", "idle"):
            p.radio_idle = 0
            return
        p.radio_idle += 1
        if p.radio_idle >= 5:
            log.info("volumio %s: radio stopped — leaving radio mode", p.entity_id)
            self._stop_icy(p)
            p.cast_active = False

    async def _queue_metadata(self, p: Player, state: dict, ours: bool) -> tuple[str, ...]:
        """A DIDA-cast queue: OUR per-track metadata is authoritative and keyed off
        the Volumio queue position, so it stays right as the queue auto-advances.
        Essential for a tagless Tidal proxy FLAC (Volumio would report the uri as
        the title); harmless for tagged local files (same values)."""
        if not (ours and p.queue and not p.radio):
            return ()
        pos = state.get("position")
        if not (isinstance(pos, int) and 0 <= pos < len(p.queue)):
            return ()
        cur = p.queue[pos]
        for cap, key in (("media_title", "title"), ("media_artist", "artist"),
                         ("media_album", "album"), ("media_art", "art")):
            await self._set(p, cap, str(cur.get(key) or ""))
        return ("media_title", "media_artist", "media_album", "media_art")

    @staticmethod
    def _owned_caps(p: Player, on_station: bool, queue_owned: tuple[str, ...]) -> tuple[str, ...]:
        if p.radio:
            return ("media_title", "media_artist", "media_album", "media_art", "media_quality")
        if on_station:
            # Orphaned station (ICY re-arming, a poll away): hold the caps the ICY task
            # owns rather than let one poll of the device's own thinner echo through —
            # its title is unsplit ('Artist - Song'), and the format it reports for a
            # stream must not clobber the upstream's real codec/bitrate badge.
            return ("media_title", "media_quality")
        if p.cast_active:
            return _OWNED + queue_owned
        return queue_owned

    async def _publish_position(self, p: Player, seek, transport) -> None:
        """Position: publish SPARSELY (baseline + drift), mirroring cast/heos/dlna.
        The UI extrapolates the playhead from the last (position, monotonic) baseline
        while playing, so we only re-publish on a real jump (seek / track change) —
        not every ~2s poll. `seek` is milliseconds."""
        if seek is None:
            return
        try:
            pos = max(0, int(seek) // 1000)
        except (TypeError, ValueError):
            return
        mono = time.monotonic()
        expected: float | None = None
        if p.pos_base is not None and transport == "playing":
            base_pos, base_mono = p.pos_base
            expected = base_pos + (mono - base_mono)
        publish = (
            (expected is None and (p.last.get("media_position") != pos or transport == "playing"))
            or (expected is not None and abs(pos - expected) > 3)
        )
        if publish:
            p.pos_base = (pos, mono)
            await self._set(p, "media_position", pos)

    async def _radio_watchdog(self, p: Player) -> None:
        """Catch the silent radio wedge no layer above ever reports.

        When the stream's TCP dies without a FIN (a short WAN blip, the station
        dropping a long-lived listener), MPD keeps state=play with the playhead
        frozen and never reconnects; Volumio's getState mirrors the "play" and
        interpolates seek, so the adapter, the UI and the automations all see a
        healthy stream while the room sits silent (measured live 2026-07-19:
        elapsed frozen at 67:15 for 20+ min, every layer reporting playing — and
        the idempotent-play guard swallowed the user's own play press on top).
        MPD's `status` elapsed is the one truthful decode signal: frozen across
        _STALL_AFTER while claiming play ⇒ dead stream. Fail loud and re-cast —
        stop+replaceAndPlay reopens the connection, and firing again every
        _STALL_AFTER doubles as the retry loop while the internet is still down.

        A reopen that cannot connect at all (measured live 2026-07-30: the box
        lost DNS, so every re-cast died before the TCP handshake) parks MPD in
        "stop" — but Volumio's webradio service keeps claiming play, so every
        screen still lies and the frozen-elapsed check never fires (stop has no
        elapsed). That mismatch held for hours is the same silent wedge through
        a different door: MPD stopped for _STALL_AFTER while getState says
        playing ⇒ re-cast. MPD "pause" stays excluded — never auto-resume what
        might be a deliberate pause."""
        try:
            # Bounded: a HUNG MPD (2026-07-18 incident — accepts TCP, never answers,
            # mpc says 'error: Timeout') must blind the watchdog, not freeze this
            # poll loop on an unanswered readline forever.
            st = await asyncio.wait_for(self._mpd_status(p), 5.0)
        except Exception as exc:
            if not p.probe_warned:
                p.probe_warned = True
                log.warning("volumio %s: MPD liveness probe failed: %s — wedge watchdog blind until it recovers",
                            p.entity_id, exc, exc_info=True)
            return
        p.probe_warned = False
        mpd_state = st.get("state")
        if mpd_state == "stop":
            now = time.monotonic()
            if p.stopped_at is None:
                p.stopped_at = now
                return
            if now - p.stopped_at < _STALL_AFTER or now - p.recast_at < _STALL_AFTER:
                return
            p.stalled = True
            p.recast_at = now
            log.error("volumio %s: radio stream WEDGED — MPD stopped for %.0fs while Volumio "
                      "claims play (reopen never connected) — re-casting %s",
                      p.entity_id, now - p.stopped_at, (p.radio or {}).get("url"))
            await self._recast_radio(p)
            return
        p.stopped_at = None
        if mpd_state != "play":
            return  # a pause may be deliberate — never auto-resume it
        try:
            elapsed = float(st["elapsed"])
        except (KeyError, ValueError):
            return
        now = time.monotonic()
        if p.live_elapsed is None or elapsed != p.live_elapsed:
            if p.stalled:
                log.info("volumio %s: radio stream decoding again", p.entity_id)
            p.live_elapsed, p.live_at, p.stalled = elapsed, now, False
            return
        if now - p.live_at < _STALL_AFTER or now - p.recast_at < _STALL_AFTER:
            return
        p.stalled = True
        p.recast_at = now
        log.error("volumio %s: radio stream WEDGED — MPD claims play with elapsed frozen at %.0fs "
                  "for %.0fs (dead upstream connection) — re-casting %s",
                  p.entity_id, elapsed, now - p.live_at, (p.radio or {}).get("url"))
        await self._recast_radio(p)

    async def _set(self, p: Player, capability: str, value: object) -> None:
        """Publish a capability, de-duplicated against the last value we sent."""
        if p.last.get(capability) != value:
            p.last[capability] = value
            assert self._bus is not None
            await self._bus.publish_state(
                StateUpdate(
                    entity_id=p.entity_id,
                    capability=capability,
                    value=value,  # type: ignore[arg-type]
                    adapter=NAMESPACE,
                    ts_ns=time.time_ns(),
                    unit=_UNITS.get(capability),
                    name=p.name,
                    device=p.host,
                    device_name=p.name,
                )
            )

    async def _poll_soon(self, p: Player) -> None:
        await asyncio.sleep(0.6)
        with contextlib.suppress(Exception):
            await self._poll(p)

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        p = self._player
        if p is None:
            raise CommandRejected("player not configured")
        # Action buttons (press) — SSH-backed, grouped under the player.
        if command.capability == "press" and command.entity_id in (f"{p.entity_id}_restart", f"{p.entity_id}_reboot"):
            await self._run_action(p, command.entity_id.rsplit("_", 1)[1])
            return
        if command.entity_id != p.entity_id:
            raise CommandRejected("unknown volumio player")
        try:
            await self._dispatch(p, command.capability, command.command, dict(command.args))
        except Exception as exc:
            raise CommandRejected(f"player refused: {exc}") from exc
        spawn(self._poll_soon(p), log=log, name="post-command poll")

    async def _dispatch(self, p: Player, cap: str, cmd: str, args: dict) -> None:
        if cap == "media_transport":
            await self._transport(p, cmd, args)
        elif cap == "volume":
            await self._volume(cmd, args)
        elif cap == "mute" and cmd in ("mute", "unmute", "toggle"):
            await self._cmd("volume", volume=cmd)

    async def _transport(self, p: Player, cmd: str, args: dict) -> None:
        simple = {"next": "next", "previous": "prev"}
        if cmd in simple:
            await self._cmd(simple[cmd])
        elif cmd in ("play", "play_pause"):
            if p.radio and str(p.last.get("media_transport")) in ("paused", "stopped"):
                await self._recast_radio(p)  # a stream has no buffer to resume → re-cast
            else:
                await self._cmd("play" if cmd == "play" else "toggle")
        elif cmd == "pause":
            await self._cmd("pause")
            # Own the transport: Volumio's getState can keep reporting "playing" after a
            # pause, which then makes a follow-up play / play_media see "already playing"
            # and skip the resume re-cast (a stream has no buffer to resume). Setting
            # "paused" here lets those paths take the re-cast branch.
            await self._set(p, "media_transport", "paused")
        elif cmd == "stop":
            self._stop_icy(p)   # a user stop ends radio (else ICY keeps polling the upstream)
            p.cast_active = False
            p.queue = []
            await self._cmd("stop")
            await self._set(p, "media_transport", "stopped")
            await self._set(p, "media_queue", "")
        elif cmd == "play_media":
            await self._play_media(p, args)
        elif cmd == "play_queue":
            await self._play_queue(p, args)
        elif cmd == "play_index":
            idx = max(0, int(args.get("index") or 0))
            await self._cmd("play", N=idx)
            p.cast_active = bool(p.queue)
            await self._publish_queue(p, idx)

    async def _volume(self, cmd: str, args: dict) -> None:
        if cmd == "set_volume":
            await self._cmd("volume", volume=_vol(args.get("value")))
            return
        step = {"volume_up": _VOL_STEP, "volume_down": -_VOL_STEP}.get(cmd)
        if step is not None:
            cur = int(self._player.last.get("volume") or 0)
            await self._cmd("volume", volume=max(0, min(100, cur + step)))

    # --- casting -----------------------------------------------------------

    async def _play_media(self, p: Player, args: dict) -> None:
        """Play a single item: a library track, or internet radio.

        A station's OWN stream url is cast to the box — no relay. A relay (ffmpeg
        transcode → FLAC) once sat here because radio "stuttered" on this box, but
        the box plays every station's raw stream cleanly, first audio in 1-2s
        (measured). What actually stuttered was one station whose filename lies
        about its codec, which mpd_uri() now corrects.
        """
        url = str(args.get("uri") or args.get("url") or "")
        if not url:
            log.warning("play_media without a uri")
            return
        p.queue = []
        await self._set(p, "media_queue", "")
        upstream_radio = _is_upstream_radio(url, await opus_base(self.broker))
        log.info("volumio play_media %s (radio=%s)", url, upstream_radio)
        if upstream_radio:
            # Poll the UPSTREAM's ICY metadata for the live song. Mark p.radio BEFORE
            # the cast so a poll firing mid-swap yields the metadata caps to ICY.
            upstream = url
            radio = {"url": upstream, "station": str(args.get("title") or "")}
            p.radio = radio
            logo, quality, cast_url = await self._radio_cast(upstream)
            radio["logo"] = logo
            radio["quality"] = quality
            # Idempotent play: if the device is ALREADY playing this exact stream
            # (occupancy re-trigger, double play press), a re-cast would stop +
            # replaceAndPlay = an audible gap for nothing. Keep the stream and just
            # refresh the metadata ownership. A paused/stopped device falls through —
            # a stream has no buffer to resume from, the re-cast IS the fix.
            # "Playing" must be PROVEN, not read off getState: after an upstream
            # drop MPD keeps status=play with the playhead frozen, and that lie
            # once swallowed the user's own play press while the room sat silent
            # (2026-07-19). Live = the watchdog saw MPD's elapsed advance within
            # the last few polls; anything less re-casts.
            state = await self._get("getState")
            fresh = (p.live_elapsed is not None
                     and time.monotonic() - p.live_at < 3 * max(1, self._cfg.int("poll_seconds", 2)))
            if (fresh and state is not None and str(state.get("status")) == "play"
                    and station_url(str(state.get("uri") or "")) == upstream):
                log.info("volumio %s: already playing %s — skipping re-cast", p.entity_id, upstream)
                p.cast_active = True
                p.cast_title = str(args.get("title") or "")
                await self._set(p, "media_source", "radio")
                self._start_icy(p, radio)
                return
            await self._cast_one(p, {**args, "uri": cast_url}, radio=True)
            self._start_icy(p, radio)
        else:
            self._stop_icy(p)
            await self._cast_one(p, args)

    async def _recast_radio(self, p: Player) -> None:
        """Re-cast the current radio stream — the only way to resume it after a pause
        (MPD drops the stream buffer, so a bare 'play' can't restart it)."""
        r = p.radio
        if r:
            await self._play_media(p, {"uri": r.get("url"), "title": r.get("station")})

    async def _cast_one(self, p: Player, t: dict, *, radio: bool = False) -> None:
        """replaceAndPlay a single URL as a webradio item, then explicitly play
        (Volumio's webradio replaceAndPlay does not reliably auto-start). For a
        library track we publish optimistic now-playing (webradio echoes only the
        title); for radio the ICY task owns title/artist, we just mark the source.

        A station goes through this service rather than straight into MPD (as a queue
        must, see _mpd) for a reason worth keeping: the service is what OWNS Volumio's
        state. It hands the url to the same MPD either way — so it cannot colour the
        decode — but driving MPD behind its back leaves it reporting a frozen item and
        a phantom seek forever after (measured), and getState is what this adapter
        reads. A station is one endless item, exactly what the service handles well;
        it is a QUEUE it cannot hold.
        """
        url = str(t.get("uri") or t.get("url") or "")
        title = str(t.get("title") or "")
        art = str(t.get("art") or "")
        # Stop first: if MPD is paused (webradio can't resume from pause) a bare
        # replaceAndPlay+play stays stuck paused. A stop clears that reliably.
        await self._cmd("stop")
        await self._post("replaceAndPlay", {
            "item": {"service": "webradio", "type": "webradio", "uri": url, "title": title, "albumart": art},
        })
        await self._cmd("play")
        if radio:
            p.cast_active = True   # keep poll from clobbering the ICY-owned metadata
            p.cast_title = title   # station name; getState echoes the live song, not this
            await self._set(p, "media_source", "radio")
        else:
            await self._apply_meta(p, t)  # sets media_source from the cast URL

    async def _radio_cast(self, upstream: str) -> tuple[str | None, str, str]:
        """(logo, quality, cast_url) for a radio cast. Radio carries no per-track art
        → the station logo IS the cover; quality is the UPSTREAM's real codec+bitrate,
        which also tells MPD what to decode when the url's suffix lies (see mpd_uri)."""
        quality, ext = await radio_probe(upstream)
        station = await self._station_for(upstream)
        if station is None:
            self._stations_at = 0.0  # a just-added station: re-read, don't wait out the TTL
            station = await self._station_for(upstream)
        logo = (station or {}).get("logo") or None
        return logo, quality, mpd_uri(upstream, ext)

    async def _station_for(self, uri: str) -> dict | None:
        """The station whose stream url the box is playing, else None. We cast
        station urls straight to the device, so its uri IS the station's (bar our
        codec hint) — matched against a snapshot of the list the api answers on
        the bus, which it in turn holds from OPUS."""
        if not uri or self._bus is None:
            return None
        now = time.monotonic()
        if now - self._stations_at >= _STATIONS_TTL:
            rows = await radio_stations(self._bus)
            if rows is not None:  # None = nothing answered; keep the previous snapshot
                self._stations = {str(r["url"]): dict(r) for r in rows if r.get("url")}
                self._stations_at = now
        return self._stations.get(station_url(uri))

    async def _resume_radio(self, p: Player, station: dict) -> None:
        """Re-enter radio mode for a station that is already playing but lost its ICY
        poll (an announcement interrupted it). Guarded by p.radio_resuming so
        concurrent polls don't double-start."""
        try:
            quality, _ext = await radio_probe(str(station["url"]))
            if p.radio is not None:  # a real play_media re-entered radio meanwhile
                return
            radio = {"url": str(station["url"]), "station": str(station["name"] or ""),
                     "logo": station["logo"] or None, "quality": quality}
            p.radio = radio
            p.cast_active = True
            p.cast_title = radio["station"]
            log.info("volumio %s: radio resumed (%s) — restoring now-playing",
                     p.entity_id, radio["station"])
            self._start_icy(p, radio)
        finally:
            p.radio_resuming = False

    def _start_icy(self, p: Player, radio: dict) -> None:
        if p.icy_task is not None:
            p.icy_task.cancel()
        p.radio_idle = 0
        p.radio_switch = 0
        log.info("volumio %s: starting ICY poll for %r", p.entity_id, radio.get("station"))
        p.icy_task = spawn(self._icy_loop(p, radio), log=log, name="volumio icy")

    def _stop_icy(self, p: Player) -> None:
        task = p.icy_task
        p.icy_task = None
        p.radio = None
        if task is not None:
            task.cancel()

    async def _icy_loop(self, p: Player, radio: dict) -> None:
        """Poll the radio stream's ICY StreamTitle → publish artist/title; keep the
        station name as the 'album' line and its logo as the cover. The poll loop
        yields these caps to us (see _poll's `owned`) while p.radio is set."""
        url, station = str(radio.get("url") or ""), str(radio.get("station") or "")
        await self._set(p, "media_album", station)
        await self._set(p, "media_art", str(radio.get("logo") or ""))
        await self._set(p, "media_quality", str(radio.get("quality") or ""))  # upstream codec/bitrate
        await self._set(p, "media_title", station)   # until the first StreamTitle
        last = None
        warned = False
        while True:
            try:
                meta = await asyncio.to_thread(icy_stream_title, url)
                warned = False
                if meta and meta != last:
                    last = meta
                    log.info("icy %s: %s", p.entity_id, meta)
                    artist, sep, song = meta.partition(" - ")
                    if sep and song.strip():
                        await self._set(p, "media_artist", artist.strip())
                        await self._set(p, "media_title", song.strip())
                    else:
                        await self._set(p, "media_artist", "")
                        await self._set(p, "media_title", meta.strip())
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not warned:
                    log.warning("icy %s: %s (retrying)", p.entity_id, exc, exc_info=True)
                    warned = True
                await asyncio.sleep(30)

    async def _play_queue(self, p: Player, args: dict) -> None:
        """Play a list of tracks as a real queue — driven straight into MPD, not through
        Volumio's queue (which cannot hold ours; see _mpd)."""
        try:
            tracks = json.loads(str(args.get("queue") or "[]"))
        except (ValueError, TypeError):
            log.warning("volumio play_queue: undecodable queue JSON")
            return
        tracks = [t for t in tracks if isinstance(t, dict) and (t.get("uri") or t.get("url"))]
        if not tracks:
            return
        self._stop_icy(p)  # a queue is DIDA library content — leave radio mode (else
        #                    p.radio lingers: source stays 'radio' and a later play
        #                    after this queue would re-cast the old station).
        p.queue = tracks
        start = max(0, min(int(args.get("start") or 0), len(tracks) - 1))
        # Straight to MPD — see _mpd() for why Volumio's own queue cannot do this.
        uris = [str(t.get("uri") or t.get("url")) for t in tracks]
        # Bounded like the watchdog probe: a hung MPD (2026-07-18) must fail this
        # command loudly, not park the command task on a dead socket forever.
        await asyncio.wait_for(
            self._mpd(p, ["clear", *(f"add {self._mpd_arg(u)}" for u in uris), f"play {start}"]), 15.0)
        await self._apply_meta(p, tracks[start])
        await self._publish_queue(p, start)

    async def _publish_queue(self, p: Player, current: int) -> None:
        if p.queue:
            items = [{"title": str(t.get("title") or ""), "artist": str(t.get("artist") or ""),
                      "art": str(t.get("art") or "")} for t in p.queue]
            value = json.dumps({"items": items, "current": current}, separators=(",", ":"))
        else:
            value = ""
        await self._set(p, "media_queue", value)

    async def _apply_meta(self, p: Player, t: dict) -> None:
        """Publish optimistic now-playing for a DIDA cast (Volumio's webradio
        service echoes only the title, so we own artist/album/art/quality). Set
        every field UNCONDITIONALLY — a field the new track lacks is cleared, never
        left showing the previous source's value (e.g. radio artist/album bleeding
        onto a library track)."""
        p.cast_active = True
        p.cast_title = str(t.get("title") or "")
        for cap, key in (("media_title", "title"), ("media_artist", "artist"),
                         ("media_album", "album"), ("media_art", "art"),
                         ("media_quality", "quality")):
            await self._set(p, cap, str(t.get(key) or ""))
        # Classify the source from the cast URL (library / tidal / …) so the signal
        # path and source badge are right — a queue play must not inherit 'radio'.
        await self._set(p, "media_source", source_of(str(t.get("uri") or t.get("url") or ""), "webradio"))

    # --- SSH actions (reboot / restart audio + MPD buffer) ----------------

    async def _ssh_run(self, cmd: str, *, stdin: str | None = None) -> str:
        import asyncssh

        host = self._cfg.get("host")
        user = self._cfg.get("ssh_user") or "volumio"
        pw = self._cfg.get("ssh_password") or ""
        # known_hosts stays off deliberately: this is a fixed appliance on the LAN
        # whose host key is regenerated by a re-flash, and the only things reachable
        # over this channel are "reboot" and "restart MPD". Pinning would buy a
        # MITM-on-your-own-LAN defence at the price of a silently broken admin
        # action after every re-image.
        async with asyncssh.connect(host, username=user, password=pw, known_hosts=None) as conn:
            r = await conn.run(cmd, check=False, timeout=30, input=stdin)
            return ((r.stdout or "") + (r.stderr or "")).strip()

    async def _ssh_sudo(self, cmds: list[str]) -> str:
        """Run a batch under ONE sudo, with the password on stdin.

        It used to be `echo <pw> | sudo -S …` per command, which put the password
        in the appliance's own process list (visible to any `ps` on the box) — and
        needed the secret once per command. One sudo wrapping the whole script
        reads the password from stdin exactly once and never puts it in argv."""
        pw = self._cfg.get("ssh_password") or ""
        script = " && ".join(cmds)
        return await self._ssh_run(f"sudo -S -p '' sh -c {shlex.quote(script)}", stdin=f"{pw}\n")

    def _buffer_cmds(self) -> list[str]:
        """sed commands writing the configured MPD buffer into /etc/mpd.conf.
        An empty config field leaves that setting untouched. MPD parses plain
        whitespace, so we avoid literal tabs (sed wouldn't expand them)."""
        cmds: list[str] = []
        kb = self._cfg.int("audio_buffer_kb", 0)
        if kb > 0:
            cmds.append(f'sed -i \'s#^audio_buffer_size.*#audio_buffer_size "{kb}"#\' /etc/mpd.conf')
        pct = str(self._cfg.get("buffer_before_play") or "").rstrip("%").strip()
        if pct.isdigit():
            cmds.append(f'sed -i \'s#^buffer_before_play.*#buffer_before_play "{pct}%"#\' /etc/mpd.conf')
        return cmds

    async def _run_action(self, p: Player, action: str) -> None:
        if action == "reboot":
            log.info("volumio %s: reboot", p.entity_id)
            with contextlib.suppress(Exception):
                await self._ssh_sudo(["reboot"])
            return
        # restart audio: apply the buffer config (if any), then restart MPD.
        cmds = self._buffer_cmds()
        applied = bool(cmds)
        cmds.append("systemctl restart mpd")
        log.info("volumio %s: restart audio (buffer applied=%s)", p.entity_id, applied)
        try:
            out = await self._ssh_sudo(cmds)
            if out:
                log.info("volumio restart audio: %s", out[:200])
        except Exception as exc:
            raise CommandRejected(f"restart audio failed: {exc}") from exc

    async def stop(self) -> None:
        if self._player is not None:
            self._stop_icy(self._player)
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()


def _vol(value: object) -> int:
    """A 0..100 volume arg → clamped int for Volumio's volume command. A missing or
    non-numeric value is a malformed command — raise (fail loud, handle_command logs
    and aborts) rather than silently setting volume 0 and muting the room."""
    if value is None:
        raise ValueError("volume set_volume: missing value")
    try:
        return max(0, min(100, int(float(value))))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"volume set_volume: invalid value {value!r}") from exc


def _is_upstream_radio(url: str, base: str) -> bool:
    """True for an internet-radio cast URL (a station's own stream), i.e. any http(s)
    URL that is NOT one of OPUS's. Keyed on the OPUS base, NOT on a '/radio/' path
    segment — real station URLs contain '/radio/' too (e.g.
    stream.yammat.fm/radio/8000/…), which would misclassify them."""
    if not url.startswith("http"):
        return False
    return not (base and url.startswith(base))
