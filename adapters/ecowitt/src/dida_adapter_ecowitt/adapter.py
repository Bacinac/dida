from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from dida_core import AdapterConfig, Bus, Command, StateUpdate, set_reachable, slug
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.ecowitt")

NAMESPACE = "ecowitt"
_NUM = re.compile(r"[-+]?\d*\.?\d+")

# Ecowitt `common_list` id -> canonical capability (outdoor entity).
COMMON_CAPS: dict[str, str] = {
    "0x02": "temperature",      # outdoor temp
    "0x07": "humidity",          # outdoor humidity
    "0x15": "solar_radiation",   # W/m²
    "0x17": "uv_index",
    "0x0b": "wind_speed",        # m/s
    "0x0c": "wind_gust",         # m/s, peak of the current window
    "0x0a": "wind_direction",    # degrees from north
    "0x03": "dew_point",         # °C
}
# What the gateway also serves and this deliberately does NOT take: `3` (feels-like)
# and `5` (vapour pressure deficit) are arithmetic on temperature and humidity we
# already carry, `0x19` is a daily statistic, and `0x6D` is the ten-minute mean of a
# direction we now have live. A reading that can be computed from another is not a
# second measurement, and every one of them would be another row on the card.

# Push receiver — a console (EasyWeatherPro etc.) that has no local poll API
# uploads via the "Customized / Ecowitt" protocol. Point it at DIDA:4199/data/report.
PUSH_PORT = 4199
PUSH_PATH = "/data/report"
# A console that stops uploading looks EXACTLY like a console that is fine: there is
# no connection to lose, so nothing fails and nothing is logged — the entities simply
# stop moving. That is not hypothetical: the WS2900 here spent seven weeks pushing at
# an address that had gone away in a host move, and the only reason it surfaced was
# somebody reading a device list. The station uploads every 30 s, so ten missed ones
# is a station that is gone, not a slow one.
PUSH_STALE_S = 300


def _f_to_c(v: object) -> float:
    return round((float(v) - 32) * 5 / 9, 2)


def _inhg_to_hpa(v: object) -> float:
    return round(float(v) * 33.8639, 1)


def _mph_to_ms(v: object) -> float:
    return round(float(v) * 0.44704, 2)


def _in_to_mm(v: object) -> float:
    return round(float(v) * 25.4, 2)


# Ecowitt push field (imperial) -> (entity suffix, capability, converter to metric).
_PUSH_MAP: dict[str, tuple[str, str, object]] = {
    "tempf":          ("outdoor", "temperature", _f_to_c),
    "humidity":       ("outdoor", "humidity", float),
    "baromrelin":     ("outdoor", "pressure", _inhg_to_hpa),
    "windspeedmph":   ("outdoor", "wind_speed", _mph_to_ms),
    "solarradiation": ("outdoor", "solar_radiation", float),
    "uv":             ("outdoor", "uv_index", float),
    "rainratein":     ("outdoor", "rain_rate", _in_to_mm),
    "tempinf":        ("indoor", "temperature", _f_to_c),
    "humidityin":     ("indoor", "humidity", float),
}


def _num(val: object) -> float | None:
    m = _NUM.search(str(val))
    return float(m.group()) if m else None


class EcowittAdapter:
    """Polls one or more Ecowitt gateways' local JSON API and publishes their
    sensors as capabilities. Read-only; never reconfigures the gateway, so it
    coexists with the gateway's existing push targets. Implements
    `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._hosts: list[str] = []
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None
        self._push_runner = None
        # Per-source health (each gateway ip + "push"): the single badge is an
        # AGGREGATE — one dead gateway + one live one used to flap ok<->error.
        self._source_ok: dict[str, tuple[bool, str]] = {}
        # Push receiver is unauthenticated by protocol, so bound the station
        # namespace: when no `stations` allowlist is configured we pin the FIRST
        # station seen and reject the rest, so a rogue LAN host can't mint unbounded
        # ecowitt:<x>:* entities. `_rejected_warned` de-dupes the reject log.
        self._pinned_station = ""
        self._rejected_warned: set[str] = set()
        # When each station last said anything, and the verdict we last published
        # about it — edge-triggered, like every other adapter's.
        self._last_push: dict[str, float] = {}
        self._reach: dict[str, bool] = {}

    def _set_source(self, source: str, ok: bool, detail: str) -> None:
        self._source_ok[source] = (ok, detail)
        bad = [d for ok_, d in self._source_ok.values() if not ok_ for d in [d]]
        good = [d for ok_, d in self._source_ok.values() if ok_]
        if not bad:
            self.status.ok(" · ".join(good) or "ok")
        elif good:
            self.status.ok(f"{len(good)}/{len(self._source_ok)} sources · down: {'; '.join(bad)}")
        else:
            self.status.error("; ".join(bad))

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig("ecowitt", self.broker)
        await self._cfg.load()
        self._hosts = [h.strip() for h in self._cfg.get("hosts").split(",") if h.strip()]
        spawn(self._cfg.poll_loop(), log=log, name="ecowitt config poll")  # live config edits
        spawn(self._push_watchdog(), log=log, name="ecowitt push watchdog")
        # Push receiver runs regardless of poll hosts: console-only stations (no local
        # poll API) upload here; a gateway can use either mode. Both coexist.
        await self._start_push_server()
        if not self._hosts:
            self._set_source("push", True, f"push :{PUSH_PORT}")
            log.info("ecowitt: no poll gateways — push receiver only on :%d%s", PUSH_PORT, PUSH_PATH)
            await asyncio.Event().wait()
            return
        log.info("ecowitt adapter starting for %d gateway(s): %s", len(self._hosts), ", ".join(self._hosts))
        await asyncio.gather(*(self._poll_gateway(h) for h in self._hosts))

    async def _start_push_server(self) -> None:
        """HTTP receiver for the Ecowitt 'Customized' upload protocol — for consoles
        with no local poll API. Point the console at DIDA:PUSH_PORT+PUSH_PATH."""
        from aiohttp import web

        app = web.Application()
        app.router.add_post(PUSH_PATH, self._on_push)
        app.router.add_post("/", self._on_push)  # some consoles drop the path
        runner = web.AppRunner(app)
        self._push_runner = runner
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PUSH_PORT).start()
        log.info("ecowitt push receiver listening on :%d%s", PUSH_PORT, PUSH_PATH)

    async def _on_push(self, request):
        from aiohttp import web

        try:
            data = dict(await request.post())
        except Exception as exc:
            log.debug("ecowitt push: undecodable upload from %s: %s", request.remote, exc, exc_info=True)
            data = {}
        if data:
            self._emit_push(data)
        return web.Response(text="OK")  # the console expects a 200 or it retries

    def _push_allowed(self, station: str) -> bool:
        """Bound the push namespace so an unauthenticated LAN host can't mint
        arbitrary ecowitt:<x>:* entities. An explicit `stations` allowlist
        (comma-separated model slugs, Settings → Adapters) wins; otherwise pin the
        first station seen and reject any other — bounded to one without config."""
        raw = (self._cfg.get("stations") if self._cfg else "") or ""
        allow = {slug(s.strip()) for s in raw.split(",") if s.strip()}
        if allow:
            return station in allow
        if not self._pinned_station:
            self._pinned_station = station
            log.info("ecowitt push: pinned station %r (set `stations` in Settings to allow others)", station)
        return station == self._pinned_station

    def _emit_push(self, data: dict) -> None:
        # One physical station per upload; keyed by model so two stations stay
        # distinct cards (outdoor/indoor entities, same shape as the poll path).
        raw_model = str(data.get("model") or data.get("stationtype") or "console")
        # Strip a trailing firmware-version suffix (EasyWeatherPro_V5.2.1 → an OTA
        # bump would otherwise rename every entity and split its history).
        station = slug(re.sub(r"[_-]?V[\d.]+$", "", raw_model, flags=re.IGNORECASE) or raw_model)
        if not self._push_allowed(station):
            if station not in self._rejected_warned:
                self._rejected_warned.add(station)
                log.warning("ecowitt push: ignoring station %r — not in the allowed/pinned set "
                            "(add it to `stations` in Settings → Adapters)", station)
            return
        ts = time.time_ns()
        for field, (suffix, cap, conv) in _PUSH_MAP.items():
            raw = data.get(field)
            if raw in (None, ""):
                continue
            try:
                value = conv(raw)
            except (ValueError, TypeError):
                continue
            entity_id = f"{NAMESPACE}:{station}:{suffix}"
            spawn(self._bus.publish_state(StateUpdate(
                entity_id=entity_id, capability=cap, value=value, adapter=NAMESPACE,
                ts_ns=ts, name=f"{station.upper()} {suffix.title()}",
                # outdoor + indoor are facets of ONE station → one named card.
                device=f"{NAMESPACE}:{station}", device_name=station.upper(),
            )), log=log, name=f"publish {entity_id}")
        self._set_source("push", True, f"push {station}")
        self._last_push[station] = time.monotonic()
        spawn(self._reach_verdict(station, True), log=log, name=f"reach {station}")

    async def _reach_verdict(self, station: str, ok: bool, detail: str = "") -> None:
        """Publish whether this station is there, on the change only."""
        if self._bus is None or self._reach.get(station) == ok:
            return
        self._reach[station] = ok
        await set_reachable(self._bus, f"{NAMESPACE}:{station}", NAMESPACE, ok, detail=detail)

    async def _push_watchdog(self) -> None:
        """A pushing station has no connection to lose, so silence is the only signal
        it can give — and silence is exactly what nothing notices. This turns it into
        a verdict, which is what the device-unreachable alert reads.

        Only stations that have ALREADY pushed are watched: one that never has is not
        late, it is unconfigured, and inventing an alert for it would mean an alert on
        every installation that has no console."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for station, seen in list(self._last_push.items()):
                quiet = now - seen
                if quiet > PUSH_STALE_S:
                    await self._reach_verdict(
                        station, False, f"no upload for {int(quiet // 60)} min")

    async def _get(self, ip: str, path: str) -> object:
        import aiohttp

        async with self._session.get(  # type: ignore[union-attr]
            f"http://{ip}{path}", timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            return await resp.json(content_type=None)

    async def _poll_gateway(self, ip: str) -> None:
        # Identify the gateway BEFORE publishing anything: falling back to a
        # slug(ip) namespace on a boot-time blip permanently forked the entity
        # ids (duplicate cards, split history). Retry identification instead.
        gw: str | None = None
        self._set_source(ip, False, f"{ip} identifying")
        backoff = 1
        while gw is None:
            try:
                ver = await self._get(ip, "/get_version")
                text = ver.get("version", "") if isinstance(ver, dict) else ""
                m = re.search(r"([A-Za-z]+\d+[A-Za-z]?)", text)
                gw = slug(m.group(1)) if m else slug(text or "gateway")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ecowitt %s: identify failed: %s (retry in %ds)", ip, exc, backoff, exc_info=True)
                self._set_source(ip, False, f"{ip} unreachable")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        log.info("ecowitt %s identified as %s", ip, gw)
        backoff = 1
        while True:
            try:
                data = await self._get(ip, "/get_livedata_info")
                if isinstance(data, dict):
                    self._emit(gw, data)
                self._set_source(ip, True, gw.upper())
                await self._reach_verdict(gw, True)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_source(ip, False, f"{ip}: {exc}" if str(exc) else f"{ip} unreachable")
                await self._reach_verdict(gw, False, str(exc) or "no answer")
                log.warning("ecowitt %s: %s (retry in %ds)", ip, exc, backoff, exc_info=True)
                backoff = min(backoff * 2, 60)
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(self._cfg.int("poll_seconds", 30) if self._cfg else 30)

    def _emit(self, gw: str, data: dict) -> None:
        readings: list[tuple[str, str, float]] = []  # (entity_suffix, cap, value)

        for item in data.get("common_list", []):
            cap = COMMON_CAPS.get(str(item.get("id", "")).lower())
            if cap and (v := _num(item.get("val"))) is not None:
                readings.append(("outdoor", cap, v))

        # Rain rate (0x0e, mm/h) + daily total (0x10, mm) — prefer the piezo
        # (WS90) gauge, fall back to tipping. Daily total drives irrigation's
        # rain-skip (don't water if it already rained enough today).
        _RAIN_IDS = {"0x0e": "rain_rate", "0x10": "rain_daily"}
        for section in ("piezoRain", "rain"):
            for item in data.get(section, []):
                cap = _RAIN_IDS.get(str(item.get("id", "")).lower())
                if cap and (v := _num(item.get("val"))) is not None:
                    readings.append(("outdoor", cap, v))
            if any(r[1] == "rain_rate" for r in readings):
                break

        for wh25 in data.get("wh25", []):
            if (v := _num(wh25.get("intemp"))) is not None:
                readings.append(("indoor", "temperature", v))
            if (v := _num(wh25.get("inhumi"))) is not None:
                readings.append(("indoor", "humidity", v))
            if (v := _num(wh25.get("rel"))) is not None:
                readings.append(("indoor", "pressure", v))

        # Publish every reading each poll (no dedup): the volume is tiny at a
        # 30 s cadence, regular samples are what you want for weather history,
        # and it self-heals the boot race (a lost first publish lands next tick).
        for suffix, cap, value in readings:
            entity_id = f"{NAMESPACE}:{gw}:{suffix}"
            spawn(self._bus.publish_state(StateUpdate(
                entity_id=entity_id, capability=cap, value=value, adapter=NAMESPACE,
                ts_ns=time.time_ns(), name=f"{gw.upper()} {suffix.title()}",
                # outdoor + indoor are facets of ONE gateway → one named card.
                device=f"{NAMESPACE}:{gw}", device_name=gw.upper(),
            )), log=log, name=f"publish {entity_id}")

    async def handle_command(self, command: Command) -> None:
        return  # read-only sensors

    async def stop(self) -> None:
        if self._push_runner is not None:
            with contextlib.suppress(Exception):
                await self._push_runner.cleanup()
            self._push_runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None
