"""What an adapter may read through its broker: its own namespace, the devices it
commands, and what `identity.READS` names. A compromised adapter must not read where
the people of the house are, nor another adapter's settings."""

from __future__ import annotations

import re

import pytest
from dida_api import broker as api
from dida_core import identity


class _Pool:
    """Records every query; answers none of them."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.queries.append((sql, args))
        return []

    async def fetchval(self, sql: str, *args):
        self.queries.append((sql, args))


def _namespaces(pool: _Pool) -> list[str]:
    (sql, args), = pool.queries
    n = re.search(r"split_part\(entity_id, ':', 1\) = ANY\(\$(\d+)::text\[\]\)", sql)
    assert n, sql
    return args[int(n.group(1)) - 1]


@pytest.mark.parametrize("op, args", [
    ("state", {"prefix": "presence:", "capabilities": ["latitude", "longitude"]}),
    ("state", {"entity_ids": ["presence:someone"]}),
    ("entities", {"prefix": "presence:"}),
    ("entities", {}),
])
async def test_an_adapter_reads_only_its_own_namespace(op, args):
    pool = _Pool()
    reply = await api.answer(api.Ctx(pool, "root"), "tuya", {"op": op, "args": args})
    assert reply == {"result": []}
    assert _namespaces(pool) == ["tuya"]


async def test_a_reader_named_in_reads_reaches_that_namespace():
    pool = _Pool()
    await api.answer(api.Ctx(pool, "root"), "notify",
                     {"op": "state", "args": {"prefix": "presence:", "capabilities": ["latitude"]}})
    assert "presence" in _namespaces(pool)


async def test_announce_reaches_the_speakers_it_commands():
    pool = _Pool()
    await api.answer(api.Ctx(pool, "root"), "announce",
                     {"op": "entities", "args": {"capability": "media_transport"}})
    assert set(_namespaces(pool)) == {"announce", *identity.COMMANDS["announce"]}


async def test_another_adapters_config_is_refused_before_the_database():
    pool = _Pool()
    reply = await api.answer(api.Ctx(pool, "root"), "tuya", {"op": "config", "args": {"of": "unifi"}})
    assert "not open to tuya" in reply["error"]
    assert pool.queries == []


def test_every_reader_and_namespace_it_reads_is_a_client():
    for reader, namespaces in identity.READS.items():
        assert reader in identity.CLIENTS
        assert namespaces <= identity.ADAPTERS
