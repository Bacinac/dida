"""The virtual adapter announces each helper before its first state, so a helper
made while the engine runs gets its device type (and a switch reaches the Matter
bridge) at once rather than at the engine's next start."""

from __future__ import annotations

import asyncio

from dida_adapter_virtual.adapter import VirtualAdapter
from dida_core import EntityInfo, StateUpdate


class _Bus:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def publish_entity(self, info: EntityInfo) -> None:
        self.sent.append(info)

    async def publish_state(self, update: StateUpdate) -> None:
        self.sent.append(update)


class _Broker:
    def __init__(self, helpers: list[dict]) -> None:
        self.helpers = helpers
        self.state: list[dict] = []

    async def call(self, method: str, **_kw):
        if method == "virtual_entities":
            return self.helpers
        return self.state


def _helper(entity_id: str, capability: str, name: str, options=None, category="control") -> dict:
    return {"entity_id": entity_id, "name": name, "capability": capability,
            "options": options, "category": category, "default_value": None}


def _adapter(helpers: list[dict]) -> tuple[VirtualAdapter, _Bus, _Broker]:
    adapter, bus, broker = VirtualAdapter(), _Bus(), _Broker(helpers)
    adapter._bus = bus
    adapter.broker = broker
    return adapter, bus, broker


def test_a_new_helper_is_announced_before_its_state_and_only_once():
    adapter, bus, broker = _adapter([_helper("virtual:voice_show_ema", "on_off", "Voice Show Ema")])

    asyncio.run(adapter._reload())
    info, seed = bus.sent
    assert isinstance(info, EntityInfo) and isinstance(seed, StateUpdate)
    assert (info.entity_id, info.adapter, info.capabilities, info.name, info.category) == (
        "virtual:voice_show_ema", "virtual", ["on_off"], "Voice Show Ema", "control")
    assert (seed.capability, seed.value) == ("on_off", False)

    broker.state = [{"entity_id": "virtual:voice_show_ema", "capability": "on_off", "value": False}]
    bus.sent.clear()
    asyncio.run(adapter._reload())
    assert bus.sent == []


def test_a_renamed_helper_is_announced_again_with_its_options_capability():
    adapter, bus, broker = _adapter([_helper("virtual:mode", "enum", "Mode", ["Day", "Night"])])
    asyncio.run(adapter._reload())
    broker.state = [{"entity_id": "virtual:mode", "capability": "enum", "value": "Day"}]

    broker.helpers = [_helper("virtual:mode", "enum", "House mode", ["Day", "Night"])]
    bus.sent.clear()
    asyncio.run(adapter._reload())
    [info] = [m for m in bus.sent if isinstance(m, EntityInfo)]
    assert (info.name, info.capabilities) == ("House mode", ["enum", "enum_options"])
