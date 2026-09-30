"""Starlark evaluation worker — a SEPARATE process so the sandbox timeout is REAL.

`starlark.eval` holds the CPython GIL for the whole evaluation, so a bounded-but-huge
`for` (e.g. `range(10**12)`) run in a thread starves the entire automation service —
the in-thread timeout can't even get scheduled. Each evaluation therefore runs in a
worker PROCESS: the parent enforces a wall-clock deadline and SIGKILLs a runaway,
which the GIL of this interpreter cannot prevent. This is what makes the CLAUDE.md /
PROPOSAL guarantee — "a script structurally cannot hang the engine" — actually true.

The worker stays warm and handles requests in a loop (one at a time) so the common
case pays no per-eval interpreter startup. It is killed + replaced by the pool on a
timeout or a protocol error.

Wire protocol on stdin/stdout: a 4-byte big-endian length prefix + a JSON body.
  request  {"script", "event", "snapshot": [[eid, cap, val], ...]}
  response {"ok": true, "emitted": [[eid, cap, cmd, args], ...], "states": [[eid, cap, val], ...]}
         | {"ok": false, "error": str, "kind": "syntax" | "error"}
"""

from __future__ import annotations

import json
import struct
import sys

from dida_automation.starlark_runtime import StarlarkError, compile_script, run, run_value


def _read_frame(f) -> bytes | None:
    hdr = f.read(4)
    if len(hdr) < 4:
        return None  # EOF → the pool closed our stdin → shut down cleanly
    (n,) = struct.unpack(">I", hdr)
    buf = f.read(n)
    return buf if len(buf) == n else None


def _write_frame(f, payload: bytes) -> None:
    f.write(struct.pack(">I", len(payload)))
    f.write(payload)
    f.flush()


def _handle(req: dict) -> dict:
    snapshot = {(str(e), str(c)): v for e, c, v in req.get("snapshot", [])}
    try:
        ast = compile_script(req["script"])
    except StarlarkError as exc:
        return {"ok": False, "error": str(exc), "kind": "syntax"}
    # mode "value" = a computed-helper definition: PURE, returns one value, no actions.
    if req.get("mode") == "value":
        try:
            return {"ok": True, "value": run_value(ast, snapshot)}
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 — incl. the Rust engine's PanicException (a
            # BaseException, not Exception) — e.g. `x="a"; [x := x+x for _ in range(40)]`
            # overflows starlark-rust and would otherwise crash the worker past the
            # wall-clock cap. Report it as a script error, stay warm.
            return {"ok": False, "error": str(exc), "kind": "error"}
    try:
        emitted, states = run(ast, req.get("event") or {}, snapshot)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 — a runtime error OR a Rust panic — report, don't crash
        return {"ok": False, "error": str(exc), "kind": "error"}
    return {
        "ok": True,
        "emitted": [[e, c, cmd, args] for e, c, cmd, args in emitted],
        "states": [[e, c, v] for e, c, v in states],
    }


def main() -> None:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        raw = _read_frame(stdin)
        if raw is None:
            return
        try:
            resp = _handle(json.loads(raw))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 — never let one bad request (or a Rust panic) kill the worker
            resp = {"ok": False, "error": f"worker: {exc}", "kind": "error"}
        try:
            body = json.dumps(resp).encode()
        except (TypeError, ValueError) as exc:
            # A "value" script assigned a non-serialisable object (e.g. a Starlark
            # function) — report it as a script error instead of dying on the write.
            body = json.dumps({"ok": False,
                               "error": f"script produced a non-serialisable value: {exc}",
                               "kind": "error"}).encode()
        _write_frame(stdout, body)


if __name__ == "__main__":
    main()
