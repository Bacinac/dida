"""Shared ClickHouse client — one place that reads the CLICKHOUSE_* env and
builds an async client.

Both the engine (history writer) and the API (history reader) used to hand-roll
the identical `get_async_client(host=…, port=…, …)` block. Reading the env here
fixes that in one place — the Postgres `pg_pool` pattern, for the firehose store.
clickhouse-connect is imported lazily so core stays light for the many adapters
that never touch ClickHouse.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clickhouse_connect.driver import AsyncClient


async def ch_client() -> AsyncClient:
    """Async ClickHouse client from CLICKHOUSE_{HOST,PORT,USER,PASSWORD,DB}.

    Password has no default — a missing one fails loud (the firehose store must
    not connect with a guessed credential and mask a misconfiguration)."""
    import clickhouse_connect

    return await clickhouse_connect.get_async_client(
        host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "dida"),
        password=os.environ["CLICKHOUSE_PASSWORD"],  # no default — fail loud
        database=os.environ.get("CLICKHOUSE_DB", "dida"),
    )
