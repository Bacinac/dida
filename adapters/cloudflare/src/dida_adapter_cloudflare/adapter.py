from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time

from dida_core import AdapterConfig, Bus, Command, StateUpdate, slug
from home_core.tasks import spawn

from dida_adapter_cloudflare.tunnel import parse_config, render_config

log = logging.getLogger("dida.adapter.cloudflare")

NAMESPACE = "cloudflare"       # owns cloudflare:<fqdn-slug> route entities
CTL_SUBJECT = "dida.cloudflare.ctl"
RUNNER_SUBJECT = "dida.runner.tunnel"
SERVE_VALUES = ("both", "lan", "wan")

_HEADER = """# Jedan izvor istine za ingress. Iz njega se generiraju Caddyfile (LAN) i
# cloudflared config.yml (WAN); split-DNS slijedi iz Caddyfilea.
#
# serve:  both = i lokalno i izvana | lan = samo lokalno | wan = samo izvana
# ssh:// origini su uvijek wan (Caddy ne proxira SSH).
# https:// origini u Caddyju dobiju tls_insecure_skip_verify (interni certifikati).
#
# hostname                 origin                             serve  opts
"""


def _parse_meta(comment: str) -> dict:
    meta: dict[str, str] = {}
    for part in comment.split("|"):
        k, sep, v = part.partition(":")
        k, v = k.strip().lower(), v.strip()
        if sep and k in ("section", "icon") and v:
            meta[k] = v
    return meta


def parse_source(text: str) -> list[dict]:
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        body, _, comment = line.partition("#")
        body = body.strip()
        if not body:
            continue
        parts = body.split()
        if len(parts) not in (3, 4):
            raise ValueError(f"line {n}: expected 3 or 4 columns, got {len(parts)}")
        host, origin, serve = parts[:3]
        if serve not in SERVE_VALUES:
            raise ValueError(f"line {n}: serve must be one of {SERVE_VALUES}, not {serve!r}")
        meta = _parse_meta(comment)
        rows.append({
            "host": host, "origin": origin, "serve": serve,
            "opts": parts[3].split(",") if len(parts) == 4 else [],
            "section": meta.get("section"), "icon": meta.get("icon"),
        })
    return rows


def format_source(rows: list[dict]) -> str:
    out = [_HEADER]
    for r in sorted(rows, key=lambda r: r["host"]):
        line = f"{r['host']:<26} {r['origin']:<34} {r['serve']}"
        if r.get("opts"):
            line += "  " + ",".join(r["opts"])
        meta = []
        if r.get("section"):
            meta.append(f"section: {r['section']}")
        if r.get("icon"):
            meta.append(f"icon: {r['icon']}")
        if meta:
            line += "   # " + " | ".join(meta)
        out.append(line + "\n")
    return "".join(out)


def validate_row(row: dict) -> None:
    """The rules the renderer enforces, checked before the file is written so a bad
    edit is rejected at the UI instead of failing the apply after the fact."""
    if not row["host"] or "." in row["host"] or "/" in row["host"]:
        raise ValueError("hostname must be the bare subdomain, e.g. gitea")
    if "://" not in row["origin"]:
        raise ValueError("origin must include a scheme, e.g. http://192.0.2.5:8080")
    if row["serve"] not in SERVE_VALUES:
        raise ValueError(f"serve must be one of {SERVE_VALUES}")
    if row["origin"].startswith("ssh://") and row["serve"] != "wan":
        raise ValueError("ssh:// origins must be wan — Caddy cannot proxy SSH")
    unknown = [o for o in row["opts"] if o not in ("nochunk",)]
    if unknown:
        raise ValueError(f"unknown option(s): {unknown}")


def _read_or_none(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _write_file(path: str, text: str, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


class CloudflareAdapter:
    """Edits the ingress this installation carries — see `_mode`.

    In `source` mode that is `services.conf`, the single source for the whole
    ingress: the Caddyfile (LAN), the cloudflared ingress (WAN), Cloudflare DNS,
    split-DNS on the router and the Homepage tiles. Writing it is all this adapter
    does there; a systemd path unit on the Proxmox host notices the change and runs
    apply-ingress.sh, which is the only thing that CAN apply it — Caddy lives in
    another container, and `pct exec` plus the router's SSH key exist only on the
    host.

    In `tunnel` mode there is no such source and no applier: DIDA runs the
    connectors itself, so their `config.yml` IS the ingress. The adapter renders it
    (see tunnel.py) and asks the runner to roll the connectors — one at a time, so
    the tunnel carrying the request that caused the change never goes dark.

    Until 2026-08-01 this adapter kept its own `cloudflare_routes` table and generated
    config.yml from it EVERYWHERE, which made two writers for one ingress wherever a
    host renderer also existed: the DB knew nothing about LAN vhosts, split-DNS or
    Homepage, so a UI edit silently reverted whatever the renderer had produced. The
    file the ingress is actually read from is now the source in both modes, so there
    is never a second copy to drift from.

    NOT a device adapter: each route is a read-only `cloudflare:<fqdn>` health entity,
    and CRUD runs over `dida.cloudflare.ctl`. The adapter's own status mirrors the last
    apply — a failed apply on the host surfaces as an adapter error rather than a
    change that quietly never took effect.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._source = "/ingress/services.conf"
        self._status_file = "/ingress/.apply-status.json"
        self._config = "/cloudflared/config.yml"
        self._apply_wait = 150.0
        self._last_pub: dict[str, tuple[object, float]] = {}
        self._tunnel_meta: dict = {}
        self._tunnel_apply: dict = {}
        self._zone = ""
        self._token = ""
        self._dns_task: asyncio.Task | None = None

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        await self._cfg.load()
        self._load_config()
        await bus.nc.subscribe(CTL_SUBJECT, cb=self._on_ctl)
        spawn(self._cfg.poll_loop(), log=log, name="cloudflare config poll")
        mode = self._mode()
        log.info("cloudflare adapter up — %s mode (%s), control on %s", mode,
                 self._config if mode == "tunnel" else self._source, CTL_SUBJECT)
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("cloudflare: refresh failed: %s", exc, exc_info=True)
                self.status.error(str(exc) or "refresh failed")
            await asyncio.sleep(self._cfg.int("poll_seconds", 60))

    def _load_config(self) -> None:
        if self._cfg is None:
            return
        self._source = self._cfg.get("source_path", "/ingress/services.conf")
        self._status_file = self._cfg.get("status_path", "/ingress/.apply-status.json")
        self._config = self._cfg.get("config_path", "/cloudflared/config.yml")
        self._apply_wait = float(self._cfg.int("apply_wait_s", 150))
        self._zone = (self._cfg.get("zone") or "").strip()
        self._token = (self._cfg.get("api_token") or "").strip()

    def _mode(self) -> str:
        """Which ingress this installation actually carries, decided by what exists
        on disk rather than by a flag someone has to remember to set:

        `source`  — a host-side services.conf drives Caddy, DNS and the connectors,
                    and a host applier owns applying it (the house).
        `tunnel`  — no such source; the connectors are ours and their config.yml IS
                    the ingress, so we render it and roll them ourselves.
        `none`    — neither is mounted. Not a failure: this installation simply does
                    not route anything, and says so instead of glowing red forever.
        """
        if os.path.exists(self._source):
            return "source"
        if os.path.exists(self._config):
            return "tunnel"
        return "none"

    # ── source file ───────────────────────────────────────────────────────────

    def _require_zone(self) -> None:
        if not self._zone:
            raise RuntimeError("set the DNS zone in this adapter's settings first")

    def _read(self) -> list[dict]:
        self._require_zone()
        if self._mode() == "tunnel":
            with open(self._config) as fh:
                self._tunnel_meta, rows = parse_config(fh.read(), self._zone)
            return rows
        with open(self._source) as fh:
            return parse_source(fh.read())

    def _write(self, rows: list[dict]) -> None:
        """Atomic replace — a truncated file would make the applier (or cloudflared
        itself) reject the whole ingress, and a rename is what the host path unit
        watches for just as it watches a write."""
        if self._mode() == "tunnel":
            self._write_tunnel(rows)
            return
        tmp = self._source + ".dida-new"
        with open(tmp, "w") as fh:
            fh.write(format_source(rows))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._source)
        log.info("cloudflare: services.conf rewritten (%d routes)", len(rows))

    def _write_tunnel(self, rows: list[dict]) -> None:
        self._require_zone()
        if not self._tunnel_meta:
            with open(self._config) as fh:
                self._tunnel_meta, _ = parse_config(fh.read(), self._zone)
        text = render_config(self._tunnel_meta, rows, self._zone)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        with contextlib.suppress(OSError):
            shutil.copy2(self._config, f"{self._config}.bak-{stamp}")
        tmp = self._config + ".dida-new"
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._config)
        log.info("cloudflare: config.yml rewritten (%d routes)", len(rows))
        if self._token and (self._dns_task is None or self._dns_task.done()):
            self._dns_task = spawn(self._ensure_dns(list(rows)), log=log, name="cloudflare dns ensure")

    async def _ensure_dns(self, rows: list[dict]) -> None:
        """Every tunnel route needs its proxied CNAME or it resolves nowhere. With
        the owner's token we own that step too; failures are LOUD on the badge —
        a route that saved but does not resolve must not look done."""
        from dida_adapter_cloudflare.provision import CfApi, CfError

        tunnel_id = str(self._tunnel_meta.get("tunnel") or "")
        if not tunnel_id:
            return
        api = CfApi(self._token)
        try:
            zone_id = await api.zone_id(self._zone)
            for row in rows:
                await api.ensure_dns(zone_id, f"{row['host']}.{self._zone}", tunnel_id)
        except CfError as exc:
            log.warning("cloudflare: dns ensure failed: %s", exc)
            self.status.error(f"DNS: {exc}")
        except Exception as exc:
            log.warning("cloudflare: dns ensure failed: %s", exc, exc_info=True)
            self.status.error(f"DNS: {exc}")
        finally:
            await api.aclose()

    def _apply_status(self) -> dict:
        if self._mode() == "tunnel":
            return self._tunnel_apply
        try:
            with open(self._status_file) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    async def _roll_connectors(self) -> dict:
        """Apply a tunnel-mode write: the connectors read config.yml at boot, so the
        change takes effect when they restart. The runner does it — it is the only
        service holding the docker socket — one connector at a time, so the tunnel
        this very reply travels through never goes dark."""
        req = {"action": "roll"}
        try:
            msg = await self._bus.nc.request(  # type: ignore[union-attr]
                RUNNER_SUBJECT, json.dumps(req).encode(), timeout=self._apply_wait)
            res = json.loads(msg.data)
        except Exception as exc:
            log.warning("cloudflare: runner did not roll the connectors", exc_info=True)
            res = {"ok": False, "error": str(exc) or "runner did not answer"}
        self._tunnel_apply = {
            "ok": bool(res.get("ok")), "error": res.get("error"),
            "steps": res.get("restarted") or [], "ts": time.time(),
        }
        return {"applied": bool(res.get("ok")), "error": res.get("error"),
                "steps": res.get("restarted") or []}

    async def _refresh(self) -> None:
        self._load_config()
        mode = self._mode()
        if mode == "none":
            # Neither ingress is mounted here. That is a fact about the
            # installation, not a fault, so it reads as idle with the reason.
            self.status.idle(f"no ingress here — neither {self._source} nor {self._config}")
            return
        rows = self._read()
        for r in rows:
            await self._pub(r["host"], r["origin"])
        st = self._apply_status()
        if st and not st.get("ok"):
            self.status.error(f"apply failed: {st.get('error') or 'unknown'}")
        else:
            self.status.ok(f"{len(rows)} route(s) · {mode}")

    async def _pub(self, host: str, origin: str) -> None:
        if self._bus is None or not host:
            return
        # Slug the FQDN, not the bare subdomain: these entities predate the move to
        # services.conf, and shortening the id would orphan every one of them and
        # publish a duplicate set beside it.
        eid = f"{NAMESPACE}:{slug(f'{host}.{self._zone}')}"
        now = time.time()
        prev = self._last_pub.get(eid)
        if prev is not None and prev[0] == origin and (now - prev[1]) < 300:
            return
        self._last_pub[eid] = (origin, now)
        await self._bus.publish_state(StateUpdate(
            entity_id=eid, capability="text", value=origin,
            adapter=NAMESPACE, ts_ns=time.time_ns(), name=f"{host}.{self._zone}", diagnostic=True,
        ))

    # ── control plane ─────────────────────────────────────────────────────────

    async def _on_ctl(self, msg) -> None:
        """Request/reply. Actions: list | add | edit | remove | reload."""
        try:
            req = json.loads(msg.data)
            action = req.get("action")
            if action == "list":
                result = self._act_list()
            elif action in ("add", "edit"):
                result = await self._act_upsert(req, must_exist=(action == "edit"))
            elif action == "remove":
                result = await self._act_remove(str(req.get("hostname", "")).strip().lower())
            elif action == "reload":
                result = await self._act_reload()
            elif action == "tunnel_state":
                result = self._act_tunnel_state()
            elif action == "tunnel_create":
                result = await self._act_tunnel_create(str(req.get("name", "")).strip())
            else:
                result = {"error": f"unknown action {action!r}"}
        except Exception as exc:
            log.warning("cloudflare ctl failed: %s", exc, exc_info=True)
            result = {"error": str(exc) or "ctl failed"}
        if msg.reply:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())  # type: ignore[union-attr]

    def _act_tunnel_state(self) -> dict:
        mode = self._mode()
        tunnel_id = None
        if mode == "tunnel":
            with contextlib.suppress(Exception):
                self._read()
                tunnel_id = str(self._tunnel_meta.get("tunnel") or "") or None
        return {
            "mode": mode, "zone": self._zone,
            "has_token": bool(self._token), "tunnel_id": tunnel_id,
        }

    async def _act_tunnel_create(self, name: str) -> dict:
        """Mint this installation's tunnel on the OWNER's account: create it, write
        the credentials file + initial config.yml, then ask the runner to bring the
        connectors up. Refuses over an existing ingress — one tunnel per install."""
        from dida_adapter_cloudflare.provision import (
            CfApi,
            CfError,
            credentials_json,
            new_tunnel_secret,
            validate_tunnel_name,
        )

        if self._mode() != "none":
            return {"error": "this installation already has an ingress"}
        reason = validate_tunnel_name(name)
        if reason:
            return {"error": reason}
        if not self._token:
            return {"error": "set the Cloudflare API token in this adapter's settings first"}
        if not self._zone:
            return {"error": "set the DNS zone in this adapter's settings first"}
        api = CfApi(self._token)
        try:
            account = await api.account_id()
            secret = new_tunnel_secret()
            tunnel_id = await api.create_tunnel(account, name, secret)
        except CfError as exc:
            return {"error": str(exc)}
        finally:
            await api.aclose()
        cred_path = os.path.join(os.path.dirname(self._config), f"{tunnel_id}.json")
        await asyncio.to_thread(_write_file, cred_path, credentials_json(account, tunnel_id, secret), 0o600)
        self._tunnel_meta = {"tunnel": tunnel_id, "credentials-file": cred_path}
        self._write_tunnel([])
        # Bring the connectors up — their config now exists, so no crash loop.
        try:
            msg = await self._bus.nc.request(  # type: ignore[union-attr]
                RUNNER_SUBJECT, json.dumps({"action": "enable"}).encode(), timeout=10)
            res = json.loads(msg.data)
        except Exception as exc:
            log.warning("cloudflare: runner did not enable the tunnel", exc_info=True)
            res = {"ok": False, "error": str(exc) or "runner did not answer"}
        if not res.get("ok"):
            log.warning("cloudflare: runner did not switch the tunnel on: %s", res.get("error"))
        log.info("cloudflare: tunnel %r created (%s)", name, tunnel_id)
        return {"ok": True, "tunnel_id": tunnel_id}

    def _act_list(self) -> dict:
        rows = self._read()
        st = self._apply_status()
        return {
            "routes": [{
                "id": r["host"], "hostname": r["host"], "service": r["origin"],
                "serve": r["serve"], "opts": r["opts"],
                "section": r["section"], "icon": r["icon"],
            } for r in rows],
            "mode": self._mode(),
            "source_path": self._config if self._mode() == "tunnel" else self._source,
            "apply": {"ok": st.get("ok"), "error": st.get("error"),
                      "steps": st.get("steps") or [], "ts": st.get("ts")},
        }

    def _row_from(self, req: dict) -> dict:
        opts = req.get("opts") or []
        if isinstance(opts, str):
            opts = [o for o in opts.split(",") if o]
        row = {
            "host": str(req.get("hostname", "")).strip().lower(),
            "origin": str(req.get("service", "")).strip(),
            "serve": str(req.get("serve", "both")).strip().lower(),
            "opts": opts,
            "section": (req.get("section") or None),
            "icon": (req.get("icon") or None),
        }
        if row["origin"].startswith("ssh://"):
            row["serve"] = "wan"
        validate_row(row)
        return row

    async def _act_upsert(self, req: dict, *, must_exist: bool) -> dict:
        row = self._row_from(req)
        rows = self._read()
        idx = next((i for i, r in enumerate(rows) if r["host"] == row["host"]), None)
        if must_exist and idx is None:
            return {"error": f"no route for {row['host']}"}
        if not must_exist and idx is not None:
            return {"error": f"route for {row['host']} already exists"}
        if idx is None:
            rows.append(row)
        else:
            rows[idx] = row
        return await self._commit(rows, row["host"])

    async def _act_remove(self, host: str) -> dict:
        if not host:
            return {"error": "hostname required"}
        rows = self._read()
        kept = [r for r in rows if r["host"] != host]
        if len(kept) == len(rows):
            return {"error": f"no route for {host}"}
        # Hand back the entity id rather than letting the API derive it: only this
        # adapter knows the route entities are keyed by FQDN, not by the bare
        # subdomain the API is given.
        return {**await self._commit(kept, host),
                "entity_id": f"{NAMESPACE}:{slug(f'{host}.{self._zone}')}"}

    async def _act_reload(self) -> dict:
        """Re-apply without changing anything — touching the source is what the host
        watches, and the applier is idempotent."""
        self._write(self._read())
        return {"ok": True, **await self._await_apply()}

    async def _commit(self, rows: list[dict], host: str) -> dict:
        live = self._config if self._mode() == "tunnel" else self._source
        prev = await asyncio.to_thread(_read_or_none, live)
        try:
            self._write(rows)
        except Exception as exc:
            log.warning("cloudflare: ingress write failed", exc_info=True)
            return {"ok": False, "error": str(exc) or "write failed"}
        res = await self._await_apply()
        if not res.get("applied") and prev is not None:
            # The apply was rejected (invalid render, caddy validate, a connector
            # that never registered). Put the previous file back so the UI and the
            # live config never disagree, and report it instead of leaving it
            # half-done. In tunnel mode the connectors still run the old ingress,
            # so restoring the file is what makes the two agree again.
            await asyncio.to_thread(_write_file, live, prev)
            log.warning("cloudflare: apply failed for %s — source reverted", host)
        return {"ok": bool(res.get("applied")), "hostname": host, **res}

    async def _await_apply(self) -> dict:
        # Tunnel mode has no host applier to wait for: we are the applier.
        if self._mode() == "tunnel":
            return await self._roll_connectors()
        return await self._await_host_apply()

    async def _await_host_apply(self) -> dict:
        """Wait for the host applier to report on this change. Fail loud on timeout —
        a silent 'saved' while the ingress never changed is the failure mode this whole
        single-source arrangement exists to remove."""
        started = time.time()
        before = self._apply_status().get("ts") or 0
        while time.time() - started < self._apply_wait:
            await asyncio.sleep(1.5)
            st = self._apply_status()
            if (st.get("ts") or 0) > before:
                return {"applied": bool(st.get("ok")), "error": st.get("error"),
                        "steps": st.get("steps") or []}
        return {"applied": False, "error": f"host applier did not report within {self._apply_wait:.0f}s"}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def handle_command(self, command: Command) -> None:
        return  # management adapter — CRUD is via the ctl subject

    async def stop(self) -> None:
        self._bus = None
