"""Android TV adapter — NVIDIA Shield & any Android TV / Google TV box, over ADB.

Uses `python-androidtv` — the library Home Assistant's (stronger) `androidtv`
integration uses — over ADB on port 5555 with "Network debugging" (developer
mode) enabled on the device. This is the strong control path (vs the lighter
Remote v2): it pulls the REAL installed-app list off the device, launches any
app, reports true play/paused state and ABSOLUTE volume, mute, and HDMI input.

Setup is a one-time RSA-key accept — no 6-digit pairing. On the first connect the
device shows an "Allow debugging?" prompt; the keypair is generated here and
stored encrypted (`_adb_key`), so reconnects are silent. State is polled (ADB has
no push); the app list is pulled on connect (the device's launchable apps, then
annotated by the user — rename / show-hide — never a generic seed).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import re
import time
from pathlib import Path

from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    InvalidTransportError,
    TcpTimeoutException,
)
from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    set_reachable,
)
from dida_core.broker import BrokerError
from home_core.tasks import spawn
from PIL import Image

from dida_adapter_androidtv.mapping import (
    KEYCODE_ENTER,
    LAUNCHER_QUERY,
    LEANBACK_QUERY,
    NAMESPACE,
    NAV_KEYS,
    TRANSPORT_METHODS,
    TRANSPORT_STATE,
    app_friendly,
    app_link,
    app_options,
    digit_keycodes,
    entity_id,
    is_real_app,
    merge_pulled,
    parse_apps,
    parse_launchable,
    parse_media_session,
)

log = logging.getLogger("dida.adapter.androidtv")

# Control plane (request/reply over NATS): the API drives a manual connect (so the
# user can trigger the "Allow debugging?" prompt when they're at the TV).
CTL_SUBJECT = "dida.androidtv.ctl"

# The ADB keypair is persisted encrypted in the DB; adb-shell wants file paths, so
# we mirror the stored blob into a private tmp dir.
_ADB_DIR = Path("/tmp/dida_androidtv")
_ADBKEY = str(_ADB_DIR / "adbkey")
_ADBKEY_PUB = str(_ADB_DIR / "adbkey.pub")

# Main media entity. volume/mute join the catalog once the device actually reports
# volume (some feed an AVR and report none → no phantom field).
_BASE_CAPS = ["on_off", "media_transport", "source", "source_options", "media_title",
              "media_artist", "media_album", "media_art", "media_position", "media_duration",
              "channel", "text"]
_VOLUME_CAPS = ["volume", "mute"]
_UNITS = {"volume": "%"}

# adb_shell closes its socket when a read times out, while python-androidtv keeps the
# object: every later call dies on the dead transport, so any of these ends the link.
_LINK_ERRORS = (TcpTimeoutException, AdbTimeoutError, AdbConnectionError, InvalidTransportError, OSError)
_SNAP_DIR = Path("/snap")


def _write_snapshot(slug: str, data: bytes) -> None:
    _SNAP_DIR.mkdir(exist_ok=True)
    (_SNAP_DIR / f"{slug}.jpg").write_bytes(data)


class AndroidTVAdapter:
    """Bridges one Android TV box onto the DIDA bus via python-androidtv (ADB)."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._atv = None                     # AndroidTVAsync
        self._entity: str | None = None
        self._name = "Shield"
        self._host = ""
        self._apps: list[dict] = []
        self._last: dict[tuple[str, str], object] = {}
        self._connected = False
        self._reach: bool | None = None  # last reachability we published (edge-trigger)
        self._has_volume = False
        self._snapping = False               # debounce fast post-command snapshots
        self._snap_hash: str | None = None   # last screencap JPEG digest (art dedupe)
        self._snap_v = 0                      # cache-buster bumped only when the image changes
        self._retry: asyncio.Event | None = None
        self._run_task: asyncio.Task | None = None

    # --- ADB keypair (encrypted, adapter-managed) --------------------------

    async def _load_key(self) -> dict | None:
        try:
            token = await self.broker.call("stored", key="_adb_key")
        except BrokerError as exc:
            log.error("androidtv: _adb_key unreadable (%s)", exc)
            return None
        if not token:
            return None
        try:
            data = json.loads(token)
        except (json.JSONDecodeError, TypeError):
            log.error("androidtv: _adb_key blob is not JSON")
            return None
        return data if isinstance(data, dict) and data.get("priv") and data.get("pub") else None

    async def _save_key(self, priv: str, pub: str) -> None:
        await self.broker.call("store", key="_adb_key", value=json.dumps({"priv": priv, "pub": pub}))

    def _write_key_files(self, priv: str, pub: str) -> None:
        _ADB_DIR.mkdir(parents=True, exist_ok=True)
        Path(_ADBKEY).write_text(priv)
        Path(_ADBKEY_PUB).write_text(pub)

    async def _prepare_key(self) -> None:
        """Ensure the adbkey files exist: from the stored blob, or freshly
        generated + persisted (so the device's 'always allow' stays valid)."""
        blob = await self._load_key()
        if blob:
            await asyncio.to_thread(self._write_key_files, blob["priv"], blob["pub"])
            return
        priv, pub = await asyncio.to_thread(self._new_key)
        await self._save_key(priv, pub)
        log.info("androidtv: generated a new ADB keypair")

    @staticmethod
    def _new_key() -> tuple[str, str]:
        from adb_shell.auth.keygen import keygen

        _ADB_DIR.mkdir(parents=True, exist_ok=True)
        keygen(_ADBKEY)  # writes adbkey + adbkey.pub
        return Path(_ADBKEY).read_text(), Path(_ADBKEY_PUB).read_text()

    # --- lifecycle ---------------------------------------------------------

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        self._apply_cfg()
        self._retry = asyncio.Event()
        spawn(self._cfg.poll_loop(), log=log, name="androidtv config poll")
        spawn(self._watch_apps(), log=log, name="androidtv apps watch")
        await bus.nc.subscribe(CTL_SUBJECT, cb=self._on_ctl)
        if self._host:
            self._run_task = spawn(self._run(), log=log, name="androidtv run")
        else:
            self.status.error("no host IP — Settings → Adapters")
            log.warning("androidtv: no host configured — idle until set")
        log.info("androidtv adapter up — host=%s ctl=%s", self._host or "(unset)", CTL_SUBJECT)
        await asyncio.Event().wait()

    def _apply_cfg(self) -> None:
        assert self._cfg is not None
        self._host = self._cfg.get("host").strip()
        self._name = self._cfg.get("name").strip() or "Shield"
        self._apps = parse_apps(self._cfg.get("apps"))
        self._entity = entity_id(self._name)

    async def _watch_apps(self) -> None:
        """Republish the launch dropdown when the user edits the app list (rename /
        show-hide) — the config poll keeps self._cfg fresh; deduped on value."""
        while True:
            await asyncio.sleep(10)
            if self._entity is None or not self._connected or self._cfg is None:
                continue
            apps = parse_apps(self._cfg.get("apps"))
            if apps != self._apps:
                self._apps = apps
                await self._publish(self._entity, "source_options", app_options(apps))

    # --- connect + poll loop -----------------------------------------------

    async def _run(self) -> None:
        assert self._retry is not None
        while True:
            try:
                if await self._connect_once():
                    await self._on_connected()
                    await self._poll_until_disconnect()
                    await self._close_atv()
                    delay = 5    # a live link dropped — the key is already accepted, reconnect is silent
                else:
                    delay = 30   # auth not completed yet — WAIT, don't spam the on-TV "Allow debugging?" prompt
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # _connect_once / _prepare_key do Postgres roundtrips — a transient DB
                # blip must NOT kill the loop (health would stay green with the adapter
                # dead). Log, surface, back off, and retry (mirrors the other supervise
                # loops: volumio/harmony/smartthings).
                log.exception("androidtv run loop error")
                self.status.error(str(exc) or "run loop error")
                await self._close_atv()
                delay = 30
            # Wait before retrying — or wake immediately when "Connect" is clicked.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._retry.wait(), timeout=delay)
            self._retry.clear()

    async def _connect_once(self) -> bool:
        from androidtv.androidtv.androidtv_async import AndroidTVAsync

        await self._cfg.load()  # honour a just-saved host without waiting for the poll
        self._apply_cfg()
        if not self._host:
            self.status.error("no host IP")
            return False
        await self._prepare_key()
        self.status.connecting("connecting — accept 'Allow debugging' on the TV")
        atv = AndroidTVAsync(self._host, 5555, adbkey=_ADBKEY)
        try:
            # A long auth window so ONE on-TV prompt stays valid while the user
            # walks over and accepts it (with "Always allow"), instead of timing
            # out at 15s and re-prompting on the next retry. auth_timeout_s governs
            # the auth read; the poll's own reads keep the normal (short) timeout.
            ok = await atv.adb_connect(auth_timeout_s=float(self._cfg.int("auth_timeout_s", 60)))
        except Exception as exc:
            log.info("androidtv adb_connect to %s failed: %s", self._host, exc, exc_info=True)
            ok = False
        if not ok:
            with contextlib.suppress(Exception):
                await atv.adb_close()
            self.status.error("not connected — enable Network debugging + accept the prompt on the TV")
            return False
        self._atv = atv
        self._connected = True
        await self._publish_reach(True)
        log.info("androidtv ADB connected — %s (%s)", self._name, self._host)
        return True

    async def _lose_link(self, detail: str) -> None:
        self._connected = False
        self.status.connecting(f"{detail} — reconnecting")
        await self._publish_reach(False, detail)

    async def _close_atv(self) -> None:
        self._connected = False
        atv, self._atv = self._atv, None
        if atv is not None:
            with contextlib.suppress(Exception):
                await atv.adb_close()

    async def _publish_reach(self, ok: bool, detail: str = "") -> None:
        """The ADB link's verdict, published on a CHANGE only. Keyed by the host —
        the same device key every entity of this TV hangs off."""
        if self._bus is None or not self._host or self._reach == ok:
            return
        self._reach = ok
        await set_reachable(self._bus, self._host, NAMESPACE, ok, detail="" if ok else detail)

    async def _on_connected(self) -> None:
        self.status.ok(self._name)
        await self._pull_apps()      # sets self._apps from the device
        await self._announce()       # publishes the entity + source_options
        with contextlib.suppress(Exception):
            await self._poll_once()  # seed state immediately

    async def _poll_until_disconnect(self) -> None:
        fails = 0
        while self._connected:
            try:
                await self._poll_once()
                fails = 0
                self.status.ok(self._name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fails += 1
                log.info("androidtv poll failed (%r)", exc, exc_info=True)
                if fails >= 2 or isinstance(exc, _LINK_ERRORS):
                    await self._lose_link("connection lost")
                    return
            await asyncio.sleep(max(2, self._cfg.int("poll_seconds", 10)))

    async def _poll_once(self) -> None:
        # update() -> (state, current_app, running_apps, audio_output_device,
        # is_volume_muted, volume_level, hdmi_input) — 7-tuple in python-androidtv.
        # lazy=False so current_app is fetched even during a screensaver (Immich's
        # ambient photo mode reads as a screensaver → lazy=True would drop it and
        # leave source/media_title stale).
        state, current_app, _running, _audio_out, is_muted, vol, _hdmi = await self._atv.update(
            get_running_apps=False, lazy=False
        )
        on = state is not None and state not in ("off", "standby")
        # python-androidtv's state/current_app are flaky on Android 11+ — correct them
        # with direct dumpsys probes (screen power + resumed activity) so on/off and
        # the foreground app don't silently come back wrong.
        screen = await self._probe_screen_on()
        if screen is not None:
            on = screen
        if on and not current_app:
            current_app = await self._probe_current_app()
        await self._publish(self._entity, "on_off", on)
        await self._publish(self._entity, "media_transport", TRANSPORT_STATE.get(state or "", "idle"))
        if on and current_app:
            await self._set_app(str(current_app))
            await self._pull_now_playing()
        else:
            # Screensaver / no foreground app (the Shield reports current_app=None
            # in ambient mode) — clear now-playing so a stale app label doesn't linger.
            for cap in ("source", "media_title", "media_artist", "media_album", "media_art", "text"):
                await self._publish(self._entity, cap, "")
        if vol is not None:
            if not self._has_volume:
                self._has_volume = True
                await self._announce()  # add volume/mute to the catalog now
            await self._publish(self._entity, "volume", max(0, min(100, round(float(vol) * 100))))
            if is_muted is not None:
                await self._publish(self._entity, "mute", bool(is_muted))

    async def _set_app(self, app_id: str) -> None:
        friendly = app_friendly(app_id, self._apps)
        await self._publish(self._entity, "text", app_id)
        await self._publish(self._entity, "media_title", friendly)
        await self._publish(self._entity, "source", friendly)

    async def _probe_screen_on(self) -> bool | None:
        """Screen/awake state straight from dumpsys power — more reliable than
        python-androidtv's `state` on Android 11+ (which can wrongly read 'off')."""
        raw = None
        with contextlib.suppress(Exception):
            raw = await self._atv.adb_shell("dumpsys power | grep -iE 'mWakefulness|Display Power|mScreenOn'")
        if not raw:
            return None
        low = raw.lower()
        if "awake" in low or "state=on" in low or "mscreenon=true" in low:
            return True
        if "asleep" in low or "dozing" in low or "state=off" in low or "mscreenon=false" in low:
            return False
        return None

    async def _probe_current_app(self) -> str | None:
        """Read the resumed activity's package straight from dumpsys — the fallback
        when python-androidtv returns current_app=None (Android 11+ format change)."""
        for query in (
            "dumpsys activity activities | grep -E 'ResumedActivity'",
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
        ):
            raw = None
            with contextlib.suppress(Exception):
                raw = await self._atv.adb_shell(query)
            if not raw:
                continue
            m = re.search(r"([a-z][a-z0-9_.]+\.[a-z0-9_]+)/", raw)
            if m:
                return m.group(1)
        return None

    async def _pull_now_playing(self) -> None:
        """Overlay the app label with the real title/artist/position from the active
        media session, when the foreground app publishes one (YouTube/Netflix/Plex/
        Jellyfin/Kodi…). Live-TV apps often publish none → keep the app label."""
        raw = None
        with contextlib.suppress(Exception):
            raw = await self._atv.adb_shell("dumpsys media_session")
        np = parse_media_session(raw or "")
        if np and np.get("title"):
            # The media session supplies its own art → skip the heavy per-poll ADB
            # screencap; only fall back to a snapshot when it publishes none.
            art = np.get("art") or await self._snapshot()
            await self._publish(self._entity, "media_title", np["title"])
            await self._publish(self._entity, "media_artist", np.get("artist") or "")
            await self._publish(self._entity, "media_album", np.get("album") or "")
            await self._publish(self._entity, "media_art", art)
            await self._publish(self._entity, "media_transport", np["transport"])
            await self._publish(self._entity, "media_position", np.get("position_s") or 0)
            await self._publish(self._entity, "media_duration", np.get("duration_s") or 0)
        else:
            # No media session → the live screen snapshot IS the feedback (e.g. live TV).
            snap = await self._snapshot()
            await self._publish(self._entity, "media_artist", "")
            await self._publish(self._entity, "media_album", "")
            await self._publish(self._entity, "media_art", snap)

    async def _snapshot(self) -> str:
        """Grab the current screen (ADB screencap), downscale to a light JPEG so the
        browser refresh is snappy (a 4K PNG is ~2 MB → this ~80 KB) → shared file → its
        API URL. DRM apps (Netflix/Disney+) return a black frame; live TV / YouTube /
        Plex show the real screen — the honest 'what's on' for apps with no metadata."""
        if self._atv is None or self._entity is None:
            return ""
        raw = b""
        with contextlib.suppress(Exception):
            raw = await self._atv.adb_screencap() or b""  # binary PNG (no base64 round-trip)
        if len(raw) < 128:
            return ""
        data = await asyncio.to_thread(self._downscale, raw)  # PIL is CPU-bound → off-loop
        if len(data) < 128:
            return ""
        # Dedupe by content: only bump ?v= (busting the browser cache) when the JPEG
        # actually changed, so a static screen doesn't churn media_art every poll
        # (~8.6k needless updates/day). An unchanged image returns the SAME URL, which
        # _publish then dedupes away.
        digest = hashlib.sha1(data).hexdigest()
        if digest == self._snap_hash:
            return f"/api/androidtv/snapshot/{self._entity}?v={self._snap_v}"
        slug = self._entity.split(":", 1)[-1]
        try:
            await asyncio.to_thread(_write_snapshot, slug, data)
        except OSError as exc:
            # Never hand out a URL to a file we failed to write (it would 404).
            log.warning("androidtv: snapshot write failed: %s", exc)
            return ""
        self._snap_hash = digest
        self._snap_v = time.time_ns()
        return f"/api/androidtv/snapshot/{self._entity}?v={self._snap_v}"

    @staticmethod
    def _downscale(raw: bytes) -> bytes:
        """Screencap PNG → ~960px JPEG."""
        try:
            img = Image.open(io.BytesIO(raw))
            img.thumbnail((960, 960))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=72)
            return buf.getvalue()
        except (OSError, ValueError):
            return b""

    async def _snap_now(self) -> None:
        """A quick, debounced snapshot right after a command — the screen reflects the
        button press without waiting for the next full poll."""
        if self._snapping:
            return
        self._snapping = True
        try:
            await asyncio.sleep(0.15)
            if self._connected:
                art = await self._snapshot()
                if art:
                    await self._publish(self._entity, "media_art", art)
        finally:
            self._snapping = False

    # --- app discovery (pull the device's launchable apps) -----------------

    async def _pull_apps(self) -> None:
        try:
            leanback = await self._atv.adb_shell(LEANBACK_QUERY) or ""
            launcher = await self._atv.adb_shell(LAUNCHER_QUERY) or ""
        except Exception as exc:
            log.warning("androidtv: app pull failed: %s", exc, exc_info=True)
            return
        # Filter BOTH lists — the leanback launcher itself lists Settings / Store /
        # vendor tools as home tiles, which aren't apps the user launches.
        pkgs = {p for p in parse_launchable(leanback) if is_real_app(p)}
        pkgs |= {p for p in parse_launchable(launcher) if is_real_app(p)}
        if not pkgs:
            log.warning("androidtv: no launchable apps found on device")
            return
        merged = merge_pulled(sorted(pkgs), parse_apps(self._cfg.get("apps")))
        await self.broker.call("store", key="apps", value=json.dumps(merged, ensure_ascii=False))
        await self._cfg.load()
        self._apps = merged
        log.info("androidtv: pulled %d launchable app(s) from %s", len(merged), self._host)

    # --- announce + publish ------------------------------------------------

    async def _announce(self) -> None:
        assert self._bus is not None and self._entity is not None
        caps = _BASE_CAPS + (_VOLUME_CAPS if self._has_volume else [])
        await self._bus.publish_entity(EntityInfo(
            entity_id=self._entity, adapter=NAMESPACE, name=self._name, device_type="media",
            device=self._host, device_name=self._name, capabilities=caps,
        ))
        for suffix, (_method, label) in NAV_KEYS.items():
            await self._bus.publish_entity(EntityInfo(
                entity_id=f"{self._entity}_{suffix}", adapter=NAMESPACE,
                name=f"{self._name} — {label}", device_type="button",
                device=self._host, device_name=self._name, capabilities=["press"],
            ))
        await self._publish(self._entity, "source_options", app_options(self._apps))

    async def _publish(self, eid: str | None, cap: str, value: object) -> None:
        if eid is None:
            return
        k = (eid, cap)
        if self._last.get(k) == value:
            return
        self._last[k] = value
        assert self._bus is not None
        await self._bus.publish_state(StateUpdate(
            entity_id=eid, capability=cap, value=value,  # type: ignore[arg-type]
            adapter=NAMESPACE, ts_ns=time.time_ns(), unit=_UNITS.get(cap),
            name=self._name, device=self._host, device_name=self._name,
        ))

    # --- commands ----------------------------------------------------------

    async def handle_command(self, command: Command) -> None:
        if self._atv is None or not self._connected:
            raise CommandRejected("not connected")
        eid, cap, cmd, args = command.entity_id, command.capability, command.command, dict(command.args)
        try:
            if cap == "press" and self._entity and eid.startswith(f"{self._entity}_"):
                nav = NAV_KEYS.get(eid[len(self._entity) + 1:])
                if nav:
                    await getattr(self._atv, nav[0])()
            elif eid == self._entity:
                await self._dispatch(cap, cmd, args)
            else:
                raise CommandRejected("unknown entity")
        except CommandRejected:
            raise
        except _LINK_ERRORS as exc:
            await self._lose_link("no answer over ADB")
            raise CommandRejected("no answer from the device — reconnecting") from exc
        except Exception as exc:
            raise CommandRejected(f"device refused: {exc}") from exc
        # Reflect the effect: a fast screen snapshot. A press (nav) only changes the
        # screen, so skip the heavier full poll — it would just double the screencap.
        spawn(self._snap_now(), log=log, name="androidtv snap")
        if cap != "press":
            spawn(self._poll_soon(), log=log, name="androidtv post-command poll")

    async def _dispatch(self, cap: str, cmd: str, args: dict) -> None:
        atv = self._atv
        if cap == "on_off":
            on = bool(self._last.get((self._entity, "on_off")))
            if cmd == "turn_on" or (cmd == "toggle" and not on):
                await atv.turn_on()
            elif cmd == "turn_off" or (cmd == "toggle" and on):
                await atv.turn_off()
        elif cap == "media_transport":
            method = TRANSPORT_METHODS.get(cmd)
            if method:
                await getattr(atv, method)()
        elif cap == "volume":
            if cmd == "set_volume":
                await atv.set_volume_level(max(0, min(100, int(args.get("value") or 0))) / 100)
            elif cmd == "volume_up":
                await atv.volume_up()
            elif cmd == "volume_down":
                await atv.volume_down()
        elif cap == "mute":
            muted = bool(self._last.get((self._entity, "mute")))
            if cmd == "toggle" or (cmd == "mute" and not muted) or (cmd == "unmute" and muted):
                await atv.mute_volume()
        elif cap == "source" and cmd == "set_source":
            link = app_link(str(args.get("value") or ""), self._apps)
            log.info("androidtv: set_source %r -> launch app %r", args.get("value"), link)
            if link:
                await self._launch(link)
        elif cap == "channel" and cmd == "set_channel":
            log.info("androidtv: set_channel -> tune %r", args.get("value"))
            await self._tune(str(args.get("value") or ""))

    async def _launch(self, package: str) -> None:
        """Launch an app. Android TV apps register LEANBACK_LAUNCHER (not LAUNCHER),
        so python-androidtv's launch_app (which uses LAUNCHER + monkey) silently
        no-ops for Netflix/Xplore/… — we wake the screen and monkey the leanback
        entry, falling back to the phone LAUNCHER category."""
        if self._atv is None:
            return
        # `package` is interpolated into an adb `monkey -p …` shell command; app_link
        # returns the raw source arg when it doesn't match a known app, so an
        # unvalidated value would be arbitrary shell on the TV. Only a real Android
        # package id is allowed — anything else fails loud.
        if not re.fullmatch(r"[A-Za-z0-9._]+", package):
            raise ValueError(f"androidtv: refusing to launch invalid package {package!r}")
        with contextlib.suppress(Exception):
            await self._atv.adb_shell("input keyevent 224")  # KEYCODE_WAKEUP
        for cat in ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER"):
            out = ""
            with contextlib.suppress(Exception):
                out = await self._atv.adb_shell(f"monkey -p {package} -c {cat} 1") or ""
            if "No activities found" not in out and "** Error" not in out:
                return

    async def _tune(self, number: str) -> None:
        """Type a channel number into the foreground TV app, then confirm — the app
        (Xplore etc.) jumps to that channel. There's no API into the app; this drives
        the on-screen number entry the same way the physical remote would."""
        codes = digit_keycodes(number)
        if not codes or self._atv is None:
            return
        for kc in codes:
            await self._atv.adb_shell(f"input keyevent {kc}")
            await asyncio.sleep(0.3)
        await self._atv.adb_shell(f"input keyevent {KEYCODE_ENTER}")
        # Remember what we tuned — DRM live-TV apps (Xplore) black out the screencap,
        # so the tuned channel is the only feedback the UI can show.
        await self._publish(self._entity, "channel", str(number))

    async def _poll_soon(self) -> None:
        await asyncio.sleep(0.8)
        if self._connected:
            with contextlib.suppress(Exception):
                await self._poll_once()

    # --- control plane -----------------------------------------------------

    async def _on_ctl(self, msg) -> None:
        try:
            req = json.loads(msg.data)
            action = req.get("action")
            if action == "status":
                assert self._cfg is not None
                await self._cfg.load()
                result = {
                    "connected": self._connected,
                    "host": self._cfg.get("host").strip(),
                    "apps": len(self._apps),
                }
            elif action == "connect":
                # Kick an immediate (re)connect so the user can accept the on-TV
                # prompt right now; the run loop does the work (one patient attempt).
                if self._retry is not None:
                    self._retry.set()
                result = {"ok": True}
            else:
                result = {"error": f"unknown action {action!r}"}
        except Exception as exc:
            log.warning("androidtv ctl failed: %s", exc, exc_info=True)
            result = {"error": str(exc)}
        if msg.reply:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._run_task is not None:
            self._run_task.cancel()
        await self._close_atv()
