"""A pool of Starlark worker subprocesses — the REAL Layer-2 sandbox boundary.

Each evaluation runs in a warm worker process; the parent enforces a wall-clock
deadline and SIGKILLs a worker that overruns (a runaway `for` loop), then respawns a
replacement. Only a separate PROCESS is actually killable — `starlark.eval` holds the
GIL, so the old thread+timeout was a no-op that let a huge bounded loop freeze the
whole automation service. See `starlark_worker` for the wire protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import resource
import struct
import sys

from dida_automation.starlark_runtime import StarlarkError

log = logging.getLogger("dida.automation.sandbox")

_WORKER_MODULE = "dida_automation.starlark_worker"

# Address-space ceiling per worker. The wall-clock kill bounds a script's TIME; this
# bounds its SPACE — an allocation bomb (a giant list/string) dies with MemoryError
# inside the sandbox instead of ballooning host RAM and OOM-killing siblings. Set on
# the hard limit too so the child can't lift it. 768 MB leaves the Rust Starlark
# engine + CPython ample headroom while still capping a runaway allocation.
_WORKER_MEM_CAP = 768 * 1024 * 1024

# A worker counts as warm only once it has ANSWERED. create_subprocess_exec returns
# the moment the process is exec'd, with the interpreter still booting and importing
# the Rust Starlark engine — so the first evaluation handed to it paid that startup
# INSIDE its own 2 s deadline. On a busy box (a deploy starting fifty containers at
# once) that blew the cap, and every restart left `script exceeded 2.0s wall-clock
# cap` as the last_error of the first two or three rules to fire. The handshake is a
# no-op script: the reply is proof the engine is loaded and the next caller measures
# only its own script. Its own timeout is generous — this IS the cold start, and the
# eval cap is not the thing being tested here.
_WARMUP_TIMEOUT = 60.0
_WARMUP_REQUEST = json.dumps({"script": "pass", "event": {}, "snapshot": []}).encode()


def _cap_worker_memory() -> None:
    """Child-side preexec: cap the worker's virtual address space. Runs in the
    forked child before exec, so it constrains only the worker, never the parent."""
    resource.setrlimit(resource.RLIMIT_AS, (_WORKER_MEM_CAP, _WORKER_MEM_CAP))


class StarlarkTimeout(RuntimeError):
    """A script exceeded the sandbox wall-clock deadline and was killed."""


class StarlarkPool:
    """Warm worker processes with a real, kill-backed per-evaluation timeout."""

    def __init__(self, *, size: int = 3, timeout: float = 2.0) -> None:
        self._size = size
        self._timeout = timeout
        self._idle: asyncio.Queue[asyncio.subprocess.Process] = asyncio.Queue()
        self._workers: set[asyncio.subprocess.Process] = set()  # every live worker (idle or busy)
        self._closing = False

    async def start(self) -> None:
        # Warmed concurrently: this is boot, and one interpreter start after another
        # would delay the first rule by the sum rather than the slowest.
        for proc in await asyncio.gather(*(self._spawn() for _ in range(self._size))):
            self._idle.put_nowait(proc)
        log.info("starlark sandbox pool up — %d warm workers, %.1fs wall-clock cap",
                 self._size, self._timeout)

    async def _spawn(self) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            preexec_fn=_cap_worker_memory,  # bound the worker's memory (see above)
        )
        self._workers.add(proc)
        try:
            await asyncio.wait_for(self._exchange(proc, _WARMUP_REQUEST),
                                   timeout=_WARMUP_TIMEOUT)
        except BaseException:  # incl. CancelledError — never leave a half-born worker
            await self._kill(proc)
            raise
        return proc

    async def run(self, script: str, event: dict, snapshot: dict) -> tuple[list, list]:
        """Evaluate `script` in a worker. Returns (emitted, states).

        Raises `StarlarkError` (syntax or runtime error in the script) or
        `StarlarkTimeout` (the script blew the wall-clock cap and was killed)."""
        resp = await self._dispatch({
            "script": script,
            "event": event,
            "snapshot": [[e, c, v] for (e, c), v in snapshot.items()],
        })
        emitted = [(e, c, cmd, args) for e, c, cmd, args in resp["emitted"]]
        states = [(e, c, v) for e, c, v in resp["states"]]
        return emitted, states

    async def run_value(self, script: str, snapshot: dict):
        """Evaluate a COMPUTED-HELPER definition (mode 'value'): the script is PURE —
        it reads `state()` and assigns `value` — and this returns that value. No
        commands, no state writes: a helper cannot act, so it cannot loop. Same
        worker pool + wall-clock kill as `run` (a bad helper script can't hang us)."""
        resp = await self._dispatch({
            "script": script, "mode": "value",
            "snapshot": [[e, c, v] for (e, c), v in snapshot.items()],
        })
        return resp["value"]

    async def _dispatch(self, req_dict: dict) -> dict:
        """Borrow a warm worker, exchange one framed request, return it to the pool
        (or kill + replenish on timeout/cancel/death). Shared by run and run_value."""
        req = json.dumps(req_dict).encode()
        proc = await self._idle.get()
        try:
            resp = await asyncio.wait_for(self._exchange(proc, req), timeout=self._timeout)
        except TimeoutError:
            await self._kill(proc)          # the runaway is gone — the GIL can't save it
            await self._replenish()
            raise StarlarkTimeout(f"script exceeded {self._timeout:.1f}s wall-clock cap") from None
        except asyncio.CancelledError:       # BaseException — the two handlers around this
            # would NOT catch it, stranding the borrowed worker (never returned, never
            # killed, so _replenish won't top up → the pool bleeds a warm worker per
            # cancel until Layer 2 wedges). Cancels reach here via the outer ACTION_TIMEOUT
            # wait_for and via reload() cancelling a Starlark hold task mid-fire.
            await self._kill(proc)
            await self._replenish()
            raise
        except Exception as exc:             # worker died / protocol desync → replace it
            await self._kill(proc)
            await self._replenish()
            # Normalize to StarlarkError so EVERY caller (run/run_value, and the helper
            # path) handles a worker death the same way it handles a script error —
            # otherwise a raw IncompleteReadError/OSError escapes the helper's except
            # tuple, leaving no last_error and churning the pool on each input tick.
            raise StarlarkError(f"sandbox worker failed: {exc}") from exc
        else:
            self._idle.put_nowait(proc)      # healthy → back into the warm pool
        if not resp.get("ok"):
            raise StarlarkError(resp.get("error", "unknown starlark error"))
        return resp

    async def _exchange(self, proc: asyncio.subprocess.Process, req: bytes) -> dict:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(struct.pack(">I", len(req)) + req)
        await proc.stdin.drain()
        hdr = await proc.stdout.readexactly(4)
        (n,) = struct.unpack(">I", hdr)
        body = await proc.stdout.readexactly(n)
        return json.loads(body)

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        self._workers.discard(proc)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()  # reap — otherwise the killed worker orphans

    async def _replenish(self) -> None:
        # Only top up to `size`: a kill just removed one, so this restores the
        # target exactly and can never over-spawn into an accumulating leak.
        if not self._closing and len(self._workers) < self._size:
            self._idle.put_nowait(await self._spawn())

    async def close(self) -> None:
        self._closing = True
        # Drain the idle queue, then kill EVERY tracked worker (incl. any busy one
        # not currently in the queue) so shutdown leaves nothing behind.
        while not self._idle.empty():
            self._idle.get_nowait()
        for proc in list(self._workers):
            await self._kill(proc)
