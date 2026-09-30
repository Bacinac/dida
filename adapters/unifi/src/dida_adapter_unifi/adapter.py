from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.request

from dida_core import (
    AdapterConfig,
    Bus,
    Command,
    PresenceReconciler,
    StateUpdate,
    fetch_presence_locations,
    slug,
)
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.unifi")

NAMESPACE = "unifi"  # source; owns presence:<user> + unifi:<device> entities
_API = "/proxy/network/integration/v1"


def _parse_people(raw: str) -> list[dict]:
    """One person per line: `user: label [| mac1, mac2]`.

    Give MACs and they are the identity — the addresses here are locally
    administered (private Wi-Fi address) but STABLE: the household's phones have
    held the same one for over a year. That makes the MAC the reliable key, while
    the client name is editable in the controller and is not even unique (a
    household regularly has two clients under one name). Without MACs the label
    falls back to matching the client name, which is enough to get started."""
    out: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        user, _, rest = line.partition(":")
        client, _, macs = rest.partition("|")
        user, client = user.strip(), client.strip()
        if user and client:
            out.append({
                "user": user,
                "name": user[:1].upper() + user[1:],
                "client": client,
                "macs": {m.strip().lower() for m in macs.split(",") if m.strip()},
            })
    return out


class UnifiAdapter:
    """Presence from the UniFi controller's ASSOCIATION table, plus each access
    point's up/down.

    Why association and not the router's ARP table: a modern phone dozes its Wi-Fi
    radio, so the router's neighbour entry ages out within ~20 min WHILE THE PHONE
    SITS AT HOME (measured 2026-07-09 — all five household phones read "offline" at
    home). 802.11 association survives power-save, and a disassociation is a
    POSITIVE departure signal rather than an entry quietly decaying. That is the one
    thing the retired OPNsense adapter could never get.

    The connected-client list is the presence signal by construction: the endpoint
    returns what is associated right now. `uplinkDeviceId` says through WHICH access
    point, which with four of them gives a coarse in-house zone on top of home/away.

    Read-only. Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    # GPS sources (OwnTracks / the web app) write the SAME presence:<user> entities.
    # Association is trustworthy on ARRIVAL and — unlike ARP — also on DEPARTURE, but
    # the grace stays wide: a phone can drop off Wi-Fi at home (radio off, roaming to
    # cellular, an AP reboot) and a network "gone" must never be the reason a GPS zone
    # collapses to "away". The reconciler owns that arbitration.
    _GRACE_S = 24 * 60 * 60

    # Two subsystems (clients + devices) share one badge; a subsystem only flips to
    # error after this many consecutive failures, so a single blip holds the last
    # good state.
    _FAIL_THRESHOLD = 3

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._url = ""
        self._key = ""
        self._verify = False
        self._site = "Home"
        self._site_id = ""
        self._people: list[dict] = []
        self._aps: dict[str, str] = {}   # device id -> name, refreshed with the device poll
        self._last_pub: dict[tuple[str, str], tuple[object, float]] = {}
        self._recon = PresenceReconciler(grace_seconds=self._GRACE_S)
        self._source_ok: dict[str, tuple[bool, str]] = {}
        self._source_fails: dict[str, int] = {}

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        self._load_config()
        log.info(
            "unifi adapter up — %s, site %s, %d people, poll %ss",
            self._url or "(unconfigured)", self._site, len(self._people),
            self._cfg.int("poll_seconds", 30),
        )
        spawn(self._cfg.poll_loop(), log=log, name="unifi config poll")
        while True:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("unifi: poll failed: %s", exc, exc_info=True)
                self.status.error(str(exc) or "poll failed")
            await asyncio.sleep(self._cfg.int("poll_seconds", 30))

    def _load_config(self) -> None:
        if self._cfg is None:
            return
        self._url = self._cfg.get("url").rstrip("/")
        self._key = self._cfg.get("api_key")
        self._verify = self._cfg.get("verify_tls") == "true"
        self._site = self._cfg.get("site", "Home")
        self._people = _parse_people(self._cfg.get("people"))

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, path: str) -> dict:
        ctx = ssl.create_default_context()
        if not self._verify:
            # The controller serves its own certificate on the LAN; the hop is
            # internal and the API key is the credential that matters.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(self._url + path)
        req.add_header("X-API-KEY", self._key)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return json.loads(r.read())

    def _resolve_site(self) -> str:
        """The site id the integration API keys everything on. Cached — it only
        changes if the controller is rebuilt."""
        if self._site_id:
            return self._site_id
        configured = self._cfg.get("site_id") if self._cfg else ""
        if configured:
            self._site_id = configured
            return self._site_id
        data = self._get(f"{_API}/sites")
        sites = data.get("data") or []
        if not sites:
            raise RuntimeError("controller reports no sites")
        self._site_id = str(sites[0]["id"])
        return self._site_id

    # ── poll ──────────────────────────────────────────────────────────────────

    async def _scan_once(self) -> None:
        self._load_config()
        if not self._url or not self._key:
            self.status.idle("not configured")
            return
        loop = asyncio.get_running_loop()
        site = await loop.run_in_executor(None, self._resolve_site)

        # Access points / switches — their own up/down, and the id → name map the
        # client list needs to say WHERE someone is.
        try:
            data = await loop.run_in_executor(None, self._get, f"{_API}/sites/{site}/devices?limit=200")
            devices = data.get("data") or []
            self._aps = {str(d["id"]): str(d.get("name") or d["id"]) for d in devices if d.get("id")}
            for d in devices:
                name = str(d.get("name") or d.get("id"))
                up = str(d.get("state", "ONLINE")).upper() not in ("OFFLINE", "DISCONNECTED")
                await self._pub(f"{NAMESPACE}:{slug(name)}", "connectivity", up, name)
            self._set_source("devices", True, f"{len(devices)} device(s)")
        except Exception as exc:
            log.warning("unifi: device poll failed: %s", exc, exc_info=True)
            self._set_source("devices", False, f"devices: {exc}" if str(exc) else "device poll failed")

        if not self._people:
            return
        try:
            data = await loop.run_in_executor(
                None, self._get, f"{_API}/sites/{site}/clients?limit=500")
            clients = data.get("data") or []
            current = await fetch_presence_locations(self.broker)
            for person in self._people:
                match = self._match(clients, person)
                await self._pub_presence(person, match is not None, current)
                if match is not None:
                    ap = self._aps.get(str(match.get("uplinkDeviceId") or ""), "")
                    if ap:
                        await self._pub(f"{NAMESPACE}:{slug(person['user'])}", "text", ap,
                                        f"{person['name']} — AP", diagnostic=True)
            self._set_source("clients", True, f"{len(self._people)} tracked / {len(clients)} online")
        except Exception as exc:
            log.warning("unifi: client poll failed: %s", exc, exc_info=True)
            self._set_source("clients", False, f"clients: {exc}" if str(exc) else "client poll failed")

    @staticmethod
    def _match(clients: list[dict], person: dict) -> dict | None:
        """MACs win outright when configured: they identify the device even if the
        client was renamed or never named. The label is the fallback."""
        if person["macs"]:
            for c in clients:
                if str(c.get("macAddress") or "").lower() in person["macs"]:
                    return c
            return None
        want = person["client"].lower()
        for c in clients:
            if str(c.get("name") or "").lower() == want:
                return c
        return None

    # ── publishing ────────────────────────────────────────────────────────────

    def _set_source(self, source: str, ok: bool, detail: str) -> None:
        """Record a subsystem's outcome and recompute the combined badge. A failure
        only registers once the streak passes the threshold, so one blip keeps the
        last good state."""
        if ok:
            self._source_fails[source] = 0
            self._source_ok[source] = (True, detail)
        else:
            self._source_fails[source] = self._source_fails.get(source, 0) + 1
            if self._source_fails[source] < self._FAIL_THRESHOLD:
                return
            self._source_ok[source] = (False, detail)
        self._render_status()

    def _render_status(self) -> None:
        good = [d for ok, d in self._source_ok.values() if ok]
        bad = [d for ok, d in self._source_ok.values() if not ok]
        if bad:
            self.status.error(" · ".join(bad + [f"ok: {g}" for g in good]))
        elif good:
            self.status.ok(" · ".join(good))

    async def _pub_presence(
        self, person: dict, online: bool, current: dict[str, tuple[str, float]]
    ) -> None:
        """Publish this person's network verdict through the shared reconciler, which
        owns the GPS-grace arbitration and the refresh dedupe."""
        if self._bus is None:
            return
        eid = f"presence:{slug(person['user'])}"
        value = self._recon.resolve(
            eid, online, self._site, current,
            refresh_seconds=self._cfg.int("refresh_seconds", 240),
        )
        if value is None:
            return
        await self._bus.publish_state(
            StateUpdate(
                entity_id=eid, capability="location", value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(),
                name=person["name"], diagnostic=False,
            )
        )

    async def _pub(
        self, entity_id: str, capability: str, value, name: str | None,
        *, force: bool = False, diagnostic: bool = False,
    ) -> None:
        if self._bus is None:
            return
        key = (entity_id, capability)
        now = time.time()
        prev = self._last_pub.get(key)
        # Re-publish an unchanged value at least this often so the UI's staleness
        # window never flags an actively-confirmed state.
        refresh = self._cfg.int("refresh_seconds", 240)
        if not force and prev is not None and prev[0] == value and (now - prev[1]) < refresh:
            return
        self._last_pub[key] = (value, now)
        await self._bus.publish_state(
            StateUpdate(
                entity_id=entity_id, capability=capability, value=value,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=name, diagnostic=diagnostic,
            )
        )

    async def handle_command(self, command: Command) -> None:
        return  # read-only source — no commands

    async def stop(self) -> None:
        self._bus = None
