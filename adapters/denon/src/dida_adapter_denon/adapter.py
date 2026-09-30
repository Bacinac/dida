"""Denon/Marantz AVR adapter — native Telnet control protocol (port 23).

This is the receiver's own control plane: power, master volume, mute and input
source, per zone (Main + Zone2). On a Denon/Marantz these actually take effect
(their UPnP RenderingControl is a no-op — so the DLNA adapter skips them and the
HEOS adapter only does transport/now-playing).

The Telnet socket is bidirectional and event-driven: the same connection that
sends commands also receives a frame whenever you touch the unit or the remote,
so we get live push state with no polling. Each zone is one DIDA entity; both
carry a shared `device` key (the receiver IP) so the UI groups them — together
with the HEOS player — into a single Marantz card.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
import time

from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    StateUpdate,
    local_ip,
    set_reachable,
    slug,
    ssdp_msearch,
)
from home_core.tasks import spawn

from dida_adapter_denon.protocol import (
    NAMESPACE,
    build_sources,
    parse_volume,
    pct_to_raw,
    vol_to_pct,
)

log = logging.getLogger("dida.adapter.denon")

ACT_DENON_ST = "urn:schemas-denon-com:device:ACT-Denon:1"
RECONNECT_SECONDS = 10.0
# Half-open detection: poll power every PROBE_SECONDS so a healthy-but-idle link
# keeps producing frames, and fail the read if nothing arrives within PROBE_DEADLINE
# (a half-open socket keeps a green badge while commands vanish into a dead writer).
PROBE_SECONDS = 30.0
PROBE_DEADLINE = 60.0
_UNITS = {"volume": "%"}

# Projector power runs over IR (unacknowledged), so we verify each transition
# against the receiver's HDMI output sync (SSINFSIGRES O): a resolution means a
# powered display on the active monitor, "---" means none. Measured on this
# Marantz + Acer X152H: after OFF the output sync drops by ~9s; after ON the
# projector warms up and syncs at ~36s. Wait past each before deciding a press
# missed, then retry. The whole loop overlaps the ~44s screen travel, so the
# long ON wait costs no extra wall-clock.
PROJ_ON_WAIT = 42.0
PROJ_OFF_WAIT = 14.0
PROJ_PRESS_GAP = 2.0
PROJ_ATTEMPTS = 3


def _discover_host(iface_ip: str, timeout: float = 3.0) -> str | None:
    # Only ACT-Denon devices answer this ST, so the first reply is our AVR.
    for sender_ip, data in ssdp_msearch(iface_ip, [ACT_DENON_ST], timeout, stop_on_first=True):
        m = re.search(rb"location:\s*https?://([0-9.]+)", data, re.IGNORECASE)
        return m.group(1).decode() if m else sender_ip
    return None


class Zone:
    """One AVR zone → one DIDA entity. Holds only the last published values."""

    def __init__(self, entity_id: str, name: str) -> None:
        self.entity_id = entity_id
        self.name = name
        self.vol_raw: float | None = None  # last raw master-volume value (0.0–98.0)
        self.last: dict[str, object] = {}


class DenonAdapter:
    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._host: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._reach: dict[str, bool] = {}  # host -> last reachability we published (edge-trigger)
        # iface_ip + zones (name-derived entity ids) are built in start() from
        # the merged config (Settings → Adapters).
        self._iface_ip = ""
        self._main: Zone | None = None
        self._zone2: Zone | None = None

        # Protocol state.
        self._mv_max = 98.0
        self._pw_on = True
        self._zm_on = True
        self._z2_on = True
        self._ssfun: dict[str, str] = {}
        self._sssod: dict[str, str] = {}
        self._label_to_code: dict[str, str] = {}
        self._code_to_label: dict[str, str] = {}
        self._source_options: list[str] = []

        # Verified projector power (see PROJ_* constants). _out_sig caches the
        # receiver's HDMI output sync on the active monitor (None = "---").
        self._out_sig: str | None = None
        self._proj_task: asyncio.Task | None = None
        self._proj_entity = "broadlink:projector_power"

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("denon", self.broker)
        await self._cfg.load()
        self._iface_ip = self._cfg.get("iface_ip") or await local_ip(self.broker)
        base = self._cfg.get("name") or "Marantz"
        dev_slug = slug(base, default="avr")
        self._main = Zone(f"{NAMESPACE}:{dev_slug}_main", base)
        self._zone2 = Zone(f"{NAMESPACE}:{dev_slug}_zone2", f"{base} Zone 2")
        spawn(self._cfg.poll_loop(), log=log, name="denon config poll")
        spawn(self._connect_loop(), log=log, name="denon connect loop")
        log.info("denon adapter up — iface=%s, name=%s", self._iface_ip, base)

    # --- connection --------------------------------------------------------

    async def _resolve_host(self) -> str | None:
        pinned = (self._cfg.get("host") if self._cfg else "").strip()
        if pinned:
            return pinned
        # SSDP search is a blocking socket loop (up to 3 s) — keep it OFF the
        # event loop or commands/status/health all freeze for that window.
        return await asyncio.to_thread(_discover_host, self._iface_ip)

    async def _connect_loop(self) -> None:
        while True:
            # Re-resolve every attempt: a UI host edit or a DHCP move must take
            # effect on the next reconnect, not never (the host used to be pinned
            # forever after the first successful connect).
            host = await self._resolve_host()
            if not host:
                log.warning("no Denon/Marantz device discovered yet; retrying")
                self.status.connecting("searching")
                await asyncio.sleep(RECONNECT_SECONDS)
                continue
            self._host = host
            try:
                await self._run_connection(host)
            except Exception:
                log.exception("denon connection to %s dropped", host)
            self._connected = False
            self._writer = None
            self._host = None  # force a fresh resolve on the next attempt
            self.status.error("connection lost")
            await self._publish_reach(host, False, "connection lost")
            await asyncio.sleep(RECONNECT_SECONDS)

    async def _publish_reach(self, host: str, ok: bool, detail: str = "") -> None:
        """The telnet link's verdict, edge-triggered, keyed by the host that groups
        both zones + the HEOS player into one device card."""
        if self._bus is None or not host or self._reach.get(host) == ok:
            return
        self._reach[host] = ok
        await set_reachable(self._bus, host, NAMESPACE, ok, detail="" if ok else detail)

    async def _run_connection(self, host: str) -> None:
        reader, writer = await asyncio.open_connection(host, 23)
        self._writer = writer
        self._connected = True
        self.status.ok(host)
        await self._publish_reach(host, True)
        log.info("connected Denon/Marantz @ %s:23", host)
        # SO_KEEPALIVE lets the OS notice a silently-dropped peer; the PW? probe +
        # read-deadline below catch the case KEEPALIVE is too slow / disabled.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        spawn(self._interrogate(), log=log, name="denon interrogate")
        probe = spawn(self._keepalive_probe(), log=log, name="denon keepalive probe")
        try:
            buf = b""
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=PROBE_DEADLINE)
                except TimeoutError as exc:
                    # No frame for PROBE_DEADLINE despite the periodic PW? probes → the
                    # socket is half-open (green badge, dead control plane). Fail loud;
                    # _connect_loop flips status.error and reconnects.
                    log.warning("denon: no data from %s for %ss — half-open, reconnecting",
                                host, PROBE_DEADLINE)
                    raise ConnectionError("read timeout (half-open)") from exc
                if not data:
                    log.info("denon connection closed by %s", host)
                    return
                buf += data
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    s = line.decode("ascii", "ignore").strip()
                    if s:
                        try:
                            await self._on_line(s)
                        except Exception:
                            log.debug("parse error on %r", s, exc_info=True)
        finally:
            probe.cancel()

    async def _keepalive_probe(self) -> None:
        """Poll power (PW?) periodically so an idle-but-healthy receiver keeps
        emitting frames — the read-deadline in _run_connection then fires only on a
        genuinely dead (half-open) socket, not on a merely quiet AVR."""
        while True:
            await asyncio.sleep(PROBE_SECONDS)
            with contextlib.suppress(Exception):
                await self._send("PW?")
                await self._send("SSINFSIGRES ?")  # refresh the video-output flag

    async def _send(self, cmd: str) -> None:
        w = self._writer
        if w is None:
            log.warning("denon: dropping %r — not connected", cmd)
            return
        w.write((cmd + "\r").encode("ascii"))
        await w.drain()

    async def _interrogate(self) -> None:
        """On (re)connect: pull the source table, then every zone's state. The
        receiver answers each as a normal frame, which _on_line publishes."""
        await self._send("SSFUN ?")
        await self._send("SSSOD ?")
        await asyncio.sleep(1.5)  # let the source frames accumulate
        self._rebuild_sources()
        for q in ("PW?", "ZM?", "MV?", "MU?", "SI?", "Z2?", "Z2MU?", "SSINFSIGRES ?"):
            await self._send(q)
            await asyncio.sleep(0.15)

    def _rebuild_sources(self) -> None:
        options, l2c, c2l = build_sources(self._ssfun, self._sssod)
        if not options:
            return
        self._source_options, self._label_to_code, self._code_to_label = options, l2c, c2l
        payload = json.dumps(options)
        for zone in (self._main, self._zone2):
            spawn(self._publish(zone, "source_options", payload), log=log, name="publish source_options")

    # --- inbound frame parsing --------------------------------------------

    async def _on_line(self, s: str) -> None:
        # Source rename / enabled tables (only during interrogation).
        if s.startswith("SSFUN"):
            self._table_entry(self._ssfun, s[5:])
        elif s.startswith("SSSOD"):
            self._table_entry(self._sssod, s[5:])
        elif s.startswith("SSINFSIGRES"):
            await self._on_signal(s[len("SSINFSIGRES"):].strip())
        elif s.startswith("Z2"):
            # Zone 2 (check before the Main MV/MU/SI prefixes can't collide; Z2*).
            await self._on_zone2(s[2:])
        elif s in ("PWON", "PWSTANDBY", "ZMON", "ZMOFF"):
            await self._on_power(s)
        else:
            await self._on_main(s)

    async def _on_signal(self, rest: str) -> None:
        """HDMI signal info: the OUTPUT sync on the active monitor drives verified
        projector power. "SSINFSIGRES O1080p:60Hz" = a powered display synced;
        "SSINFSIGRES O ---" = none. Ignore the "I" (input) frame."""
        if not rest.startswith("O"):
            return
        val = rest[1:].strip()
        self._out_sig = None if (not val or val.startswith("-")) else val
        # Surface the main zone's HDMI output as media_transport so the floor
        # plan can flag "video playing" in the living room: a synced display =
        # something is on screen; "---" = audio-only (or off).
        if self._main is not None:
            await self._publish(self._main, "media_transport",
                                "playing" if self._out_sig else "stopped")

    async def _on_power(self, s: str) -> None:
        """System power → gates Main power."""
        if s in ("PWON", "PWSTANDBY"):
            self._pw_on = s == "PWON"
        else:
            self._zm_on = s == "ZMON"
        await self._emit_main_power()
        if s == "PWSTANDBY":
            await self._emit_zone2_power()

    async def _on_main(self, s: str) -> None:
        if s.startswith("MVMAX"):
            mx = parse_volume(s[5:])
            if mx:
                # Kept ONLY to clamp outgoing set_volume to the unit's ceiling — the
                # displayed % scales against the fixed absolute max (see protocol.py),
                # so a Volume-Limit change no longer needs a republish.
                self._mv_max = mx
        elif s.startswith("MV"):
            raw = parse_volume(s[2:])
            if raw is not None:
                self._main.vol_raw = raw
                await self._publish(self._main, "volume", vol_to_pct(raw))
        elif s in ("MUON", "MUOFF"):
            await self._publish(self._main, "mute", s == "MUON")
        elif s.startswith("SI"):
            await self._publish(self._main, "source", self._label_of(s[2:]))

    async def _on_zone2(self, rest: str) -> None:
        if rest in ("ON", "OFF"):
            self._z2_on = rest == "ON"
            await self._emit_zone2_power()
        elif rest.startswith("MU"):
            await self._publish(self._zone2, "mute", rest == "MUON")
        elif rest[:1].isdigit():
            raw = parse_volume(rest)
            if raw is not None:
                self._zone2.vol_raw = raw
                await self._publish(self._zone2, "volume", vol_to_pct(raw))
        elif rest.startswith(("SLP", "PS", "HPF", "QUICK", "CS", "CV", "FAVORITE", "SOUND", "STBY")):
            return  # zone-2 extras/settings we don't model. "PS" (not just "PSV") so the
            #         tone frames Z2PSBAS/Z2PSTRE aren't mis-parsed as a source; STBY =
            #         auto-standby, also NOT a source. No real Zone2 source code starts "PS".
        else:
            await self._publish(self._zone2, "source", self._label_of(rest))

    @staticmethod
    def _table_entry(table: dict[str, str], rest: str) -> None:
        rest = rest.strip()
        if not rest or rest.upper().startswith("END"):
            return
        code, _, val = rest.partition(" ")
        if code:
            table[code] = val.strip()

    def _label_of(self, code: str) -> str:
        return self._code_to_label.get(code, code)

    async def _emit_main_power(self) -> None:
        await self._publish(self._main, "on_off", self._pw_on and self._zm_on)

    async def _emit_zone2_power(self) -> None:
        on = self._pw_on and self._z2_on
        await self._publish(self._zone2, "on_off", on)
        # Zone 2 (patio) is audio-only — its power stands in for "audio playing" so the
        # floor plan can flag the patio, mirroring the main zone's video flag.
        await self._publish(self._zone2, "media_transport", "playing" if on else "stopped")

    async def _publish(self, zone: Zone, capability: str, value: object) -> None:
        if zone.last.get(capability) == value:
            return
        zone.last[capability] = value
        assert self._bus is not None
        await self._bus.publish_state(
            StateUpdate(
                entity_id=zone.entity_id,
                capability=capability,
                value=value,  # type: ignore[arg-type]
                adapter=NAMESPACE,
                ts_ns=time.time_ns(),
                unit=_UNITS.get(capability),
                name=zone.name,
                device=self._host,  # group both zones + the HEOS player into one card
            )
        )

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        if self._main is None or self._zone2 is None:
            raise CommandRejected("not started")
        is_zone2 = command.entity_id == self._zone2.entity_id
        zone = self._zone2 if is_zone2 else self._main
        try:
            await self._dispatch(zone, is_zone2, command.capability, command.command, dict(command.args))
        except Exception as exc:
            raise CommandRejected(f"device refused: {exc}") from exc

    async def _dispatch(self, zone: Zone, z2: bool, cap: str, cmd: str, args: dict) -> None:
        if cap == "on_off":
            on = {"turn_on": True, "turn_off": False}.get(
                cmd, not bool(zone.last.get("on_off"))
            )
            await self._send(("Z2ON" if on else "Z2OFF") if z2 else ("ZMON" if on else "ZMOFF"))
        elif cap == "volume":
            if cmd == "set_volume":
                raw = pct_to_raw(int(args.get("value", 0)), self._mv_max)
                await self._send((f"Z2{raw:02d}") if z2 else (f"MV{raw:02d}"))
            elif cmd in ("volume_up", "volume_down"):
                up = cmd == "volume_up"
                await self._send(("Z2UP" if up else "Z2DOWN") if z2 else ("MVUP" if up else "MVDOWN"))
        elif cap == "mute":
            on = {"mute": True, "unmute": False}.get(cmd, not bool(zone.last.get("mute")))
            await self._send(("Z2MUON" if on else "Z2MUOFF") if z2 else ("MUON" if on else "MUOFF"))
        elif cap == "source" and cmd == "set_source":
            label = str(args.get("value", ""))
            code = self._label_to_code.get(label)
            if not code:
                # Reject an unknown label instead of shipping it verbatim as SI<label>
                # — an unmapped string reached the receiver as a raw command and could
                # trigger arbitrary behaviour. Fail loud, drop it.
                log.warning("denon: unknown source %r (known: %s)", label, self._source_options)
                return
            await self._send((f"Z2{code}") if z2 else (f"SI{code}"))
        elif cap == "video_select" and cmd == "set_video_select":
            # The receiver takes picture and sound off the same input, so listening
            # to an analogue source blanks whatever was on the screen. Video Select
            # breaks that pair: the ear stays on the chosen input, the eye is told
            # which input to keep showing. "off" gives the pair back.
            label = str(args.get("value", ""))
            if label.lower() == "off":
                await self._send("SVOFF")
                return
            code = self._label_to_code.get(label)
            if not code:
                log.warning("denon: unknown video source %r (known: %s)",
                            label, self._source_options)
                return
            await self._send(f"SV{code}")
        elif cap == "video_output" and cmd == "set_video_output":
            # HDMI output routing WITH verified projector power — the projector is on
            # IR (unacknowledged), so we confirm each transition against the receiver's
            # output HDMI sync and retry the press until it takes (see _projector_*).
            # "projector" = Monitor 2 (forced 1080p, its EDID is unreliable) + power ON;
            # "tv" = power the projector OFF, then Monitor 1 with AUTO so the receiver
            # outputs the TV's native 4K(60/50). Spawned so the ~40s verify loop runs
            # in the background (overlapping the screen travel) and never blocks command
            # handling.
            mode = str(args.get("value", ""))
            if mode == "projector":
                self._start_projector(self._projector_on())
            elif mode == "tv":
                self._start_projector(self._projector_off())

    # --- verified projector power -----------------------------------------

    async def _read_output_sync(self, settle: float = 2.0) -> str | None:
        """Query the receiver's signal info and return the OUTPUT sync on the active
        monitor (a resolution, or None for '---'). The reply lands as a frame in
        _on_line, so send then settle briefly before reading the cached value."""
        await self._send("SSINFSIGRES ?")
        await asyncio.sleep(settle)
        return self._out_sig

    async def _press_projector(self) -> None:
        if self._bus is None:
            return
        await self._bus.publish_command(Command(
            entity_id=self._proj_entity, capability="press", command="press",
            ts_ns=time.time_ns(), args={}, source="automation:projector-verify"))

    async def _projector_on(self) -> None:
        await self._send("VSMONI2")
        await self._send("VSSCH10P")
        for attempt in range(1, PROJ_ATTEMPTS + 1):
            await self._press_projector()
            log.info("denon: projector ON attempt %d/%d — waiting %.0fs for sync",
                     attempt, PROJ_ATTEMPTS, PROJ_ON_WAIT)
            await asyncio.sleep(PROJ_ON_WAIT)
            if await self._read_output_sync():
                log.info("denon: projector ON confirmed (output synced)")
                return
            log.warning("denon: projector ON attempt %d — no output sync, retrying", attempt)
        log.error("denon: projector ON failed after %d attempts (no output sync)", PROJ_ATTEMPTS)

    async def _projector_off(self) -> None:
        await self._send("VSMONI2")  # read the projector's HPD, not the TV's
        ok = False
        for attempt in range(1, PROJ_ATTEMPTS + 1):
            await self._press_projector()
            await asyncio.sleep(PROJ_PRESS_GAP)
            await self._press_projector()  # second press confirms the power-off prompt
            log.info("denon: projector OFF attempt %d/%d — waiting %.0fs for HPD drop",
                     attempt, PROJ_ATTEMPTS, PROJ_OFF_WAIT)
            await asyncio.sleep(PROJ_OFF_WAIT)
            if await self._read_output_sync() is None:
                log.info("denon: projector OFF confirmed (output ---)")
                ok = True
                break
            log.warning("denon: projector OFF attempt %d — still synced, retrying", attempt)
        if not ok:
            log.error("denon: projector OFF failed after %d attempts (still synced)", PROJ_ATTEMPTS)
        await self._send("VSMONI1")
        await self._send("VSSCHAUTO")
        # Big-screen teardown, powered off LAST — AFTER the projector-off verify, since a
        # powered-off amp drops HDMI sync and would else read as a false "projector off".
        # Tear down THROUGH Harmony so its activity state follows reality: the physical
        # Harmony remote and the living-room presence-audio automation both break when
        # Harmony still thinks an activity is running. PowerOff is known to leave the main
        # zone up, so the direct amp-off stays (all house audio runs through this amp, and
        # the presence-audio automation only starts music when it is off).
        if self._bus is not None:
            await self._bus.publish_command(Command(
                entity_id="harmony:hub", capability="source", command="set_source",
                args={"value": "PowerOff"}, ts_ns=time.time_ns(),
                source="automation:projector-verify"))
        await asyncio.sleep(0.3)
        await self._send("ZMOFF")

    def _start_projector(self, coro) -> None:
        # One projector transition at a time — a new command supersedes any in-flight
        # verify loop (e.g. a fast OFF right after an ON).
        if self._proj_task is not None and not self._proj_task.done():
            self._proj_task.cancel()
        self._proj_task = spawn(coro, log=log, name="denon projector power")

    async def stop(self) -> None:
        if self._proj_task is not None and not self._proj_task.done():
            self._proj_task.cancel()
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
