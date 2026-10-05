import json
from unittest.mock import AsyncMock

import pytest
from dida_adapter_presence import adapter as runtime
from dida_adapter_presence.adapter import PresenceAdapter


async def test_stationary_valid_reports_keep_coordinate_freshness_without_repeating_location(monkeypatch):
    adapter = PresenceAdapter()
    adapter._bus = AsyncMock()
    clock = [0]
    monkeypatch.setattr(runtime.time, "time_ns", lambda: clock[0])
    frame = json.dumps({"_type": "location", "lat": 45.0, "lon": 15.0, "acc": 10})
    for hour in range(4):
        clock[0] = hour * 3600 * 10**9
        await adapter._handle("owntracks/alex/phone", frame)
    updates = [call.args[0] for call in adapter._bus.publish_state.await_args_list]
    assert len([u for u in updates if u.capability == "location"]) == 1
    assert len([u for u in updates if u.capability == "latitude"]) == 4
    assert len([u for u in updates if u.capability == "longitude"]) == 4
    assert updates[-1].ts_ns == clock[0]
    assert updates[-1].ts_ns - updates[0].ts_ns > 2 * 3600 * 10**9


@pytest.mark.parametrize("values", [
    {"lat": 91}, {"lon": 181}, {"lat": True}, {"lat": float("nan")},
    {"acc": float("inf")}, {"acc": -1}, {"acc": 5000},
])
async def test_invalid_or_coarse_report_cannot_refresh_any_presence_value(values):
    adapter = PresenceAdapter()
    adapter._bus = AsyncMock()
    frame = {"_type": "location", "lat": 45.0, "lon": 15.0, "acc": 10} | values
    await adapter._handle("owntracks/alex/phone", json.dumps(frame))
    adapter._bus.publish_state.assert_not_awaited()
