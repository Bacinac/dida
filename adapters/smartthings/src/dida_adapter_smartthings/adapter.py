from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import aiohttp
from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    StateUpdate,
    forget_reachable,
    set_reachable,
    validate_command,
)
from dida_core.broker import BrokerError
from home_core.tasks import spawn

from dida_adapter_smartthings.mapping import map_component, translate_command

log = logging.getLogger("dida.adapter.smartthings")

NAMESPACE = "smartthings"
TOKEN_URL = "https://auth-global.api.smartthings.com/oauth/token"
# How long an optimistic echo is held against a still-stale cloud poll before we
# give up and accept whatever the cloud reports (i.e. assume the command no-op'd).
_ECHO_GRACE_NS = 35_000_000_000


class ReauthNeeded(Exception):
    """The refresh token is dead/revoked — the user must reconnect (fail loud)."""


def _is_auth_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "auth" in name or "401" in msg or "unauthor" in msg or "invalid_token" in msg


def _type_hint(caps: set[str]) -> str | None:
    """Adapter-native device-type hint that SEEDS entities.device_type."""
    if "media_transport" in caps:
        return "media_player"
    if "on_off" in caps:
        return "light" if (caps & {"brightness", "color_temp"}) else "switch"
    if "lock" in caps:
        return "lock"
    if "open_close" in caps:
        return "cover"
    if caps & {"temperature", "humidity", "contact", "motion", "occupancy", "battery", "power", "energy", "smoke"}:
        return "sensor"
    return None


class SmartThingsAdapter:
    """Samsung appliances/AV via the SmartThings cloud (OAuth 2.0, rolling refresh).

    Cloud-only, like `landroid`: these devices have no local API. The one-time
    authorization runs in the `api` service; this adapter reads the encrypted
    token blob from `adapter_config`, keeps a valid access token by refreshing
    against the rolling refresh token (and persisting each new one), polls device
    status, and maps SmartThings' capability taxonomy onto DIDA's at the boundary.
    A dead refresh token fails LOUD (`status.error`, "reconnect") — never a silent
    degrade. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    # Consecutive get_locations() failures before we escalate from debug to a
    # visible warning (a persistent failure must not hide behind silent retries).
    _FAIL_THRESHOLD = 3

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self.broker = None
        self._cfg: AdapterConfig | None = None
        self._session: aiohttp.ClientSession | None = None
        self._st = None  # pysmartthings.SmartThings client
        self._client_id = ""
        self._client_secret = ""
        self._tokens: dict = {}                         # {access_token, refresh_token, expires_at}
        self._token_lock = asyncio.Lock()               # serialise refresh (rolling token!)
        self._labels: dict[str, str] = {}               # device_id -> label
        self._state: dict[str, dict] = {}               # entity_id -> {cap: value} (change detection)
        self._routes: dict[str, dict] = {}              # entity_id -> {device_id, component, routes}
        self._announced: dict[str, tuple] = {}          # entity_id -> catalog signature
        # entity_id -> {cap: (expected_value, expiry_ns)}: after an optimistic echo,
        # HOLD that value against a still-stale cloud poll until the cloud confirms
        # it (or the grace window lapses), so the switch doesn't flip back and forth.
        self._pending: dict[str, dict] = {}
        self._reach: dict[str, bool] = {}               # device_key -> last published reachability (edge-trigger)
        self._locations_synced = False                  # published the account's locations to app_settings?
        self._loc_fails = 0                              # consecutive get_locations() failures (surface a persistent one)

    # ── OAuth token lifecycle ────────────────────────────────────────────────

    async def _load_oauth(self) -> None:
        """Load the persisted OAuth blob WITHOUT clobbering a fresher in-memory token.

        This runs every tick. SmartThings rolls the refresh token on every use, so
        if `_ensure_token` refreshes + persists between this fetch and the assignment,
        blindly adopting the pre-roll DB blob would replay a used (single-use) refresh
        token → invalid_grant. So: hold `_token_lock` (serialising against the refresh),
        and adopt the DB blob ONLY when it carries a DIFFERENT refresh token AND we
        don't already hold a valid access token (adopt-only-if-newer). That still picks
        up a brand-new grant written out-of-band by the Connect flow, once our cached
        token lapses."""
        async with self._token_lock:
            try:
                raw = await self.broker.call("stored", key="_oauth")
                blob = json.loads(raw) if raw else None
            except (BrokerError, json.JSONDecodeError) as exc:
                # A rotated key makes the blob undecryptable — surface it, run empty.
                log.error("smartthings: _oauth blob unreadable (%s)", exc)
                blob = None
            if blob is None:
                if not self._tokens:
                    self._tokens = {}
                return
            if not isinstance(blob, dict):
                return
            if blob.get("refresh_token") == self._tokens.get("refresh_token"):
                return  # same grant already in memory — nothing newer to adopt
            have_valid = bool(self._tokens.get("access_token")) and time.time() < self._tokens.get("expires_at", 0)
            if have_valid:
                return  # our in-memory token is still valid — don't revert to the DB blob
            self._tokens = blob

    async def _save_oauth(self) -> None:
        await self.broker.call("store", key="_oauth", value=json.dumps(self._tokens))

    async def _oauth_refresh(self) -> str:
        """Exchange the refresh token for a fresh access token; persist the ROLLED
        refresh token. Returned to pysmartthings as its `refresh_token_function`,
        and called proactively before expiry. Raises `ReauthNeeded` if refused."""
        refresh = self._tokens.get("refresh_token")
        if not (refresh and self._client_id and self._client_secret):
            raise ReauthNeeded("no refresh token / client credentials")
        assert self._session is not None
        auth = aiohttp.BasicAuth(self._client_id, self._client_secret)
        data = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": self._client_id}
        async with self._session.post(
            TOKEN_URL, data=data, auth=auth, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            body = await r.text()
            if r.status != 200:
                if r.status in (400, 401):  # invalid_grant → refresh token is dead
                    raise ReauthNeeded(f"refresh rejected {r.status}: {body[:160]}")
                raise RuntimeError(f"token refresh {r.status}: {body[:160]}")
            tok = json.loads(body)
        self._tokens = {
            "access_token": tok["access_token"],
            # SmartThings rolls the refresh token on every use; keep the old one only
            # if a (non-conformant) response omits it.
            "refresh_token": tok.get("refresh_token", refresh),
            "expires_at": time.time() + int(tok.get("expires_in", 86400)) - 60,
        }
        await self._save_oauth()
        log.info("smartthings: access token refreshed (expires in %ss)", tok.get("expires_in"))
        return self._tokens["access_token"]

    async def _ensure_token(self) -> str:
        """A valid access token — refreshing only when the 24h token is near expiry.

        This is ALSO pysmartthings' `refresh_token_function`, which the library may
        invoke on every request, so the fast path MUST be cheap: return the cached
        token without touching the network. The lock serialises the rare real
        refresh so concurrent in-flight requests can't roll the (single-use,
        rolling) refresh token twice and invalidate the grant."""
        if self._tokens.get("access_token") and time.time() < self._tokens.get("expires_at", 0):
            return self._tokens["access_token"]
        async with self._token_lock:
            if self._tokens.get("access_token") and time.time() < self._tokens.get("expires_at", 0):
                return self._tokens["access_token"]
            return await self._oauth_refresh()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        self._session = aiohttp.ClientSession()
        while True:
            try:
                await self._cfg.load()
                self._client_id = self._cfg.get("client_id").strip()
                self._client_secret = self._cfg.get("client_secret").strip()
                await self._load_oauth()
                if not (self._client_id and self._client_secret and self._tokens.get("refresh_token")):
                    self._st = None
                    self.status.idle("Connect SmartThings in Settings → Adapters")
                else:
                    token = await self._ensure_token()
                    if self._st is None:
                        from pysmartthings import SmartThings
                        # refresh_token_function = the CHEAP _ensure_token (returns the
                        # cached token, refreshing only near expiry). pysmartthings
                        # calls it per request; a function that always did a full OAuth
                        # exchange would roll the single-use refresh token several times
                        # per cycle and kill the grant — the bug this replaces.
                        self._st = SmartThings(session=self._session, refresh_token_function=self._ensure_token)
                    self._st.authenticate(token)
                    if not self._locations_synced:
                        await self._publish_locations()
                    n = await self._poll_once()
                    self.status.ok(f"{n} devices" if n else "connected")
            except asyncio.CancelledError:
                raise
            except ReauthNeeded as exc:
                self._st = None
                self._locations_synced = False
                self.status.error("SmartThings disconnected — reconnect")
                log.warning("smartthings: %s", exc)
            except Exception as exc:
                # An auth failure from any request = a dead/revoked grant → say
                # "reconnect", not the opaque library message.
                if _is_auth_error(exc):
                    self._st = None
                    self._locations_synced = False
                    self.status.error("SmartThings disconnected — reconnect")
                else:
                    self.status.error(str(exc) or "cloud error")
                log.warning("smartthings: poll error: %s", exc, exc_info=True)
            await asyncio.sleep(self._cfg.int("poll_seconds", 30) if self._cfg else 30)

    # ── polling / projection ─────────────────────────────────────────────────

    async def _publish_locations(self) -> None:
        """Fetch the account's locations and stash [{id, name}] in app_settings so the
        UI can offer a home-location picker. A ST account can span several homes; the
        picker lets the user scope DIDA to one (see the `location` config filter)."""
        try:
            locs = await self._st.get_locations()
        except Exception as exc:
            self._loc_fails += 1
            # Retried silently every tick otherwise, so a persistent failure (e.g. a
            # grant lacking the locations scope) would never surface — the
            # home-location picker just stays empty. Log loud once the streak shows
            # it's no longer a transient blip.
            if self._loc_fails == self._FAIL_THRESHOLD:
                log.warning("smartthings: get_locations still failing after %d tries: %s "
                            "(home-location picker unavailable)", self._loc_fails, exc, exc_info=True)
            else:
                log.debug("smartthings: get_locations failed (%d): %s", self._loc_fails, exc, exc_info=True)
            return
        self._loc_fails = 0
        payload = json.dumps([{"id": loc.location_id, "name": loc.name} for loc in locs])
        await self.broker.call("set_setting", key="smartthings_locations", value=payload)
        self._locations_synced = True

    async def _poll_once(self) -> int:
        # get_raw_devices() returns a LIST OF PAGES (each {"items":[…], "_links":…}),
        # not a flat device list — flatten the items out of every page.
        raw = await self._st.get_raw_devices()
        pages = raw if isinstance(raw, list) else [raw]
        devices: list = []
        for page in pages:
            if isinstance(page, dict):
                devices.extend(page.get("items") or [])
            elif isinstance(page, list):
                devices.extend(page)
        # Home-location gate: a ST account can span multiple homes; ingest only the
        # chosen location's devices so a TV at another house never enters DIDA.
        home = self._cfg.get("location").strip() if self._cfg else ""
        seen: set[str] = set()
        polled: set[str] = set()
        count = 0
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            if home and dev.get("locationId") != home:
                continue
            device_id = dev.get("deviceId")
            if not device_id:
                continue
            label = str(dev.get("label") or dev.get("name") or device_id).strip()
            self._labels[device_id] = label
            try:
                entities = await self._refresh_device(device_id)
            except ReauthNeeded:
                raise
            except Exception as exc:
                if _is_auth_error(exc):
                    raise ReauthNeeded(str(exc)) from exc
                log.debug("smartthings: status failed for %s: %s", label, exc)
                continue
            seen.update(entities)
            polled.add(f"{NAMESPACE}:{device_id}")
            count += 1
            await self._refresh_health(device_id)
        # Drop caches for entities that disappeared (device removed from the account).
        for key in [k for k in self._reach if k not in polled]:
            self._reach.pop(key)
            forget_reachable(key, NAMESPACE)
        self._routes = {k: v for k, v in self._routes.items() if k in seen}
        self._state = {k: v for k, v in self._state.items() if k in seen}
        self._pending = {k: v for k, v in self._pending.items() if k in seen}
        return count

    async def _refresh_health(self, device_id: str) -> None:
        # The status endpoint keeps serving a device's last values after it drops off
        # SmartThings (a TV unlinked from the cloud reads "switch: on" for weeks), so
        # the health endpoint is the only source that says whether those values are live.
        try:
            health = await self._st.get_device_health(device_id)
        except Exception as exc:
            if _is_auth_error(exc):
                raise ReauthNeeded(str(exc)) from exc
            log.debug("smartthings: health failed for %s: %s", self._labels.get(device_id, device_id), exc)
            return
        state = str(getattr(health.state, "value", health.state))
        ok = state == "ONLINE"
        key = f"{NAMESPACE}:{device_id}"
        if self._reach.get(key) == ok:
            return
        self._reach[key] = ok
        await set_reachable(self._bus, key, NAMESPACE, ok,
                            detail=f"SmartThings reports the device {state.lower()}")

    async def _refresh_device(self, device_id: str) -> set[str]:
        """Fetch one device's status and project every component onto DIDA. Returns
        the entity ids it owns."""
        label = self._labels.get(device_id, device_id)
        status = await self._st.get_raw_device_status(device_id)
        comps = status.get("components", status) if isinstance(status, dict) else {}
        owned: set[str] = set()
        for comp_id, comp in comps.items():
            if not isinstance(comp, dict):
                continue
            readings, routes = map_component(comp)
            if not readings and not routes:
                continue
            entity_id = f"{NAMESPACE}:{device_id}" if comp_id == "main" else f"{NAMESPACE}:{device_id}:{comp_id}"
            name = label if comp_id == "main" else f"{label} · {comp_id}"
            await self._project(entity_id, device_id, comp_id, label, name, readings, routes)
            owned.add(entity_id)
        return owned

    async def _project(self, entity_id, device_id, comp_id, label, name, readings, routes) -> None:
        caps = [r.cap for r in readings]
        device_key = f"{NAMESPACE}:{device_id}"
        sig = (tuple(caps), name)
        if self._announced.get(entity_id) != sig:
            await self._bus.publish_entity(EntityInfo(
                entity_id=entity_id, adapter=NAMESPACE, capabilities=caps, name=name,
                device=device_key, device_name=label, device_type=_type_hint(set(caps)),
            ))
            self._announced[entity_id] = sig
        prev = self._state.get(entity_id, {})
        pending = self._pending.get(entity_id, {})
        newst: dict = {}
        now = time.time_ns()
        for r in readings:
            val = r.value
            p = pending.get(r.cap)
            if p is not None:
                exp_val, expiry = p
                if val == exp_val:
                    pending.pop(r.cap, None)     # cloud caught up → optimistic confirmed
                elif now < expiry:
                    newst[r.cap] = exp_val        # cloud still lagging → hold the echo, ignore stale
                    continue
                else:
                    pending.pop(r.cap, None)      # grace lapsed → the command really didn't take; accept reality
            newst[r.cap] = val
            if prev.get(r.cap) != val:  # publish only real changes (history-friendly)
                await self._bus.publish_state(StateUpdate(
                    entity_id=entity_id, capability=r.cap, value=val, adapter=NAMESPACE,
                    ts_ns=time.time_ns(), unit=r.unit, name=name, device=device_key,
                    device_name=label, category=r.category, diagnostic=r.diagnostic,
                ))
        self._state[entity_id] = newst
        self._routes[entity_id] = {"device_id": device_id, "component": comp_id, "routes": routes,
                                   "name": name, "label": label, "device_key": device_key}

    # ── commands ─────────────────────────────────────────────────────────────

    def _resolve(self, entity_id: str, cap: str, command: str) -> str:
        """Resolve toggles to a concrete command using the last known state."""
        st = self._state.get(entity_id, {})
        if cap == "on_off" and command == "toggle":
            return "turn_off" if st.get("on_off") else "turn_on"
        if cap == "mute" and command == "toggle":
            return "unmute" if st.get("mute") else "mute"
        if cap == "media_transport" and command == "play_pause":
            return "pause" if st.get("media_transport") == "playing" else "play"
        return command

    @staticmethod
    def _optimistic(cap: str, cmd: str, args: dict):
        """The value a capability will hold after `cmd` succeeds, so we can echo it
        NOW instead of waiting for SmartThings to reflect it on the next poll (its
        cloud can lag 10-30s, which reads as a sluggish UI switch). None = don't
        guess; the poll then remains the only source. A wrong guess self-corrects
        on the confirming poll below."""
        if cap == "on_off":
            return True if cmd == "turn_on" else False if cmd == "turn_off" else None
        if cap == "mute":
            return True if cmd == "mute" else False if cmd == "unmute" else None
        if cap == "media_transport":
            return {"play": "playing", "pause": "paused", "stop": "idle"}.get(cmd)
        if cap == "volume":
            v = args.get("value")
            return int(v) if isinstance(v, (int, float)) else None
        return None

    async def handle_command(self, command: Command) -> None:
        ent = self._routes.get(command.entity_id)
        if ent is None or self._st is None:
            raise CommandRejected("not ready")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        route = ent["routes"].get(command.capability)
        if route is None:
            raise CommandRejected(f"{command.capability} is not controllable here")
        cmd = self._resolve(command.entity_id, command.capability, command.command)
        try:
            stcap, stcmd, argument = translate_command(command.capability, cmd, dict(command.args or {}), route)
        except (ValueError, TypeError, KeyError) as exc:
            raise CommandRejected(f"cannot translate {command.capability}/{cmd}: {exc}") from exc
        try:
            await self._st.execute_device_command(
                ent["device_id"], stcap, stcmd, component=ent["component"], argument=argument
            )
        except Exception as exc:
            if _is_auth_error(exc):
                self._st = None
                self.status.error("SmartThings disconnected — reconnect")
            raise CommandRejected(f"{stcap}/{stcmd} failed: {exc}") from exc
        # Optimistic echo: publish the expected value NOW so the UI switch flips
        # instantly instead of lagging until SmartThings' cloud reflects the change
        # (10-30s). Update the cache too, so a follow-up toggle resolves correctly
        # and the confirming poll only re-publishes if the cloud disagrees (a
        # silent no-op self-corrects, no flicker on the common success path).
        opt = self._optimistic(command.capability, cmd, dict(command.args or {}))
        if opt is not None:
            # Hold this value against stale polls until the cloud confirms it.
            self._pending.setdefault(command.entity_id, {})[command.capability] = (
                opt, time.time_ns() + _ECHO_GRACE_NS)
            if self._state.get(command.entity_id, {}).get(command.capability) != opt:
                self._state.setdefault(command.entity_id, {})[command.capability] = opt
                await self._bus.publish_state(StateUpdate(
                    entity_id=command.entity_id, capability=command.capability, value=opt,
                    adapter=NAMESPACE, ts_ns=time.time_ns(),
                    unit="%" if command.capability == "volume" else None,
                    name=ent.get("name"), device=ent.get("device_key"), device_name=ent.get("label"),
                ))
        # Confirm against the cloud with spaced refreshes across the grace window;
        # a poll that still reports the old value is held (not re-published), so the
        # switch stays put until the cloud actually catches up.
        spawn(self._nudge(ent["device_id"]))

    async def _nudge(self, device_id: str) -> None:
        for delay in (3.0, 7.0, 10.0, 12.0):
            await asyncio.sleep(delay)
            with contextlib.suppress(Exception):
                await self._refresh_device(device_id)

    async def stop(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
