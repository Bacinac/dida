import json
from types import SimpleNamespace

import dida_runner.runner as runner_mod
import pytest
from dida_adapter_cloudflare.adapter import CloudflareAdapter
from dida_runner.runner import TUNNEL_CONNECTORS, Runner


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_partial_roll_restores_effective_connector_configuration(tmp_path, monkeypatch, rollback_fails):
    config = tmp_path / "config.yml"
    config.write_text("OLD")
    states = dict.fromkeys(TUNNEL_CONNECTORS, "OLD")
    calls = []
    runner = Runner(tmp_path)

    async def catalog():
        return {"cloudflare-tunnel": TUNNEL_CONNECTORS}

    async def compose(action, name, **kwargs):
        assert action == "restart"
        calls.append(name)
        if len(calls) == 2 or (rollback_fails and len(calls) == 3):
            raise RuntimeError("restart failed")
        states[name] = config.read_text()

    async def sleep(seconds):
        pass

    async def request(subject, payload, **kwargs):
        assert json.loads(payload) == {"action": "roll"}
        return SimpleNamespace(data=json.dumps(await runner._roll(TUNNEL_CONNECTORS)).encode())

    monkeypatch.setattr(runner, "_catalog", catalog)
    monkeypatch.setattr(runner, "_compose", compose)
    monkeypatch.setattr(runner_mod.asyncio, "sleep", sleep)
    adapter = CloudflareAdapter()
    adapter._config = str(config)
    adapter._bus = SimpleNamespace(nc=SimpleNamespace(request=request))
    monkeypatch.setattr(adapter, "_mode", lambda: "tunnel")
    monkeypatch.setattr(adapter, "_write", lambda rows: config.write_text("NEW"))
    result = await adapter._commit([], "new.example.com")
    assert result["ok"] is False and result["applied"] is False
    assert config.read_text() == "OLD"
    assert result["partial_apply"] is rollback_fails
    if rollback_fails:
        assert states == {TUNNEL_CONNECTORS[0]: "NEW", TUNNEL_CONNECTORS[1]: "OLD"}
        assert "rollback failed" in result["error"]
        assert adapter._tunnel_apply["partial_apply"] is True
    else:
        assert set(states.values()) == {"OLD"}
        assert result["rollback"]["applied"] is True
        assert calls == [*TUNNEL_CONNECTORS, *TUNNEL_CONNECTORS]
