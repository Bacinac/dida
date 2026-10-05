import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import msgspec
from dida_adapter_calendar import adapter as runtime
from dida_core import Bus, StateUpdate, validate_state


async def test_disable_and_reenable_publish_a_valid_clear_even_after_adapter_restart():
    adapter = runtime.CalendarAdapter()
    adapter._bus = AsyncMock()
    adapter.status = Mock()
    row = {"id": 7, "name": "Collection", "enabled": True,
           "params": {"recurrence_type": "daily", "start_date": "2020-01-01"}}
    adapter.broker = AsyncMock()
    adapter.broker.call.return_value = [row]
    await adapter._evaluate()
    await asyncio.sleep(0)
    row["enabled"] = False
    await adapter._evaluate()
    await asyncio.sleep(0)
    adapter._next.clear()
    await adapter._evaluate()
    await asyncio.sleep(0)
    row["enabled"] = True
    await adapter._evaluate()
    await asyncio.sleep(0)
    updates = [call.args[0] for call in adapter._bus.publish_state.await_args_list]
    dates = [update.value for update in updates if update.capability == "next_occurrence"]
    assert dates == [datetime.now(adapter._tz).date().isoformat(), "", "", datetime.now(adapter._tz).date().isoformat()]
    for update in updates:
        assert validate_state(update.capability, update.value) == update.value
        assert msgspec.msgpack.decode(Bus.encode_event(update), type=StateUpdate).value == update.value


async def test_consumed_one_shot_clears_the_previous_next_date(monkeypatch):
    day = [datetime(2026, 10, 5)]

    class Clock:
        @staticmethod
        def now(tz):
            return day[0].replace(tzinfo=tz)

    monkeypatch.setattr(runtime, "datetime", Clock)
    adapter = runtime.CalendarAdapter()
    adapter._bus = AsyncMock()
    adapter.status = Mock()
    adapter.broker = AsyncMock()
    adapter.broker.call.return_value = [{"id": 7, "name": "Once", "enabled": True,
                                        "params": {"recurrence_type": "once", "start_date": "2026-10-05"}}]
    await adapter._evaluate()
    await asyncio.sleep(0)
    day[0] = datetime(2026, 10, 6)
    await adapter._evaluate()
    await asyncio.sleep(0)
    dates = [call.args[0].value for call in adapter._bus.publish_state.await_args_list
             if call.args[0].capability == "next_occurrence"]
    assert dates == ["2026-10-05", ""]
