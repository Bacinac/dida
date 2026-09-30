from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dida_core import (
    Bus,
    Command,
    CommandRejected,
    EntityInfo,
    FailureGate,
    StateUpdate,
    host_setting,
    set_reachable,
)

from dida_adapter_opus.mapping import (
    CAPABILITIES,
    CONTROLS,
    DAC,
    DAC_CAPABILITIES,
    NAMESPACE,
    TV,
    TV_NAME,
    order_from_address,
    order_from_media,
    order_from_queue,
    read_dac,
    read_now,
)

log = logging.getLogger("dida.adapter.opus")

TOKEN_HEADER = "X-OPUS-Token"
POLL_SECONDS = 2
_UNITS = {"media_position": "s", "media_duration": "s"}


@dataclass
class _Output:
    """One of the player's outputs as a media entity: where it is read, where its
    orders go, and what the house was last told about it."""

    entity: str
    base: str
    read: Callable[[dict], dict[str, object]]
    capabilities: list[str]
    name: str = ""
    announced: bool = False
    last: dict[str, object] = field(default_factory=dict)
    reach: bool | None = None
    failures: int = 0


class OpusAdapter:
    """OPUS · Player's two outputs as media players: the television (`/api/tv/*`)
    and the DAC on the player's host (`/api/dac/*`). Where the player answers and
    with what token is the house's one statement of it — `opus_url` and
    `opus_token` under Settings → Network — not a second copy in this adapter's
    own settings. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._session = None
        self._gate: FailureGate | None = None
        self._outputs = {
            TV: _Output(TV, "/api/tv", read_now, CAPABILITIES, name=TV_NAME),
            DAC: _Output(DAC, "/api/dac", read_dac, DAC_CAPABILITIES),
        }

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        self._gate = FailureGate(self.status, threshold=3)
        log.info("opus adapter starting")
        await self._poll()

    async def _where(self) -> tuple[str, str]:
        url = (await host_setting(self.broker, "opus_url")).rstrip("/")
        return url, await self.broker.call("setting", key="opus_token") or ""

    async def _poll(self) -> None:
        while True:
            try:
                url, token = await self._where()
                if not url or not token:
                    self.status.idle("OPUS is not configured — Settings → Network")
                    await asyncio.sleep(10)
                    continue
                problems = [p for p in await asyncio.gather(
                    *(self._read(url, token, out) for out in self._outputs.values())) if p]
                if problems:
                    self._gate.fail("; ".join(problems))  # type: ignore[union-attr]
                elif self._outputs[TV].last.get("media_transport") == "idle":
                    self._gate.ok("OPUS is not open on the television")  # type: ignore[union-attr]
                else:
                    self._gate.ok("listening")  # type: ignore[union-attr]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._gate.fail(str(exc) or type(exc).__name__)  # type: ignore[union-attr]
                log.debug("opus poll failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    async def _read(self, url: str, token: str, out: _Output) -> str:
        """One output read and published; what went wrong, or nothing. The two fail
        on their own — an unplugged DAC is no reason to call the television
        unreachable."""
        try:
            now = await self._get(url, token, f"{out.base}/now")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("opus: %s unreadable", out.entity, exc_info=True)
            problem = f"{out.entity}: {str(exc) or type(exc).__name__}"
            out.failures += 1
            if out.failures >= 3:
                await self._publish_reach(out, False, problem)
            return problem
        out.failures = 0
        await self._announce(out, str(now.get("name") or out.name or out.entity))
        for capability, value in out.read(now).items():
            await self._set(out, capability, value)
        await self._publish_reach(out, True)
        return ""

    async def _announce(self, out: _Output, name: str) -> None:
        # the DAC is named in the player's settings, so a rename there is news
        if out.announced and out.name == name:
            return
        out.name = name
        await self._bus.publish_entity(EntityInfo(  # type: ignore[union-attr]
            entity_id=out.entity, adapter=NAMESPACE, name=name, device_type="media",
            device=out.entity, device_name=name, capabilities=out.capabilities,
        ))
        out.announced = True

    async def _publish_reach(self, out: _Output, ok: bool, detail: str = "") -> None:
        if self._bus is None or out.reach == ok:
            return
        out.reach = ok
        await set_reachable(self._bus, out.entity, NAMESPACE, ok, detail="" if ok else detail)

    async def _set(self, out: _Output, capability: str, value: object) -> None:
        if out.last.get(capability) == value:
            return
        out.last[capability] = value
        await self._bus.publish_state(StateUpdate(  # type: ignore[union-attr]
            entity_id=out.entity, capability=capability, value=value,  # type: ignore[arg-type]
            adapter=NAMESPACE, ts_ns=time.time_ns(), unit=_UNITS.get(capability),
            name=out.name, device=out.entity, device_name=out.name,
        ))

    async def _get(self, url: str, token: str, path: str) -> dict:
        async with self._session.get(f"{url}{path}", headers={TOKEN_HEADER: token}) as resp:  # type: ignore[union-attr]
            if resp.status == 401:
                raise RuntimeError("OPUS · Player refused the token")
            if resp.status >= 400:
                said = (await resp.text())[:200]
                raise RuntimeError(f"OPUS · Player answered {resp.status} to {path}: {said}")
            return await resp.json()

    async def _post(self, url: str, token: str, path: str, body: dict) -> None:
        async with self._session.post(f"{url}{path}", json=body, headers={TOKEN_HEADER: token}) as resp:  # type: ignore[union-attr]
            if resp.status >= 400:
                said = (await resp.text())[:200]
                raise RuntimeError(f"OPUS · Player answered {resp.status} to {path}: {said}")

    async def handle_command(self, command: Command) -> None:
        out = self._outputs.get(command.entity_id)
        if out is None or command.capability != "media_transport":
            raise CommandRejected(f"no OPUS output takes {command.capability} as {command.entity_id}")
        url, token = await self._where()
        if not url or not token:
            raise CommandRejected("OPUS is not configured")
        if command.command in CONTROLS:
            await self._post(url, token, f"{out.base}/control", {"command": command.command})
        elif command.command == "play_queue":
            order = order_from_queue(dict(command.args))
            if order is None:
                raise CommandRejected(f"play_queue carried nothing {out.entity} can play")
            await self._post(url, token, f"{out.base}/play", order)
        elif command.command == "play_media" and str(command.args.get("uri") or "").startswith("opus:"):
            asked = order_from_address(dict(command.args)) if out.entity == TV else None
            if asked is None:
                raise CommandRejected(f"{command.args.get('uri')} is not something {out.entity} does")
            await self._post(url, token, f"{out.base}{asked[0]}", asked[1])
        elif command.command == "play_media":
            order = order_from_media(dict(command.args))
            if order is None:
                raise CommandRejected("play_media carried no address")
            await self._post(url, token, f"{out.base}/play", order)
        else:
            raise CommandRejected(f"{command.command} is not something {out.entity} does")
        log.info("opus: %s sent to %s (source %s)", command.command, out.entity, command.source)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
