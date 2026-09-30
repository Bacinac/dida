"""Regression tests for the Layer-2 Starlark sandbox.

Run via the pytest gate (see tests/run.sh); or directly in the automation image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/automation:latest \
      -c "python -m pytest tests/test_sandbox.py"

Covers the two guarantees the sandbox exists to provide — a script emits only
validated commands/states through the injected API, and a runaway script is
KILLED (wall-clock cap) rather than hanging the engine — plus that a normal
script still evaluates under the new RLIMIT_AS memory cap.
"""
import asyncio
import json
import struct
import sys
import time

import pytest
from dida_automation.sandbox import StarlarkPool, StarlarkTimeout
from dida_automation.starlark_runtime import MAX_EMITS, StarlarkError, compile_script, run


def go(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_injected_api_emits():
    # the injected API produces commands + states
    emitted, states = run(compile_script('turn_on("mqtt:light")'), {}, {})
    assert emitted == [("mqtt:light", "on_off", "turn_on", {})], "turn_on sugar emits an on_off command"

    emitted, states = run(compile_script('command("mqtt:l", "brightness", "set_brightness", 128)'), {}, {})
    assert emitted == [("mqtt:l", "brightness", "set_brightness", {"value": 128})], "command() maps a scalar to the value arg"

    emitted, states = run(compile_script('notify("notify:all", "T", "M")'), {}, {})
    assert emitted == [("notify:all", "notify", "notify", {"title": "T", "message": "M"})], "notify() builds the multi-arg map"

    emitted, states = run(compile_script('notify("notify:all", "T", "M", "baba:cam:zone:z")'), {}, {})
    assert emitted == [("notify:all", "notify", "notify",
                        {"title": "T", "message": "M", "camera": "baba:cam:zone:z"})], \
        "notify() carries the camera whose frame the push should show"

    emitted, states = run(compile_script('set_state("virtual:night", "on_off", True)'), {}, {})
    assert states == [("virtual:night", "on_off", True)], "set_state writes a derived state"


def test_one_run_emits_a_bounded_amount():
    emitted, _ = run(compile_script(f'for i in range({MAX_EMITS}):\n    turn_on("mqtt:l%d" % i)'), {}, {})
    assert len(emitted) == MAX_EMITS
    flood = f'for i in range({MAX_EMITS}):\n    turn_on("mqtt:l")\nset_state("derived:x", "on_off", True)'
    with pytest.raises(StarlarkError, match=f"at most {MAX_EMITS}"):
        run(compile_script(flood), {}, {})


def test_event_and_state_branching():
    script = 'if event["value"] and state("sensor:lux", "illuminance") < 10:\n    turn_on("mqtt:lamp")'
    emitted, _ = run(compile_script(script), {"value": True}, {("sensor:lux", "illuminance"): 5})
    assert emitted == [("mqtt:lamp", "on_off", "turn_on", {})], "script branches on event + snapshot"
    emitted, _ = run(compile_script(script), {"value": True}, {("sensor:lux", "illuminance"): 500})
    assert emitted == [], "condition not met → nothing emitted"


def test_no_filesystem_io():
    # `open` in the sandbox is DIDA's cover-open COMMAND sugar, not Python's file
    # builtin — it's shadowed, so a script cannot read the filesystem. open() on a
    # path just emits a (nonsense) cover command; no file is ever touched.
    emitted, _ = run(compile_script('x = open("/etc/passwd")'), {}, {})
    assert emitted == [("/etc/passwd", "open_close", "open", {})], "open() is the cover-open sugar, not filesystem access"


def test_compile_rejects_bad_scripts():
    with pytest.raises(StarlarkError):  # syntax error is a StarlarkError, not a crash
        compile_script("this is (not valid starlark")
    with pytest.raises(StarlarkError):  # `while` is not in the grammar — scripts can't spin
        compile_script("while True:\n    pass")


async def _pool_checks():
    # The cap no longer covers a worker's startup — start() hands out only workers
    # that have already answered — so 3s is pure evaluation budget, generous even on
    # a loaded host. It still hard-bounds the runaway below: the ~1e10-iteration loop
    # blows past 3s just as easily, so the SIGKILL guarantee is unchanged.
    pool = StarlarkPool(size=2, timeout=3.0)
    await pool.start()
    try:
        emitted, _ = await pool.run('turn_on("mqtt:x")', {}, {})
        assert emitted == [("mqtt:x", "on_off", "turn_on", {})], "pool evaluates a normal script under RLIMIT_AS (cap doesn't break eval)"

        # Nested bounded loops (~1e10 iterations, each range within Starlark's i32
        # limit) hold the GIL for far longer than the 1s cap; only the SIGKILL of
        # the worker process can stop it. The pool must surface StarlarkTimeout.
        runaway = "total = 0\nfor i in range(100000):\n    for j in range(100000):\n        total = total + 1"
        with pytest.raises(StarlarkTimeout):  # runaway loop is KILLED, engine not hung
            await pool.run(runaway, {}, {})

        # The pool recovers — a fresh eval works after the kill+replace.
        emitted, _ = await pool.run('turn_off("mqtt:x")', {}, {})
        assert emitted == [("mqtt:x", "on_off", "turn_off", {})], "pool replenishes after a kill"
    finally:
        await pool.close()


def test_pool_eval_and_runaway_kill():
    go(_pool_checks())


async def _warm_checks():
    """The pool promises WARM workers, and the eval deadline is supposed to measure
    the script — not the interpreter starting up behind it.

    `create_subprocess_exec` returns as soon as the process is exec'd, so without a
    readiness handshake the first evaluation paid CPython's boot plus the import of
    the Rust Starlark engine inside its own cap. On a busy box that blew it, and
    every restart of the automation service left "script exceeded 2.0s wall-clock
    cap" as the last_error of the first rules to fire — seen on production across
    twelve separate days.

    Self-calibrating rather than a fixed threshold: it times what a cold worker
    actually costs on THIS machine (after a throwaway spawn, so the page cache is
    warm for both measurements and the comparison is fair), then gives the pool a
    deadline too tight for a cold start to fit inside. A pool handing out unwarmed
    workers times out here; a warm one answers in milliseconds.
    """
    async def cold_exchange() -> float:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "dida_automation.starlark_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        req = json.dumps({"script": "pass", "event": {}, "snapshot": []}).encode()
        started = time.monotonic()
        proc.stdin.write(struct.pack(">I", len(req)) + req)
        await proc.stdin.drain()
        (n,) = struct.unpack(">I", await proc.stdout.readexactly(4))
        await proc.stdout.readexactly(n)
        elapsed = time.monotonic() - started
        proc.kill()
        await proc.wait()
        return elapsed

    await cold_exchange()          # throwaway: page-cache the interpreter + engine
    cold = await cold_exchange()   # what a cold worker's first answer really costs

    pool = StarlarkPool(size=2, timeout=cold * 0.6)
    await pool.start()
    try:
        assert await pool.run("pass", {}, {}) == ([], [])
        # The second worker too — start() must warm ALL of them, not just the first.
        assert await pool.run("pass", {}, {}) == ([], [])
    finally:
        await pool.close()


def test_a_worker_is_only_handed_out_once_it_has_answered():
    go(_warm_checks())
