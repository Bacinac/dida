from __future__ import annotations

import time
from uuid import uuid4

import pytest
from dida_core import apply_ch_migrations, ch_client
from dida_engine.history import HistoryWriter


@pytest.fixture
async def client():
    conn = await ch_client()
    await apply_ch_migrations(conn, "/w/ch/migrations")
    yield conn
    await conn.close()


class LostResponse:
    def __init__(self, client):
        self.client = client
        self.closed = False

    async def insert(self, *args, **kwargs):
        await self.client.insert(*args, **kwargs)
        raise RuntimeError("committed insert response was lost")

    async def close(self):
        self.closed = True


def ready(writer, client):
    writer._client = client
    writer._schema_ready = True
    writer._next_day_tz_check = float("inf")


async def test_a_committed_insert_with_lost_response_is_not_repeated_in_raw_or_rollups(client):
    entity = "audit:" + uuid4().hex
    writer = HistoryWriter(None)
    writer.enqueue(entity, "temperature", "audit", 10, time.time_ns())
    lost = LostResponse(client)
    ready(writer, lost)
    assert await writer.flush() is False
    writer.enqueue(entity, "temperature", "audit", 20, time.time_ns())
    ready(writer, client)
    assert await writer.flush() is True
    assert writer.stats()["buffered"] == 1
    assert await writer.flush() is True

    raw = await client.query("SELECT value_num FROM state_history WHERE entity_id = {entity:String} ORDER BY value_num",
                             parameters={"entity": entity})
    assert raw.result_rows == [(10.0,), (20.0,)]
    for table in ("state_history_1h", "state_history_1d"):
        rollup = await client.query(
            f"SELECT countMerge(cnt), avgMerge(avg_v) FROM {table} WHERE entity_id = {{entity:String}}",  # noqa: S608
            parameters={"entity": entity})
        assert rollup.result_rows == [(2, 15.0)]
    assert lost.closed
