"""Regression tests for the Layer-2 Starlark worker.

`starlark_worker` is the process the sandbox pool spawns: it speaks a length-prefixed
JSON wire protocol on stdin/stdout, compiles + runs untrusted user Starlark, and turns
the result (or any syntax/runtime error) into a structured response WITHOUT crashing —
a malformed request must never take the warm worker down. These tests drive that real
protocol IN-PROCESS (frames through `_read_frame`/`_write_frame`, dispatch through
`_handle`, the full read→handle→write loop through `main`), asserting on the exact
emitted-command payloads and error `kind`s rather than merely "no exception".

Run directly in the automation image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/automation:latest \
      -c "python -m pytest tests/test_starlark_worker.py"
"""
import io
import json
import struct
import sys
import types

from dida_automation import starlark_worker as worker


def _frame(payload: bytes) -> bytes:
    """Encode a body the way the pool does: 4-byte big-endian length + JSON."""
    return struct.pack(">I", len(payload)) + payload


def _read_response(buf: io.BytesIO) -> dict:
    """Pull one response frame back off a worker's output buffer."""
    raw = worker._read_frame(buf)
    assert raw is not None, "expected a response frame, got EOF"
    return json.loads(raw)


# --- _handle: the request dispatcher ---------------------------------------


def test_handle_valid_script_emits_command():
    resp = worker._handle({"script": 'turn_on("mqtt:light")', "event": {}, "snapshot": []})
    assert resp == {
        "ok": True,
        "emitted": [["mqtt:light", "on_off", "turn_on", {}]],
        "states": [],
    }, "a valid script returns ok + the emitted command as [eid, cap, cmd, args]"


def test_handle_set_state_returns_states():
    resp = worker._handle({"script": 'set_state("virtual:night", "on_off", True)', "snapshot": []})
    assert resp["ok"] is True
    assert resp["emitted"] == [], "set_state emits no command"
    assert resp["states"] == [["virtual:night", "on_off", True]], "set_state writes a derived state"


def test_handle_branches_on_injected_event_and_snapshot():
    script = 'if event["value"] and state("sensor:lux", "illuminance") < 10:\n    turn_on("mqtt:lamp")'
    resp = worker._handle(
        {"script": script, "event": {"value": True}, "snapshot": [["sensor:lux", "illuminance", 5]]}
    )
    assert resp["emitted"] == [["mqtt:lamp", "on_off", "turn_on", {}]], \
        "snapshot rows rebuild into state() lookups and the branch fires"

    resp = worker._handle(
        {"script": script, "event": {"value": True}, "snapshot": [["sensor:lux", "illuminance", 500]]}
    )
    assert resp["emitted"] == [], "condition not met → nothing emitted"


def test_handle_syntax_error_is_reported_not_raised():
    # `while` is not in the Starlark grammar — a script structurally cannot spin.
    resp = worker._handle({"script": "while True:\n    pass", "event": {}, "snapshot": []})
    assert resp["ok"] is False, "a syntax error is reported, not raised"
    assert resp["kind"] == "syntax", "compile failure is classified as a syntax error"
    assert "emitted" not in resp, "a rejected script never produces commands"


def test_handle_runtime_error_is_reported_not_raised():
    # Compiles fine, blows up at eval (division by zero via the injected event so it
    # can't be constant-folded away). The worker catches it and reports kind=error.
    resp = worker._handle({"script": "x = 100 // event['value']", "event": {"value": 0}, "snapshot": []})
    assert resp["ok"] is False, "a runtime error is reported, not raised"
    assert resp["kind"] == "error", "an eval-time failure is classified as a runtime error"


# --- wire framing: _read_frame / _write_frame -----------------------------


def test_frame_write_read_roundtrip():
    buf = io.BytesIO()
    worker._write_frame(buf, b"payload-123")
    buf.seek(0)
    assert worker._read_frame(buf) == b"payload-123", "a written frame reads back byte-identical"


def test_read_frame_eof_returns_none():
    # An empty / short header means the pool closed our stdin → clean shutdown, not a crash.
    assert worker._read_frame(io.BytesIO(b"")) is None, "no bytes → EOF sentinel"
    assert worker._read_frame(io.BytesIO(b"\x00\x00")) is None, "truncated header → EOF sentinel"


def test_read_frame_truncated_body_returns_none():
    # Header claims 100 bytes but only 3 follow → treat as EOF rather than block/garble.
    truncated = struct.pack(">I", 100) + b"abc"
    assert worker._read_frame(io.BytesIO(truncated)) is None, "short body → EOF sentinel"


# --- main: the full read→handle→write loop --------------------------------


def _run_main(monkeypatch, in_bytes: bytes) -> io.BytesIO:
    out_buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(in_bytes)))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out_buf))
    worker.main()  # loops until stdin EOF, then returns cleanly
    out_buf.seek(0)
    return out_buf


def test_main_processes_request_then_exits_on_eof(monkeypatch):
    req = json.dumps({"script": 'turn_off("mqtt:x")', "event": {}, "snapshot": []}).encode()
    out_buf = _run_main(monkeypatch, _frame(req))
    assert _read_response(out_buf) == {
        "ok": True,
        "emitted": [["mqtt:x", "on_off", "turn_off", {}]],
        "states": [],
    }, "main frames the request in, dispatches it, and frames the response out"
    assert worker._read_frame(out_buf) is None, "no second response — the loop stopped at EOF"


def test_main_survives_malformed_request_and_keeps_serving(monkeypatch):
    # A non-JSON body must NOT kill the warm worker: it reports kind=error and the
    # very next (valid) frame is still served.
    bad = _frame(b"this is not json {")
    good = _frame(json.dumps({"script": 'turn_on("mqtt:y")', "event": {}, "snapshot": []}).encode())
    out_buf = _run_main(monkeypatch, bad + good)

    err = _read_response(out_buf)
    assert err["ok"] is False and err["kind"] == "error", "malformed request → structured error"
    assert err["error"].startswith("worker:"), "the outer guard tags its own failures"

    ok = _read_response(out_buf)
    assert ok["emitted"] == [["mqtt:y", "on_off", "turn_on", {}]], \
        "the worker kept serving — one bad request did not take it down"


# --- mode "value": computed-helper pure eval (no actions, returns a value) ------
def test_handle_value_mode_returns_computed_value():
    # A computed helper reads state() and assigns `value`. mode 'value' returns it.
    resp = worker._handle({
        "script": 'value = "night" if state("astro:sun", "sun_elevation") < -6 else "day"',
        "snapshot": [["astro:sun", "sun_elevation", -8.0]], "mode": "value",
    })
    assert resp == {"ok": True, "value": "night"}, "reads state, computes, returns the value"
    resp = worker._handle({
        "script": 'value = "night" if state("astro:sun", "sun_elevation") < -6 else "day"',
        "snapshot": [["astro:sun", "sun_elevation", 12.0]], "mode": "value",
    })
    assert resp == {"ok": True, "value": "day"}, "the other branch"


def test_value_mode_free_form_value():
    # 'text' helper: an arbitrary value composed from statuses (fully flexible).
    resp = worker._handle({
        "script": 'value = "Marko je " + state("virtual:daynight", "enum")',
        "snapshot": [["virtual:daynight", "enum", "night"]], "mode": "value",
    })
    assert resp == {"ok": True, "value": "Marko je night"}


def test_value_mode_cannot_command_a_device():
    # THE loop-safety guarantee: a helper has NO action builtins. `turn_on` is not
    # even defined in value mode, so a helper structurally cannot drive a device.
    resp = worker._handle({"script": 'turn_on("mqtt:light")\nvalue = 1', "snapshot": [], "mode": "value"})
    assert resp["ok"] is False, "calling an action builtin fails — it does not exist for a helper"
    assert resp["kind"] == "error"
    resp = worker._handle({"script": 'set_state("derived:x", "boolean", True)\nvalue = 1',
                           "snapshot": [], "mode": "value"})
    assert resp["ok"] is False, "set_state is unavailable too — a helper writes only its own value"


def test_value_mode_missing_value_is_an_error():
    # A helper script that never assigns `value` is malformed — reported, not crashing.
    resp = worker._handle({"script": 'x = 5', "snapshot": [], "mode": "value"})
    assert resp["ok"] is False and resp["kind"] == "error", "no `value =` → a clear error"
