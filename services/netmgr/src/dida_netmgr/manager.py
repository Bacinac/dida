"""DIDA network manager — gives DIDA a presence on the IoT VLAN(s) WITHOUT touching
the host's network config.

It does this the container-scoped way: for each desired VLAN it asks Docker to
create a **macvlan** network trunked to `<parent>.<vlan>` (Docker creates + later
removes that tagging sub-link itself; the host's parent NIC, its IP and routing
are never touched, and nothing is written to netplan). It then connects THIS
container to that network — so the VLAN interface lands in netmgr's own netns —
and runs a DHCP client on it, so the address comes from the router, which stays
the sole owner of DHCP/firewall/routing.

Desired VLAN ids live in `app_settings.managed_vlans` (from the UI). A reconcile
loop converges Docker + this container to it and publishes each interface's
obtained address to `app_settings.vlan_status`. State is reconstructed from
Docker on every boot — no host files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re

from dida_core import emit_journal, set_app_setting
from home_core.tasks import spawn

log = logging.getLogger("dida.netmgr")

async def parent_nic(pool) -> str:
    """The host NIC the VLAN sub-links hang off. Settings → Network owns it (it is a
    property of THIS box, like the VLAN list next to it); the env is the seed."""
    from dida_core import host_setting

    return await host_setting(pool, "net_parent", "ens18")


CONTAINER = os.environ.get("DIDA_NETMGR_CONTAINER", "dida-netmgr")
# The filtered socket of docker-proxy-netmgr, never the raw Docker socket.
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/docker/docker.sock")
POLL_SECONDS = float(os.environ.get("DIDA_NETMGR_POLL_SECONDS", "10"))
NET_PREFIX = "dida-vlan"  # Docker networks we own: dida-vlan<id>

# VLAN ingress: netmgr is the one container with a foot on the IoT VLAN, so it
# relays these ports from its VLAN address to the (bridge-only) adapter that
# actually serves them. An Ecowitt console can only push same-VLAN → it hits
# netmgr:<port>, which forwards to the ecowitt push receiver. (listen, target, port)
FORWARDS: list[tuple[int, str, int]] = [(4199, "dida-adapter-ecowitt", 4199)]


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            w.close()


def _valid_vlan(vid: int) -> bool:
    return 1 <= vid <= 4094


def _dhcp_hostname(name: str) -> str:
    """The host-name to present over DHCP, reduced to what a hostname may contain.

    It reaches us from the VLAN config (admin-entered), and it is written into a
    dhclient CONFIG FILE as `send host-name "<name>";` — so a stray quote or
    semicolon would end that statement and let the rest of the string become
    further dhclient directives. Keep letters, digits, hyphen and dot (RFC 1123),
    drop everything else; an empty result means "send no host-name at all"."""
    cleaned = re.sub(r"[^A-Za-z0-9.-]", "", name).strip(".-")
    return cleaned[:63]  # a DNS label maxes out at 63 chars


async def _run(*args: str) -> tuple[int, str]:
    # Bound every helper (ip/dhclient): a hung tool must fail the 10s reconcile fast,
    # not stall it forever while the healthcheck ticks green (same rationale as _docker).
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()  # reap the killed child
        log.warning("command timed out (>15s), killed: %s", " ".join(args))
        return 1, ""
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _write_text(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


class NetManager:
    def __init__(self, pool, bus=None) -> None:
        self._pool = pool
        self._bus = bus  # journal only; netmgr's real job must not depend on the bus
        self._last_status: dict | None = None  # write-on-change guard for vlan_status

    # --- Docker API over the unix socket ----------------------------------

    async def _docker(self, method: str, path: str, body: dict | None = None):
        import aiohttp

        conn = aiohttp.UnixConnector(path=DOCKER_SOCK)
        # Bound every Docker call: a wedged dockerd must fail the 10s reconcile fast,
        # not stall it for aiohttp's 300s default while the healthcheck ticks green.
        async with (
            aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=10)) as s,
            s.request(method, f"http://docker{path}", json=body) as r,
        ):
            txt = await r.text()
            return r.status, txt

    def _net(self, vid: int) -> str:
        return f"{NET_PREFIX}{vid}"

    async def _existing(self) -> set[int]:
        """VLAN ids THIS container is actually CONNECTED to (not merely networks
        that exist globally). Key on connection, not existence: after a container
        recreate the macvlan network persists but our attachment is gone — reconcile
        must then re-`_add` (create-is-idempotent) to reconnect + re-DHCP, or we'd
        wait forever for an address on a network we never joined."""
        st, body = await self._docker("GET", f"/containers/{CONTAINER}/json")
        out: set[int] = set()
        if st == 200:
            nets = json.loads(body).get("NetworkSettings", {}).get("Networks", {}) or {}
            for name in nets:
                if name.startswith(NET_PREFIX):
                    tail = name[len(NET_PREFIX):]
                    if tail.isdigit():
                        out.add(int(tail))
        return out

    # --- reconcile primitives ---------------------------------------------

    async def _iface_names(self) -> set[str]:
        _, out = await _run("ip", "-o", "link", "show")
        names = set()
        for line in out.splitlines():
            if ":" in line:
                names.add(line.split(":", 2)[1].strip().split("@")[0])
        return names

    def _dummy_subnet(self, vid: int) -> str:
        # Docker macvlan requires a subnet, but the real address comes from the
        # router via DHCP — so give it a unique unused placeholder per VLAN (it's
        # immediately flushed off the interface; only DHCP's address remains).
        return f"10.{240 + (vid >> 8)}.{vid & 0xff}.0/24"

    def _is_placeholder(self, vid: int, cidr: str) -> bool:
        """True if `cidr` (e.g. '10.240.20.1/24') is from this VLAN's dummy subnet
        — i.e. Docker's IPAM placeholder, NOT a real DHCP lease. Docker re-applies
        it to the iface on every reattach, so we must never mistake it for a lease
        (or netmgr would publish a bogus 10.x gateway-squatting address as DIDA's)."""
        prefix = self._dummy_subnet(vid).rsplit(".", 1)[0] + "."  # '10.240.20.'
        return cidr.split("/", 1)[0].startswith(prefix)

    async def _add(self, vid: int, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        parent = f"{await parent_nic(self._pool)}.{vid}"
        log.info("creating macvlan network %s (parent %s)", self._net(vid), parent)
        st, body = await self._docker("POST", "/networks/create", {
            "Name": self._net(vid), "Driver": "macvlan", "CheckDuplicate": True,
            "Options": {"parent": parent},
            "IPAM": {"Config": [{"Subnet": self._dummy_subnet(vid)}]},
        })
        if st not in (200, 201) and "exists" not in body:
            log.warning("create %s failed (%s): %s", self._net(vid), st, body)
            return
        before = await self._iface_names()
        connect: dict = {"Container": CONTAINER}
        mac = (cfg.get("mac") or "").strip()
        if mac:  # a fixed MAC → the router can pin a reserved DHCP lease to it
            connect["EndpointConfig"] = {"MacAddress": mac}
        st, body = await self._docker("POST", f"/networks/{self._net(vid)}/connect", connect)
        if st not in (200, 201):
            log.warning("connect self to %s failed (%s): %s", self._net(vid), st, body)
            return
        await asyncio.sleep(1)
        hostname = (cfg.get("hostname") or "").strip()
        for iface in await self._iface_names() - before:
            await _run("ip", "addr", "flush", "dev", iface)   # drop Docker's placeholder
            await _run("ip", "link", "set", iface, "up")
            await self._dhcp(iface, hostname)                  # real lease from the router

    async def _remove(self, vid: int) -> None:
        log.info("removing macvlan network %s", self._net(vid))
        # Kill THIS vlan's dhclient (its own pidfile) before the iface disappears — a
        # lingering daemon on a gone iface would zombie, and with a shared pidfile a
        # later `-r` on another vlan would kill the wrong (pid-in-file) daemon.
        iface = await self._iface_for(vid)
        if iface:
            await self._dhcp_release(iface)
        await self._docker("POST", f"/networks/{self._net(vid)}/disconnect", {"Container": CONTAINER, "Force": True})
        await self._docker("DELETE", f"/networks/{self._net(vid)}")

    @staticmethod
    def _dhcp_files(iface: str) -> tuple[str, str]:
        """Per-iface pidfile + leasefile. Each managed VLAN MUST get its own, or a
        `dhclient -r ethB` on the default shared /var/run/dhclient.pid would kill
        whatever daemon (ethA's) that file happens to point at — a multi-VLAN cross-kill."""
        return f"/run/dhclient-{iface}.pid", f"/run/dhclient-{iface}.leases"

    async def _iface_for(self, vid: int) -> str:
        """The in-container interface for a VLAN: its Docker endpoint MAC → iface name."""
        st, body = await self._docker("GET", f"/containers/{CONTAINER}/json")
        nets = json.loads(body).get("NetworkSettings", {}).get("Networks", {}) if st == 200 else {}
        mac = (nets.get(self._net(vid), {}).get("MacAddress") or "").lower()
        return (await self._mac_to_iface()).get(mac, "")

    async def _dhcp_release(self, iface: str) -> None:
        """Release this iface's lease + stop its dedicated dhclient (its own pidfile)."""
        pf, lf = self._dhcp_files(iface)
        await _run("dhclient", "-4", "-pf", pf, "-lf", lf, "-r", iface)

    async def _dhcp(self, iface: str, hostname: str = "") -> None:
        pf, lf = self._dhcp_files(iface)
        await _run("dhclient", "-4", "-pf", pf, "-lf", lf, "-r", iface)
        args = ["dhclient", "-4", "-pf", pf, "-lf", lf, "-nw"]
        safe = _dhcp_hostname(hostname)
        if safe:  # present a friendly host-name so DIDA is identifiable in DHCP leases
            cf = f"/tmp/dhclient-{iface}.conf"
            try:
                await asyncio.to_thread(_write_text, cf, f'send host-name "{safe}";\n')
                args += ["-cf", cf]
            except OSError:
                log.warning("dhclient on %s: host-name not presented", iface, exc_info=True)
        rc, out = await _run(*args, iface)
        if rc != 0:
            log.warning("dhclient on %s: %s", iface, out)

    async def _mac_to_iface(self) -> dict[str, str]:
        _, out = await _run("ip", "-o", "link", "show")
        res: dict[str, str] = {}
        for line in out.splitlines():
            if "link/ether" in line and ":" in line:
                name = line.split(":", 2)[1].strip().split("@")[0]
                mac = line.split("link/ether", 1)[1].split()[0].lower()
                res[mac] = name
        return res

    async def _addr_by_iface(self) -> dict[str, str]:
        _, out = await _run("ip", "-4", "-o", "addr", "show")
        res: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                res[parts[1]] = parts[3]
        return res

    async def _publish_status(self, desired: set[int]) -> None:
        """Per VLAN: map its Docker network's MacAddress (from container inspect)
        to the in-container interface, then to its DHCP-obtained address."""
        st, body = await self._docker("GET", f"/containers/{CONTAINER}/json")
        nets = json.loads(body).get("NetworkSettings", {}).get("Networks", {}) if st == 200 else {}
        mac_iface = await self._mac_to_iface()
        addrs = await self._addr_by_iface()
        status: dict[str, dict] = {}
        for vid in sorted(desired):
            mac = (nets.get(self._net(vid), {}).get("MacAddress") or "").lower()
            iface = mac_iface.get(mac, "")
            addr = addrs.get(iface, "")
            if addr and self._is_placeholder(vid, addr):
                addr = ""  # Docker's IPAM placeholder, not a real lease — don't surface it
            # Surface the live MAC so the user can pin a reserved lease to it.
            status[str(vid)] = {"iface": iface, "address": addr, "mac": mac}
        if status == self._last_status:  # only write on change (no needless 10s churn)
            return
        await self._journal_status_change(status)
        await set_app_setting(self._pool, "vlan_status", json.dumps(status))
        self._last_status = status

    async def _journal_status_change(self, status: dict[str, dict]) -> None:
        """Record VLAN and lease transitions. This method is already the fleet's
        change detector — it exists because the status is only worth writing when
        it differs — so hanging the journal off it costs one diff and can never
        report a change that did not happen. A first reconcile (no previous
        status) is a boot, not a transition, and is deliberately silent."""
        if self._bus is None or self._last_status is None:
            return
        prev = self._last_status
        for vid in sorted(set(status) | set(prev)):
            was, now = prev.get(vid), status.get(vid)
            if now is None:
                await emit_journal(self._bus, "vlan_down", source="netmgr", severity="notice",
                                   message=f"VLAN {vid} released", data={"vlan": vid})
            elif was is None:
                await emit_journal(self._bus, "vlan_up", source="netmgr", severity="notice",
                                   message=f"VLAN {vid} joined", data={"vlan": vid, **now})
            elif was.get("address") != now.get("address"):
                # The one that matters operationally: DIDA's address on the IoT
                # VLAN changed under it, which is what a device suddenly failing
                # to reach it looks like from the other side.
                addr = now.get("address") or ""
                await emit_journal(
                    self._bus, "dhcp_lease", source="netmgr",
                    severity="warning" if not addr else "info",
                    message=(f"VLAN {vid} lease {was.get('address') or 'none'} → {addr or 'none'}"),
                    data={"vlan": vid, "was": was.get("address") or "", "now": addr},
                )

    async def _desired(self) -> set[int]:
        raw = await self._pool.fetchval("SELECT value FROM app_settings WHERE key = 'managed_vlans'") or ""
        return {int(t.strip()) for t in raw.split(",") if t.strip().isdigit() and _valid_vlan(int(t.strip()))}

    async def _vlan_config(self) -> dict:
        """Per-VLAN {mac, hostname} overrides (from the UI), keyed by vlan id (str)."""
        raw = await self._pool.fetchval("SELECT value FROM app_settings WHERE key = 'vlan_config'") or "{}"
        try:
            cfg = json.loads(raw)
            return cfg if isinstance(cfg, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    async def _current_macs(self) -> dict[int, str]:
        """Per connected VLAN: the MAC Docker gave its endpoint (lowercased)."""
        st, body = await self._docker("GET", f"/containers/{CONTAINER}/json")
        nets = json.loads(body).get("NetworkSettings", {}).get("Networks", {}) if st == 200 else {}
        out: dict[int, str] = {}
        for name, n in nets.items():
            if name.startswith(NET_PREFIX) and name[len(NET_PREFIX):].isdigit():
                out[int(name[len(NET_PREFIX):])] = (n.get("MacAddress") or "").lower()
        return out

    async def _ensure_dhcp(self, desired: set[int], config: dict) -> None:
        """After a restart Docker reattaches the macvlan iface but the lease is
        gone — and it re-applies its IPAM placeholder (the dummy subnet) to the
        iface. That placeholder is not a real lease, so treat it like an empty
        iface: flush it and re-acquire from the router. Otherwise netmgr keeps the
        throwaway 10.x address and publishes it as DIDA's VLAN address."""
        st, body = await self._docker("GET", f"/containers/{CONTAINER}/json")
        nets = json.loads(body).get("NetworkSettings", {}).get("Networks", {}) if st == 200 else {}
        mac_iface = await self._mac_to_iface()
        addrs = await self._addr_by_iface()
        for vid in desired:
            mac = (nets.get(self._net(vid), {}).get("MacAddress") or "").lower()
            iface = mac_iface.get(mac, "")
            if not iface:
                continue
            addr = addrs.get(iface, "")
            if addr and not self._is_placeholder(vid, addr):
                continue  # a real DHCP lease is already present — nothing to do
            await _run("ip", "addr", "flush", "dev", iface)   # drop the placeholder, if any
            await _run("ip", "link", "set", iface, "up")
            await self._dhcp(iface, (config.get(str(vid), {}).get("hostname") or "").strip())

    async def reconcile(self) -> None:
        desired = await self._desired()
        config = await self._vlan_config()
        existing = await self._existing()
        # Reconnect a VLAN whose configured MAC no longer matches the live endpoint
        # (the user set/changed a MAC to pin a reserved DHCP lease).
        macs = await self._current_macs()
        for vid in list(desired & existing):
            want = (config.get(str(vid), {}).get("mac") or "").strip().lower()
            if want and macs.get(vid, "") != want:
                log.info("vlan %d MAC change (%s → %s) — reconnecting", vid, macs.get(vid, "?"), want)
                await self._remove(vid)
                existing.discard(vid)
        for vid in desired - existing:
            await self._add(vid, config.get(str(vid), {}))
        for vid in existing - desired:
            await self._remove(vid)
        await self._ensure_dhcp(desired & existing, config)
        await self._publish_status(desired)

    async def _container_ip(self, name: str) -> str | None:
        """A container's bridge IP via the Docker API. netmgr is on a macvlan net,
        which breaks Docker's embedded DNS — so resolve by inspect, not by name."""
        st, body = await self._docker("GET", f"/containers/{name}/json")
        if st != 200:
            return None
        for n in (json.loads(body).get("NetworkSettings", {}).get("Networks", {}) or {}).values():
            if n.get("IPAddress"):
                return n["IPAddress"]
        return None

    async def _forward(self, listen_port: int, target: str, target_port: int) -> None:
        """Relay 0.0.0.0:listen_port (reachable on netmgr's VLAN address) to a
        bridge-only container, so VLAN devices reach an adapter with no VLAN foot."""
        async def _handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
            ip = await self._container_ip(target)
            if not ip:
                cw.close()
                return
            try:
                tr, tw = await asyncio.open_connection(ip, target_port)
            except (OSError, TimeoutError):
                cw.close()
                return
            await asyncio.gather(_pipe(cr, tw), _pipe(tr, cw))

        while True:
            try:
                server = await asyncio.start_server(_handle, "0.0.0.0", listen_port)
                log.info("vlan ingress :%d → %s:%d", listen_port, target, target_port)
                async with server:
                    await server.serve_forever()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("forward :%d failed; retrying", listen_port)
                await asyncio.sleep(5)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("netmgr up — parent=%s, container=%s (macvlan; host untouched)",
                 await parent_nic(self._pool), CONTAINER)
        for lp, th, tp in FORWARDS:  # VLAN ingress relays (e.g. Ecowitt push → ecowitt)
            spawn(self._forward(lp, th, tp), log=log, name=f"vlan forward :{lp}")
        while not stop.is_set():
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("netmgr reconcile error")
            with contextlib.suppress(TimeoutError):  # wake early on SIGTERM instead of sleeping through it
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        log.info("netmgr shutting down")
