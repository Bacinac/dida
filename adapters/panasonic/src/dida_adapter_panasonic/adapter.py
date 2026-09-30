from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    StateUpdate,
    validate_command,
)
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.panasonic")

NAMESPACE = "panasonic"


@dataclass(frozen=True)
class _Facet:
    """One switchable thing a unit carries next to its climate control."""

    name: str
    setter: str                       # ChangeRequestBuilder method
    attr: str                         # PanasonicDeviceParameters attribute
    unavailable: object = None        # the value that means "this unit lacks it"
    feature: str | None = None        # PanasonicDeviceFeatures flag that gates it
    category: str = "control"

    def supported(self, features, params) -> bool:
        if self.feature is not None and not getattr(features, self.feature, False):
            return False
        value = getattr(params, self.attr, None)
        if value is None:
            return False
        return not (self.unavailable is not None and value == self.unavailable)


@dataclass(frozen=True)
class _Enum(_Facet):
    values: dict = field(default_factory=dict)   # vendor constant -> canonical name

    @property
    def options(self) -> list[str]:
        return list(self.values.values())

    def read(self, params) -> str | None:
        return self.values.get(getattr(params, self.attr))

    def to_vendor(self, name: str):
        return {v: k for k, v in self.values.items()}[name]


@dataclass(frozen=True)
class _Toggle(_Facet):
    on: object = None
    off: object = None

    def read(self, params) -> bool:
        return getattr(params, self.attr) == self.on


class PanasonicAdapter:
    """Drives Panasonic air conditioners through Panasonic Comfort Cloud.

    These units carry Panasonic's own WiFi module, which answers nothing on the
    LAN — no open port, no discovery response — so the vendor cloud is the only
    control path there is. A poll loop reads each unit and publishes the climate
    capabilities; commands go out as a change request and are read back.

    Identity is the account's device id (a stable hash), never the unit's name,
    so renaming it in the Comfort Cloud app is display-only. Implements
    `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._cfg: AdapterConfig | None = None
        self._bus: Bus | None = None
        self._session = None            # aiohttp ClientSession
        self._client = None             # ApiClient
        self._active_key: tuple | None = None
        self._lock = asyncio.Lock()     # a command and a poll never share a call
        # Every entity we publish, back to the unit that owns it and the facet it
        # drives ("" = the climate control itself) — commands route through this.
        self._targets: dict[str, tuple[object, str]] = {}
        self._last: dict[tuple[str, str], object] = {}
        self._mode_to_str: dict = {}
        self._str_to_mode: dict = {}
        self._fan_to_str: dict = {}
        self._str_to_fan: dict = {}
        self._facets: dict[str, _Facet] = {}
        self._power = None
        self._zone_mode = None

    def _conn_key(self) -> tuple | None:
        """Comfort Cloud account from the DB, or None while it is unset."""
        email = (self._cfg.get("username") if self._cfg else "").strip()
        password = (self._cfg.get("password") if self._cfg else "").strip()
        if not (email and password):
            return None
        return (email, password)

    def _build_maps(self) -> None:
        from aio_panasonic_comfort_cloud import constants

        M = constants.OperationMode
        self._mode_to_str = {
            M.Auto: "auto", M.Dry: "dry", M.Cool: "cool", M.Heat: "heat", M.Fan: "fan_only",
        }
        self._str_to_mode = {v: k for k, v in self._mode_to_str.items()}
        # Five discrete speeds folded onto the canonical ladder in order, so
        # "higher" always means higher: the names differ from the app's, the
        # ranking does not.
        F = constants.FanSpeed
        self._fan_to_str = {
            F.Auto: "auto", F.Low: "silent", F.LowMid: "low",
            F.Mid: "medium", F.HighMid: "high", F.High: "max",
        }
        self._str_to_fan = {v: k for k, v in self._fan_to_str.items()}
        self._power = constants.Power
        self._zone_mode = constants.ZoneMode
        self._build_facets(constants)

    def _build_facets(self, constants) -> None:
        """Everything the unit carries beyond the climate control itself, each as
        its own entity on the same card. A facet is skipped for a unit whose
        feature flag says it lacks the hardware, or whose value reads
        `Unavailable` — the vocabulary is per-unit, never the protocol's."""
        S_UD, S_LR = constants.AirSwingUD, constants.AirSwingLR
        self._facets = {
            "eco": _Enum(
                name="Eco mode", setter="set_eco_mode", attr="eco_mode",
                values={constants.EcoMode.Auto: "auto", constants.EcoMode.Powerful: "powerful",
                        constants.EcoMode.Quiet: "quiet"},
            ),
            "nanoe": _Enum(
                name="nanoe", setter="set_nanoe_mode", attr="nanoe_mode",
                values={constants.NanoeMode.Off: "off", constants.NanoeMode.On: "on",
                        constants.NanoeMode.ModeG: "mode_g", constants.NanoeMode.All: "all"},
                unavailable=constants.NanoeMode.Unavailable, feature="nanoe",
            ),
            "swing_ud": _Enum(
                name="Swing (up/down)", setter="set_vertical_swing", attr="vertical_swing_mode",
                values={S_UD.Auto: "auto", S_UD.Up: "up", S_UD.UpMid: "up_mid", S_UD.Mid: "mid",
                        S_UD.DownMid: "down_mid", S_UD.Down: "down", S_UD.Swing: "swing"},
                feature="auto_swing_ud",
            ),
            "swing_lr": _Enum(
                name="Swing (left/right)", setter="set_horizontal_swing", attr="horizontal_swing_mode",
                values={S_LR.Auto: "auto", S_LR.Left: "left", S_LR.LeftMid: "left_mid",
                        S_LR.Mid: "mid", S_LR.RightMid: "right_mid", S_LR.Right: "right"},
                unavailable=S_LR.Unavailable, feature="air_swing_lr",
            ),
            "eco_navi": _Toggle(
                name="ECONAVI", setter="set_eco_navi_mode", attr="eco_navi_mode",
                on=constants.EcoNaviMode.On, off=constants.EcoNaviMode.Off,
                unavailable=constants.EcoNaviMode.Unavailable, feature="eco_navi",
                category="config",
            ),
            "eco_function": _Toggle(
                name="Eco function", setter="set_eco_function_mode", attr="eco_function_mode",
                on=constants.EcoFunctionMode.On, off=constants.EcoFunctionMode.Off,
                unavailable=constants.EcoFunctionMode.Unavailable, category="config",
            ),
            "iautox": _Toggle(
                name="iAUTO-X", setter="set_iautox_mode", attr="iautox_mode",
                on=constants.IAutoXMode.On, off=constants.IAutoXMode.Off,
                unavailable=constants.IAutoXMode.Unavailable, category="config",
            ),
        }

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._build_maps()
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        # Supervise: log in again whenever the UI changes the account, and treat
        # a failed cycle as a dead session — the next tick logs in from scratch.
        failures = 0
        while True:
            try:
                await self._cfg.load()
                key = self._conn_key()
                if key != self._active_key:
                    await self._connect(key)
                if self._client is not None:
                    async with self._lock:
                        await self._poll()
                    failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                blocked = self._blocked_reason(exc)
                log.warning("panasonic: cloud cycle failed (%d in a row): %s", failures, exc, exc_info=True)
                # Something only Ivo can clear (terms, 2FA, a wrong password) is
                # not a flaky cloud: say so on the FIRST failure instead of
                # looking merely slow for three minutes.
                if blocked:
                    self.status.error(blocked)
                elif failures >= 3:
                    self.status.error(str(exc) or "Comfort Cloud unreachable")
                await self._disconnect()
                self._active_key = None
            await asyncio.sleep(self._cfg.int("poll_seconds", 60) if self._cfg else 60)

    async def _connect(self, key: tuple | None) -> None:
        await self._disconnect()
        self._active_key = key
        if key is None:
            self.status.idle("no account configured")
            log.info("panasonic: no account — idle (set it in Settings → Adapters)")
            return
        import aiohttp
        from aio_panasonic_comfort_cloud import ApiClient

        email, password = key
        self.status.connecting(email)
        session = aiohttp.ClientSession()
        try:
            client = ApiClient(email, password, session)
            await client.start_session()
        except Exception:
            await session.close()
            raise
        self._session = session
        self._client = client
        units = len(client.get_devices())
        self.status.ok(f"{email} ({units} unit(s))")
        log.info("panasonic: signed in as %s — %d unit(s)", email, units)

    @staticmethod
    def _blocked_reason(exc: Exception) -> str | None:
        """A failure the account holder has to clear, worded as the action."""
        from aio_panasonic_comfort_cloud import exceptions as pexc

        if isinstance(exc, pexc.AgreementNotAcceptedError):
            pending = getattr(exc, "pending_types", None)
            return ("Panasonic updated its terms — open the Comfort Cloud app "
                    f"and accept them (pending: {pending or 'unspecified'})")
        if isinstance(exc, pexc.MFARequiredError):
            return "the account has 2FA on; this adapter signs in with e-mail + password only"
        if isinstance(exc, pexc.LoginError):
            return "Comfort Cloud rejected the e-mail or password"
        return None

    async def _disconnect(self) -> None:
        self._client = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def _poll(self) -> None:
        infos = self._client.get_devices()
        if not infos:
            # An account with no units is a configuration mistake, not a quiet
            # success: say so rather than sit green with nothing to show.
            self.status.error("account has no air conditioners")
            return
        for info in infos:
            device = await self._client.get_device(info)
            if device is None:
                continue
            self._publish(info, device)
        self.status.ok(f"{self._active_key[0]} ({len(infos)} unit(s))")

    def _hvac_mode(self, params) -> str:
        if params.power != self._power.On:
            return "off"
        return self._mode_to_str.get(params.mode, "auto")

    def _mode_options(self, info, features) -> list[str]:
        opts = ["off"]
        if info.auto_mode:
            opts.append("auto")
        if features.cool_mode:
            opts.append("cool")
        if features.heat_mode:
            opts.append("heat")
        if features.dry_mode:
            opts.append("dry")
        if features.fan_mode:
            opts.append("fan_only")
        return opts

    def _publish(self, info, device) -> None:
        eid = f"{NAMESPACE}:{info.id}"
        params = device.parameters
        readings: dict[str, object] = {
            "hvac_mode": self._hvac_mode(params),
            "hvac_mode_options": json.dumps(self._mode_options(info, device.features)),
            "fan_mode": self._fan_to_str.get(params.fan_speed, "auto"),
            "fan_mode_options": json.dumps(list(dict.fromkeys(self._fan_to_str.values()))),
        }
        if params.target_temperature is not None:
            readings["target_temperature"] = float(params.target_temperature)
        if params.inside_temperature is not None:
            readings["temperature"] = float(params.inside_temperature)
        self._targets[eid] = (info, "")
        for cap, value in readings.items():
            self._pub(eid, cap, value, name=info.name, device=eid, device_name=info.name)
        # The outdoor probe is a real sensor of its own, not a facet of the
        # climate control — its own entity, grouped on the same device card.
        if params.outside_temperature is not None:
            self._pub(f"{eid}:outside", "temperature", float(params.outside_temperature),
                      name=f"{info.name} outside", device=eid, device_name=info.name)
        if getattr(params, "air_quality", None) is not None:
            self._pub(f"{eid}:air_quality", "measurement", float(params.air_quality),
                      name=f"{info.name} air quality", device=eid, device_name=info.name,
                      category="diagnostic", diagnostic=True)
        self._publish_facets(info, device, eid)
        self._publish_zones(info, params, eid)

    def _publish_facets(self, info, device, eid: str) -> None:
        params, features = device.parameters, device.features
        for key, facet in self._facets.items():
            feid = f"{eid}:{key}"
            if not facet.supported(features, params):
                continue
            name = f"{info.name} {facet.name}"
            if isinstance(facet, _Enum):
                value = facet.read(params)
                if value is None:
                    # A vendor value we have no canonical name for: publishing a
                    # guess would let an automation compare against a lie.
                    log.warning("panasonic: %s reports an unmapped %s (%s)",
                                info.name, key, getattr(params, facet.attr, None))
                    continue
                self._pub(feid, "enum", value, name=name, device=eid,
                          device_name=info.name, category=facet.category)
                self._pub(feid, "enum_options", json.dumps(facet.options), name=name,
                          device=eid, device_name=info.name, category=facet.category)
            else:
                self._pub(feid, "boolean", facet.read(params), name=name, device=eid,
                          device_name=info.name, category=facet.category)
            self._targets[feid] = (info, key)

    def _publish_zones(self, info, params, eid: str) -> None:
        """Ducted units split into dampered zones; a single split has none."""
        for zone in getattr(params, "zones", None) or []:
            zname = zone.name or f"Zone {zone.id}"
            zeid = f"{eid}:zone:{zone.id}"
            self._pub(zeid, "boolean", zone.mode == self._zone_mode.On, name=zname,
                      device=eid, device_name=info.name)
            self._targets[zeid] = (info, f"zone:{zone.id}")
            self._pub(f"{zeid}:level", "number", float(zone.level), name=f"{zname} damper",
                      device=eid, device_name=info.name)
            self._pub(f"{zeid}:level", "number_options",
                      json.dumps({"min": 0, "max": 100, "unit": "%"}), name=f"{zname} damper",
                      device=eid, device_name=info.name)
            self._targets[f"{zeid}:level"] = (info, f"zone_level:{zone.id}")
            if zone.has_temperature and zone.temperature is not None:
                self._pub(f"{zeid}:temperature", "temperature", float(zone.temperature),
                          name=f"{zname} temperature", device=eid, device_name=info.name)

    def _pub(self, entity_id: str, cap: str, value, *, name: str, device: str,
             device_name: str, category: str = "control", diagnostic: bool = False) -> None:
        if self._last.get((entity_id, cap)) == value:
            return
        self._last[(entity_id, cap)] = value
        unit = "°C" if cap in ("temperature", "target_temperature") else None
        spawn(self._bus.publish_state(StateUpdate(
            entity_id=entity_id, capability=cap, value=value, adapter=NAMESPACE,
            ts_ns=time.time_ns(), unit=unit, name=name, device=device, device_name=device_name,
            category=category, diagnostic=diagnostic,
        )), log=log, name=f"publish {cap}")

    async def handle_command(self, command: Command) -> None:
        target = self._targets.get(command.entity_id)
        if target is None:
            raise CommandRejected("unknown AC")
        info, facet = target
        if self._client is None:
            raise CommandRejected("not signed in")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        from aio_panasonic_comfort_cloud import ChangeRequestBuilder

        try:
            async with self._lock:
                device = await self._client.get_device(info)
                if device is None:
                    raise CommandRejected("vanished from the account")
                builder = ChangeRequestBuilder(device)
                self._apply_command(builder, command, facet, device.parameters)
                if not builder.has_changes:
                    return
                await self._client.set_device_raw(device, builder.build())
                # The cloud reports the old values for a moment after a write;
                # reading back too soon would publish the pre-command state.
                await asyncio.sleep(3)
                device = await self._client.get_device(info)
            if device is not None:
                self._publish(info, device)
        except CommandRejected:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandRejected(f"cannot map {command.command}: {exc}") from exc
        except Exception as exc:
            raise CommandRejected(f"cloud refused: {exc}") from exc

    def _apply_command(self, builder, command: Command, facet: str, params) -> None:
        cap, args = command.capability, command.args
        if facet.startswith("zone"):
            self._apply_zone_command(builder, command, facet, params)
            return
        if facet:
            self._apply_facet_command(builder, command, self._facets[facet], params)
            return
        if cap == "hvac_mode":  # set_hvac_mode
            mode = str(args.get("value"))
            if mode == "off":
                builder.set_power_mode(self._power.Off)
            else:
                builder.set_power_mode(self._power.On)
                builder.set_hvac_mode(self._str_to_mode[mode])
        elif cap == "target_temperature":  # set_temperature
            builder.set_target_temperature(float(args.get("value")))
        elif cap == "fan_mode":  # set_fan_mode
            builder.set_fan_speed(self._str_to_fan[str(args.get("value"))])

    def _apply_facet_command(self, builder, command: Command, facet: _Facet, params) -> None:
        setter = getattr(builder, facet.setter)
        if isinstance(facet, _Enum):  # set_option
            setter(facet.to_vendor(str(command.args.get("value"))))
            return
        setter(facet.off if self._wants_off(command, facet.read(params)) else facet.on)

    def _apply_zone_command(self, builder, command: Command, facet: str, params) -> None:
        kind, _, raw_id = facet.partition(":")
        zone_id = int(raw_id)
        if kind == "zone_level":  # set_value
            builder.set_zone_damper(zone_id, int(float(command.args.get("value"))))
            return
        zone = params.get_zone(zone_id)
        on_now = zone.mode == self._zone_mode.On
        builder.set_zone_mode(
            zone_id, self._zone_mode.Off if self._wants_off(command, on_now) else self._zone_mode.On)

    @staticmethod
    def _wants_off(command: Command, on_now: bool) -> bool:
        """turn_on / turn_off / toggle → the state being asked for."""
        if command.command == "toggle":
            return on_now
        return command.command == "turn_off"

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.stop_session()
        await self._disconnect()
