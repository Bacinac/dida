from __future__ import annotations

import asyncio
import logging
import time

from dida_core import AdapterConfig, Bus, Command, EntityInfo, FailureGate, StateUpdate
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.solar")

NAMESPACE = "solar"
DEVICE = "inverter"              # entity-id segment: solar:inverter:<facet>
DEVICE_GROUP = "solar:inverter"  # UI grouping key — all facets render as one card
DEVICE_NAME = "Solar inverter"
# A PV inverter powers itself OFF when there's no light, so an unreachable poll
# with the sun at/below the horizon is the nightly sleep — expected, not a fault.
# Read from astro:sun; a few degrees of margin covers deep twilight when the
# inverter is already asleep. Above it, an unreachable inverter is a real concern.
_SUN_ENTITY = "astro:sun"
_SUN_ASLEEP_DEG = 3.0

# The inverter's catalog. Each reading is its OWN single-capability entity so the
# UI shows its descriptive `name` (a multi-cap entity would collapse every row to
# the bare capability label — three "Napon"/"Struja" with no way to tell the grid
# from PV string 1 from PV string 2). (suffix, capability, name, diagnostic, unit)
CATALOG: list[tuple[str, str, str, bool, str | None]] = [
    ("power",          "power",        "Current power",          False, None),
    ("energy_today",   "energy",       "Energy today",       False, None),
    ("energy_total",   "energy",       "Total energy",      False, None),
    ("grid_voltage",   "voltage",      "Grid voltage",             False, None),
    ("grid_current",   "current",      "Grid current",            False, None),
    ("grid_frequency", "frequency",    "Grid frequency",       False, None),
    ("power_factor",   "power_factor", "Power factor",            False, None),
    ("pv1_voltage",    "voltage",      "String 1 voltage (PV1)",      False, None),
    ("pv1_current",    "current",      "String 1 current (PV1)",     False, None),
    ("pv2_voltage",    "voltage",      "String 2 voltage (PV2)",      False, None),
    ("pv2_current",    "current",      "String 2 current (PV2)",     False, None),
    ("temperature",    "temperature",  "Inverter temperature",  True,  None),
    ("runtime",        "measurement",  "Operating hours",               True,  "h"),
    ("status",         "text",         "Status",                  True,  None),
    ("apparent_power", "measurement",  "Apparent power",          True,  "VA"),
    ("reactive_power", "measurement",  "Reactive power",            True,  "var"),
]


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # The logger reports signed quantities (e.g. reactive power) as UNSIGNED 32-bit,
    # so a small negative wraps to ~4.29e9 (0xFFFFFFFE = -2). Unwrap the top half to
    # its real signed value; no genuine field here approaches 2^31.
    if 2**31 <= f < 2**32:
        f -= 2**32
    return f


def _arr(data: dict, key: str, i: int) -> float | None:
    """First-or-nth element of a per-phase/per-string array field (vac/iac/vpv/ipv)."""
    a = data.get(key)
    if isinstance(a, list) and len(a) > i:
        return _num(a[i])
    return None


class SolarAdapter:
    """Polls a PV inverter's datalogger local JSON API and publishes its readings
    as canonical capabilities. Read-only; never writes to the logger, so it
    coexists with the logger's existing cloud/HA uploads. Implements
    `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None
        self._gate: FailureGate | None = None  # daytime-unreachable badge (created in start)

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        self._gate = FailureGate(self.status, threshold=3)  # no single-blip badge flap
        await self._cfg.load()
        spawn(self._cfg.poll_loop(), log=log, name="solar config poll")  # live config edits
        log.info("solar adapter starting")
        await self._poll()

    async def _announce(self) -> None:
        for suffix, cap, name, diag, _unit in CATALOG:
            await self._bus.publish_entity(EntityInfo(  # type: ignore[union-attr]
                entity_id=f"{NAMESPACE}:{DEVICE}:{suffix}", adapter=NAMESPACE,
                capabilities=[cap], name=name, device=DEVICE_GROUP,
                device_name=DEVICE_NAME, diagnostic=diag,
            ))

    async def _get(self, url: str) -> object:
        import aiohttp

        async with self._session.get(  # type: ignore[union-attr]
            url, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            # The logger's CGI often mislabels the content-type (text/html) — parse anyway.
            return await resp.json(content_type=None)

    async def _sun_elevation(self) -> float | None:
        """Sun elevation from astro:sun (current_state), or None if unavailable —
        distinguishes 'inverter asleep, no sun' from a real daytime fault."""
        try:
            rows = await self.broker.call("state", entity_ids=[_SUN_ENTITY], capabilities=["sun_elevation"])
        except Exception as exc:
            log.debug("solar: sun-elevation lookup failed: %s", exc, exc_info=True)
            return None
        try:
            return float(rows[0]["value"]) if rows else None
        except (TypeError, ValueError):
            return None

    async def _poll(self) -> None:
        backoff = 1
        announced = False
        while True:
            url = (self._cfg.get("url") if self._cfg else "").strip()
            if not url:
                self.status.idle("Inverter URL not configured")
                await asyncio.sleep(10)
                continue
            if not announced:
                # Catalog before the first reading, so the card has names and a
                # header straight away — but only once an inverter is configured.
                # An installation without one must not carry sixteen entities for it.
                await self._announce()
                announced = True
            try:
                data = await self._get(url)
                if isinstance(data, dict):
                    self._emit(data)
                    pac = _num(data.get("pac")) or 0.0
                    etd = (_num(data.get("etd")) or 0.0) / 10
                    self._gate.ok(f"{pac:.0f} W · {etd:.1f} kWh today")  # type: ignore[union-attr]
                else:
                    self._gate.fail("unexpected logger response")  # type: ignore[union-attr]
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elev = await self._sun_elevation()
                if elev is not None and elev <= _SUN_ASLEEP_DEG:
                    # No sun — the inverter powers itself off. Expected nightly sleep,
                    # NOT a fault: show idle, don't count it toward the fault gate, and
                    # sleep the NORMAL interval — a 1s retry would hammer the dead
                    # datalogger and spew a warning line every second all night.
                    self.status.idle("inverter asleep (no sun)")
                    self._gate.reset()  # type: ignore[union-attr]
                    backoff = self._cfg.int("poll_seconds", 60) if self._cfg else 60
                    log.debug("solar: inverter asleep (elev=%s), next poll in %ds", elev, backoff, exc_info=True)
                else:
                    # Daytime (or sun state unknown): a genuinely unreachable inverter,
                    # but one blip must not flap the badge — error only after the streak.
                    self._gate.fail(f"{exc}" if str(exc) else "logger unreachable")  # type: ignore[union-attr]
                    backoff = min(backoff * 2, 60)
                    log.warning("solar: poll failed: %s (elev=%s, retry in %ds)", exc, elev, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(self._cfg.int("poll_seconds", 60) if self._cfg else 60)

    def _status_text(self, data: dict) -> str:
        err = _num(data.get("err")) or 0
        if err:
            return f"Error {int(err)}"
        return "Running" if _num(data.get("flg")) == 1 else "Idle"

    def _emit(self, data: dict) -> None:
        def scaled(key: str, div: float, ndigits: int) -> float | None:
            v = _num(data.get(key))
            return round(v / div, ndigits) if v is not None else None

        def scaled_arr(key: str, i: int, div: float, ndigits: int) -> float | None:
            v = _arr(data, key, i)
            return round(v / div, ndigits) if v is not None else None

        # The fixed-point logger fields, scaled to SI, keyed by catalog suffix.
        vals: dict[str, object] = {
            "power":          scaled("pac", 1, 1),      # W
            "energy_today":   scaled("etd", 10, 1),     # kWh today
            "energy_total":   scaled("eto", 10, 1),     # kWh lifetime
            "grid_voltage":   scaled_arr("vac", 0, 10, 1),
            "grid_current":   scaled_arr("iac", 0, 10, 2),
            "grid_frequency": scaled("fac", 100, 2),
            "power_factor":   scaled("pf", 100, 2),
            # PV string voltage is ÷10 (V) but string CURRENT is ÷100 (A): DC power
            # (V·A) then balances AC `pac` at ~95% efficiency (÷10 gave a 10× DC vs
            # AC mismatch). The logger scales the two differently.
            "pv1_voltage":    scaled_arr("vpv", 0, 10, 1),
            "pv1_current":    scaled_arr("ipv", 0, 100, 2),
            "pv2_voltage":    scaled_arr("vpv", 1, 10, 1),
            "pv2_current":    scaled_arr("ipv", 1, 100, 2),
            "temperature":    scaled("tmp", 10, 1),
            "runtime":        _num(data.get("hto")),    # operating hours
            "status":         self._status_text(data),
            "apparent_power": scaled("sac", 1, 1),      # VA
            "reactive_power": scaled("qac", 1, 1),      # var
        }

        ts = time.time_ns()
        for suffix, cap, name, diag, unit in CATALOG:
            value = vals.get(suffix)
            if value is None:
                continue
            entity_id = f"{NAMESPACE}:{DEVICE}:{suffix}"
            spawn(self._bus.publish_state(StateUpdate(  # type: ignore[union-attr]
                entity_id=entity_id, capability=cap, value=value, adapter=NAMESPACE,
                ts_ns=ts, unit=unit, name=name, diagnostic=diag, device=DEVICE_GROUP,
            )), log=log, name=f"publish {entity_id}")

    async def handle_command(self, command: Command) -> None:
        return  # read-only sensor

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
