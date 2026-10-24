"""A Shelly talks back over its socket only to a peer that has spoken first.

Gen2+ devices push NotifyStatus only to a client that has sent a frame with a
`src`. The adapter used to open the socket and listen in silence, so it heard
nothing and every change waited for the five-minute reconcile: on 24.10 the
heating fired the boiler relay at 17:32 and DIDA saw it on at 17:34, re-sending
`turn_on` every tick in between.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from dida_adapter_shelly.adapter import ShellyAdapter

DEV = "shelly1g4-boiler"


class _Ws:
    def __init__(self, frames):
        self.sent = []
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        if self._frames:
            return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(self._frames.pop(0)), None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)


class _Session:
    def __init__(self, ws):
        self.ws = ws

    def ws_connect(self, url, **kwargs):
        return self.ws


class _Bus:
    def __init__(self):
        self.states = []

    async def publish_state(self, update):
        self.states.append(update)


def _adapter(ws):
    adapter = ShellyAdapter()
    adapter._session = _Session(ws)
    adapter._bus = bus = _Bus()

    async def rpc(ip, method, params=None):
        return {"Shelly.GetDeviceInfo": {"id": DEV, "model": "S4SW-001X16EU"},
                "Shelly.GetConfig": {}}[method]

    adapter._rpc = rpc
    return adapter, bus


async def _listen(adapter):
    with pytest.raises(RuntimeError) as exc:
        await adapter._sync_and_listen("192.0.2.21")
    await asyncio.sleep(0)
    return str(exc.value)


async def test_the_socket_speaks_first_and_hears_the_change():
    ws = _Ws([
        {"id": 1, "src": DEV, "result": {"switch:0": {"id": 0, "output": False}}},
        {"src": DEV, "method": "NotifyStatus", "params": {"switch:0": {"id": 0, "output": True}}},
    ])
    adapter, bus = _adapter(ws)
    await _listen(adapter)
    assert ws.sent and ws.sent[0].get("src"), "without a src the device never pushes"
    assert [s.value for s in bus.states if s.capability == "on_off"] == [False, True]


async def test_a_refused_status_read_reconnects():
    ws = _Ws([{"id": 1, "src": DEV, "error": {"code": -103, "message": "busy"}}])
    adapter, bus = _adapter(ws)
    assert "Shelly.GetStatus" in await _listen(adapter)
    assert bus.states == []
