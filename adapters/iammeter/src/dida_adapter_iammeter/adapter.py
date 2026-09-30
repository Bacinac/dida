from __future__ import annotations

import asyncio
import logging
import time

from dida_core import AdapterConfig, Bus, Command, EntityInfo, FailureGate, StateUpdate
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.iammeter")

NAMESPACE = "iammeter"
DEVICE = "meter"                 # entity-id segment: iammeter:meter:<facet>
DEVICE_GROUP = "iammeter:meter"  # UI grouping key — all facets render as one card
DEVICE_NAME = "Grid meter"

# IAMMETER `/monitorjson` → {"SN":…, "Data":[…]} on the single-phase WEM3080 and
# {"SN":…, "Datas":[[…], […], […]]} on the three-phase WEM3080T — one row per
# phase, same fixed order. Each element becomes its OWN single-capability entity so
# the UI shows the descriptive name (a multi-cap entity would collapse every row
# to the bare capability label — three "Napon"/"Struja" with no way to tell which).
# (suffix, capability, name, diagnostic, unit, row index)
CATALOG: list[tuple[str, str, str, bool, str | None, int]] = [
    ("voltage",       "voltage",      "Grid voltage",           False, None, 0),
    ("current",       "current",      "Grid current",          False, None, 1),
    # NET grid power: negative = exporting to the grid (solar surplus), positive =
    # importing. `power` has no minimum in the capability model, so the sign lands.
    ("power",         "power",        "Grid power (− export)", False, None, 2),
    ("import_energy", "energy",       "Imported energy",      False, None, 3),
    ("export_energy", "energy",       "Exported energy",     False, None, 4),
    ("frequency",     "frequency",    "Grid frequency",     True,  None, 5),
    ("power_factor",  "power_factor", "Power factor",          True,  None, 6),
]

# On a three-phase meter the CATALOG facets describe the WHOLE service, so the same
# entity ids mean the same thing at every location and an automation written against
# `iammeter:meter:power` is portable. Extensive quantities add up across the phases;
# the rest (voltage, frequency, power factor) are averaged.
SUMMED = {"current", "power", "import_energy", "export_energy"}

# Per-phase facets ride along as diagnostics. Energy is deliberately NOT among them:
# the energy dashboard groups counters by device and reads every `*_energy` leaf on a
# grid meter as import/export, so per-phase counters would double the house total.
PHASE_FACETS = {"voltage": "voltage", "current": "current",
                "power": "power", "power_factor": "power factor"}


def _num(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rows(data: object) -> list[list]:
    """Meter payload → one CATALOG-ordered row per phase, or [] if unrecognised."""
    if not isinstance(data, dict):
        return []
    single = data.get("Data")
    if isinstance(single, list) and len(single) >= 5:
        return [single]
    phases = data.get("Datas")
    if isinstance(phases, list) and phases and all(
        isinstance(r, list) and len(r) >= 5 for r in phases
    ):
        return list(phases)
    return []


def _mains() -> list[tuple[str, str, str, bool, str | None]]:
    return [(suffix, cap, name, diag, unit) for suffix, cap, name, diag, unit, _ in CATALOG]


def _facets(rows: list[list]) -> list[tuple[str, str, str, bool, str | None, float]]:
    """Rows → (suffix, capability, name, diagnostic, unit, value) for every reading."""
    out: list[tuple[str, str, str, bool, str | None, float]] = []
    for suffix, cap, name, diag, unit, idx in CATALOG:
        values = [v for v in (_num(r[idx]) if idx < len(r) else None for r in rows) if v is not None]
        if not values:
            continue
        out.append((suffix, cap, name, diag, unit,
                    sum(values) if suffix in SUMMED else sum(values) / len(values)))
    if len(rows) == 1:
        return out
    for n, row in enumerate(rows, 1):
        for suffix, cap, _name, _diag, unit, idx in CATALOG:
            label = PHASE_FACETS.get(suffix)
            if label is None or idx >= len(row):
                continue
            value = _num(row[idx])
            if value is not None:
                out.append((f"l{n}_{suffix}", cap, f"L{n} {label}", True, unit, value))
    return out


class IammeterAdapter:
    """Polls an IAMMETER WiFi grid meter's LOCAL JSON API (`/monitorjson`) and
    publishes its readings as canonical capabilities. Read-only; never writes to
    the meter, so it coexists with whatever else reads it (its own cloud/MQTT
    uploads keep working). Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None
        self._gate: FailureGate | None = None

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        self._gate = FailureGate(self.status, threshold=3)  # no single-blip badge flap
        await self._cfg.load()
        spawn(self._cfg.poll_loop(), log=log, name="iammeter config poll")  # live config edits
        log.info("iammeter adapter starting")
        await self._poll()

    async def _announce(self, facets, seen: set[str]) -> None:
        for suffix, cap, name, diag, *_rest in facets:
            if suffix in seen:
                continue
            seen.add(suffix)
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
            # The meter's CGI can mislabel the content-type — parse anyway.
            return await resp.json(content_type=None)

    async def _poll(self) -> None:
        backoff = 1
        seen: set[str] = set()
        while True:
            url = (self._cfg.get("url") if self._cfg else "").strip()
            if not url:
                self.status.idle("Meter URL not configured")
                await asyncio.sleep(10)
                continue
            if not seen:
                # Catalog before the first reading, so the card has names and a
                # header straight away — but only once a meter is configured. An
                # installation without one must not carry seven entities for it.
                # The per-phase facets follow the first reading: how many phases the
                # meter serves is only knowable from its answer.
                await self._announce(_mains(), seen)
            try:
                rows = _rows(await self._get(url))
                if rows:
                    facets = _facets(rows)
                    await self._announce(facets, seen)
                    self._emit(facets)
                    value = {f[0]: f[5] for f in facets}
                    phases = f"{len(rows)}φ · " if len(rows) > 1 else ""
                    self._gate.ok(  # type: ignore[union-attr]
                        f"{phases}{value.get('power', 0.0):.0f} W · "
                        f"import {value.get('import_energy', 0.0):.0f} · "
                        f"export {value.get('export_energy', 0.0):.0f} kWh"
                    )
                else:
                    self._gate.fail("unexpected response (no 'Data'/'Datas' field)")  # type: ignore[union-attr]
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._gate.fail(f"{exc}" if str(exc) else "meter unreachable")  # type: ignore[union-attr]
                backoff = min(backoff * 2, 60)
                log.warning("iammeter: poll failed: %s (retry in %ds)", exc, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(self._cfg.int("poll_seconds", 10) if self._cfg else 10)

    def _emit(self, facets) -> None:
        ts = time.time_ns()
        for suffix, cap, name, diag, unit, value in facets:
            entity_id = f"{NAMESPACE}:{DEVICE}:{suffix}"
            spawn(self._bus.publish_state(StateUpdate(  # type: ignore[union-attr]
                entity_id=entity_id, capability=cap, value=round(value, 3), adapter=NAMESPACE,
                ts_ns=ts, unit=unit, name=name, diagnostic=diag, device=DEVICE_GROUP,
            )), log=log, name=f"publish {entity_id}")

    async def handle_command(self, command: Command) -> None:
        return  # read-only sensor

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
