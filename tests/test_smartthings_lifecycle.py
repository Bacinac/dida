import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from dida_adapter_smartthings.adapter import ReauthNeeded, SmartThingsAdapter
from dida_core import Command, CommandRejected


def grant(name, expires=1000):
    return json.dumps({"access_token": name, "refresh_token": f"{name}-refresh", "expires_at": time.time() + expires})


class Broker:
    def __init__(self, value):
        self.value = value

    async def call(self, op, **args):
        if op == "stored":
            return self.value
        assert op == "store_if_current"
        if self.value != args["current"]:
            return False
        self.value = args["value"]
        return True


async def adapter(value):
    result = SmartThingsAdapter()
    result.broker = Broker(value)
    await result._load_oauth()
    return result


async def test_disconnect_clears_valid_cached_tokens_sdk_and_routes():
    a = await adapter(grant("old"))
    a._st = object()
    a._routes["smartthings:old"] = {"device_id": "old"}
    a._state["smartthings:old"] = {"on_off": True}
    a._locations_synced = True
    a.broker.value = None
    with pytest.raises(ReauthNeeded):
        await a._ensure_token()
    assert a._tokens == {} and a._st is None and not a._routes and not a._state
    assert not a._locations_synced


async def test_new_connect_replaces_a_still_valid_previous_account():
    a = await adapter(grant("old"))
    a._st = object()
    a._routes["smartthings:old"] = {}
    a.broker.value = grant("new")
    await a._load_oauth()
    assert await a._ensure_token() == "new"
    assert a._st is None and not a._routes


@pytest.mark.parametrize("reconnect", [False, True])
async def test_refresh_in_flight_cannot_resurrect_or_replace_the_grant(reconnect):
    a = await adapter(grant("old", expires=-100))
    started, release = asyncio.Event(), asyncio.Event()

    class Response:
        status = 200

        async def __aenter__(self):
            started.set()
            await release.wait()
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            return json.dumps({"access_token": "rolled", "refresh_token": "rolled-refresh", "expires_in": 86400})

    a._session = SimpleNamespace(post=lambda *args, **kwargs: Response())
    a._client_id, a._client_secret = "client", "secret"
    refreshing = asyncio.create_task(a._ensure_token())
    await started.wait()
    replacement = grant("new") if reconnect else None
    a.broker.value = replacement
    release.set()
    with pytest.raises(ReauthNeeded, match="changed during refresh"):
        await refreshing
    assert a.broker.value == replacement
    assert not a._tokens


async def test_a_command_cannot_use_a_disconnected_sdk():
    a = await adapter(grant("old"))
    a._st = object()
    a._routes["smartthings:old"] = {}
    a.broker.value = None
    with pytest.raises(CommandRejected, match="not ready"):
        await a.handle_command(Command(entity_id="smartthings:old", capability="on_off", command="turn_on", ts_ns=1))
