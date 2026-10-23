"""The poll: what OPUS · Player says is on reaches the house.

23.10.2026: a second method took the entity announcement's name, every read died
on a TypeError before publishing, and the Living room card stood on a song that
had ended hours before while the DAC played something else."""
from __future__ import annotations

import pytest
from dida_adapter_opus.adapter import OpusAdapter
from dida_adapter_opus.mapping import DAC
from dida_core.health import StatusReporter

URL, TOKEN = "http://192.168.1.103:8098", "house-token"


class Answer:
    def __init__(self, body):
        self.status, self.body = 200, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.body


class Session:
    def __init__(self, dac):
        self.dac = dac

    def get(self, url, headers=None):
        return Answer(self.dac if url.endswith("/api/dac/now") else {"listening": False})


class Bus:
    def __init__(self):
        self.entities, self.states = [], {}

    async def publish_entity(self, info):
        self.entities.append(info)

    async def publish_state(self, update):
        self.states[(update.entity_id, update.capability)] = update.value

    async def publish_reachability(self, event):
        pass


def rig(dac):
    a = OpusAdapter()
    a.status = StatusReporter("opus")
    a._bus, a._session = Bus(), Session(dac)
    return a


def song(title, position):
    return {"name": "iFi DAC", "transport": "playing", "kind": "track", "title": title,
            "artist": "R.E.M.", "position": position, "duration": 252}


@pytest.mark.asyncio
async def test_the_dac_read_reaches_the_house():
    a = rig(song("Radio Song", 12))
    assert await a._read(URL, TOKEN, a._outputs[DAC]) == ""
    assert a._bus.entities[0].entity_id == DAC and a._bus.entities[0].name == "iFi DAC"
    assert a._bus.states[(DAC, "media_title")] == "Radio Song"
    assert a._bus.states[(DAC, "media_position")] == 12


@pytest.mark.asyncio
async def test_the_next_song_replaces_the_last():
    a = rig(song("Sparrow", 150))
    await a._read(URL, TOKEN, a._outputs[DAC])
    a._session.dac = song("Radio Song", 3)
    await a._read(URL, TOKEN, a._outputs[DAC])
    assert a._bus.states[(DAC, "media_title")] == "Radio Song"
    assert a._bus.states[(DAC, "media_position")] == 3
    assert len(a._bus.entities) == 1
