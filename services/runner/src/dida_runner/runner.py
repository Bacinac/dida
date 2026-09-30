from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from dida_core import CORE, Bus
from home_core.health import HealthMarker

log = logging.getLogger("dida.runner")

NAME = "runner"
CTL_SUBJECT = "dida.runner.ctl"
# The one runner subject the adapter role may publish on (docker/nats.conf): the
# cloudflare adapter rolls the tunnel connectors and switches the tunnel on. It
# names no service itself, so a compromised adapter can do exactly these two things.
TUNNEL_SUBJECT = "dida.runner.tunnel"
TUNNEL_PROFILE = "cloudflare-tunnel"
TUNNEL_CONNECTORS = ["cloudflared", "cloudflared-2"]
COMPOSE = "/usr/local/bin/docker-compose"
ENV_KEY = "COMPOSE_PROFILES"
# Build-only; it holds the base image, never a service anyone switches on.
HIDDEN_PROFILES = frozenset({"bases"})


class ComposeError(RuntimeError):
    pass


class Runner:
    """Switches an installation's optional services on and off.

    Every adapter is its own compose profile, and the set of enabled profiles
    lives in one place — `COMPOSE_PROFILES` in the installation's `.env`, which is
    what compose itself reads. This service is the only writer of that line and
    the only container with the Docker socket: the api asks over NATS, so the
    publicly-exposed process never holds root-equivalent access to the host.

    Applying runs in the BACKGROUND and the reply returns at once. The request
    travels over the very stack being changed — switching `cloudflare` off tears
    down the tunnel the browser is talking through — so a caller that waited for
    compose to finish would be waiting on a connection its own action just cut.
    """

    def __init__(self, project_dir: Path) -> None:
        self._dir = project_dir
        self._env = project_dir / ".env"
        self._bus: Bus | None = None
        self._lock = asyncio.Lock()
        self._applying: str | None = None
        # Held so the background apply can't be garbage-collected mid-compose.
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    # ── compose plumbing ──────────────────────────────────────────────────────

    async def _compose(self, *args: str, timeout_s: float = 600) -> str:
        proc = await asyncio.create_subprocess_exec(
            COMPOSE, *args, cwd=str(self._dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            raise ComposeError(f"compose {' '.join(args)} timed out after {timeout_s}s") from None
        if proc.returncode != 0:
            tail = (err or b"").decode(errors="replace").strip().splitlines()[-4:]
            raise ComposeError(f"compose {' '.join(args)} failed: {' / '.join(tail) or proc.returncode}")
        return (out or b"").decode(errors="replace")

    async def _catalog(self) -> dict[str, list[str]]:
        """profile -> services, for every profile the compose file defines."""
        raw = await self._compose("--profile", "*", "config", "--format", "json", timeout_s=120)
        cfg = json.loads(raw)
        out: dict[str, list[str]] = {}
        for name, svc in cfg.get("services", {}).items():
            for profile in svc.get("profiles") or []:
                if profile in HIDDEN_PROFILES:
                    continue
                out.setdefault(profile, []).append(name)
        return {p: sorted(s) for p, s in sorted(out.items())}

    async def _containers(self) -> dict[str, str]:
        """service -> container state ('running', 'exited', …) for what exists now."""
        raw = await self._compose("--profile", "*", "ps", "-a", "--format", "json", timeout_s=120)
        states: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows = row if isinstance(row, list) else [row]
            for r in rows:
                if r.get("Service"):
                    states[r["Service"]] = r.get("State", "")
        return states

    # ── the enabled set (COMPOSE_PROFILES in .env) ────────────────────────────

    def _read_enabled(self) -> list[str]:
        for line in self._env.read_text().splitlines():
            if line.startswith(f"{ENV_KEY}="):
                raw = line.split("=", 1)[1].strip()
                return sorted({p.strip() for p in raw.split(",") if p.strip()})
        return []

    def _write_env_vars(self, vars: dict[str, str]) -> None:
        """Rewrite the named lines, atomically, leaving every other line untouched —
        this file is the installation's own config, not something we own."""
        lines = self._env.read_text().splitlines(keepends=True)
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0] if "=" in line else ""
            if key in vars:
                out.append(f"{key}={vars[key]}\n")
                seen.add(key)
            else:
                out.append(line)
        for key in vars:
            if key not in seen:
                out.append(f"{key}={vars[key]}\n")
        tmp = self._env.with_suffix(".env.tmp")
        tmp.write_text("".join(out))
        # Carry the original owner and mode across: this container runs as root, so a
        # plain replace would hand the installation's .env to root and lock its own
        # user out of the file it edits by hand.
        st = self._env.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, self._env)
        log.info("runner: wrote %s", ", ".join(f"{k}={v}" for k, v in vars.items()))

    def _read_env_var(self, key: str) -> str:
        for line in self._env.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
        return ""

    def _write_enabled(self, profiles: set[str]) -> None:
        self._write_env_vars({ENV_KEY: ",".join(sorted(profiles))})

    # ── actions ───────────────────────────────────────────────────────────────

    async def state(self) -> dict:
        catalog = await self._catalog()
        enabled = set(self._read_enabled())
        containers = await self._containers()
        profiles = {}
        for profile, services in catalog.items():
            running = [s for s in services if containers.get(s) == "running"]
            profiles[profile] = {
                "services": services,
                "enabled": profile in enabled,
                "running": len(running),
                "total": len(services),
            }
        return {
            "profiles": profiles,
            "applying": self._applying,
            "error": self._last_error,
        }

    async def _apply(self, profile: str, enable: bool) -> None:
        async with self._lock:
            self._applying = profile
            self._last_error = None
            try:
                catalog = await self._catalog()
                services = catalog[profile]
                enabled = set(self._read_enabled())
                if enable:
                    enabled.add(profile)
                    self._write_enabled(enabled)
                    await self._compose("--profile", profile, "up", "-d", "--no-deps", *services)
                    log.info("runner: enabled %s (%s)", profile, ", ".join(services))
                else:
                    # Remove the containers BEFORE the file: compose only knows a
                    # service by a profile that is still active, so a `.env` written
                    # first would leave them running with nothing able to name them.
                    await self._compose("--profile", "*", "rm", "-s", "-f", *services)
                    enabled.discard(profile)
                    self._write_enabled(enabled)
                    log.info("runner: disabled %s (%s)", profile, ", ".join(services))
            except Exception as exc:
                self._last_error = f"{profile}: {exc}"
                log.exception("runner: applying %s failed", profile)
            finally:
                self._applying = None

    async def restart(self, services: list[str]) -> dict:
        """Restart the named services ONE AT A TIME, so a pair that serves the same
        traffic keeps serving throughout. The caller may be reaching us through the
        very thing being restarted (the tunnel connectors are the case this exists
        for), which is why the roll never takes them all down at once."""
        catalog = await self._catalog()
        known = {s for svcs in catalog.values() for s in svcs}
        unknown = [s for s in services if s not in known]
        if unknown:
            return {"ok": False, "error": f"unknown service(s): {unknown}"}
        async with self._lock:
            done = []
            for name in services:
                try:
                    await self._compose("restart", name, timeout_s=180)
                except Exception as exc:
                    log.warning("runner: restarting %s failed: %s", name, exc, exc_info=True)
                    return {"ok": False, "error": f"{name}: {exc}", "restarted": done}
                done.append(name)
                if name != services[-1]:
                    await asyncio.sleep(6)  # let it re-register before the next goes
            log.info("runner: rolled %s", ", ".join(done))
            return {"ok": True, "restarted": done}

    async def _switch(self, profile: str, enable: bool) -> dict:
        catalog = await self._catalog()
        if profile not in catalog:
            return {"ok": False, "error": f"unknown profile {profile!r}"}
        if self._applying:
            return {"ok": False, "error": f"busy applying {self._applying}"}
        self._task = asyncio.create_task(self._apply(profile, enable))
        return {"ok": True, "applying": True, "profile": profile}

    async def _roll(self, services: list[str]) -> dict:
        if self._applying:
            return {"ok": False, "error": f"busy applying {self._applying}"}
        return await self.restart(services)

    async def _on_ctl(self, msg) -> None:
        """Request/reply. Actions: state | enable | disable | restart."""
        try:
            req = json.loads(msg.data)
            action = req.get("action")
            if action == "state":
                result = await self.state()
            elif action in ("enable", "disable"):
                result = await self._switch(str(req.get("profile", "")).strip(), action == "enable")
            elif action == "restart":
                services = req.get("services") or []
                if not isinstance(services, list) or not services:
                    result = {"ok": False, "error": "services required"}
                else:
                    result = await self._roll([str(s) for s in services])
            else:
                result = {"ok": False, "error": f"unknown action {action!r}"}
        except Exception as exc:
            log.warning("runner ctl failed: %s", exc, exc_info=True)
            result = {"ok": False, "error": str(exc) or "ctl failed"}
        await self._reply(msg, result)

    async def _on_tunnel(self, msg) -> None:
        """Request/reply for the cloudflare adapter. Actions: roll | enable."""
        try:
            action = json.loads(msg.data).get("action")
            if action == "roll":
                result = await self._roll(TUNNEL_CONNECTORS)
            elif action == "enable":
                result = await self._switch(TUNNEL_PROFILE, True)
            else:
                result = {"ok": False, "error": f"unknown action {action!r}"}
        except Exception as exc:
            log.warning("runner tunnel ctl failed: %s", exc, exc_info=True)
            result = {"ok": False, "error": str(exc) or "ctl failed"}
        await self._reply(msg, result)

    async def _reply(self, msg, result: dict) -> None:
        if msg.reply:
            await self._bus.nc.publish(msg.reply, json.dumps(result).encode())  # type: ignore[union-attr]

    async def run(self) -> None:
        self._bus = Bus(os.environ["DIDA_NATS_URL"], name=f"dida-{NAME}", user=CORE)
        await self._bus.connect()
        await self._bus.nc.subscribe(CTL_SUBJECT, cb=self._on_ctl)  # type: ignore[union-attr]
        await self._bus.nc.subscribe(TUNNEL_SUBJECT, cb=self._on_tunnel)  # type: ignore[union-attr]
        # Fail loud at boot rather than on the first click: without the socket or the
        # project directory this service cannot do the one thing it exists for.
        catalog = await self._catalog()
        log.info("runner up — %d switchable profiles, control on %s", len(catalog), CTL_SUBJECT)
        health = HealthMarker("dida", NAME)
        await health.run_loop()
