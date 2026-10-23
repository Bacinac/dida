"""A sentence over the music on OPUS's DAC: the sound travels, never its address."""
from __future__ import annotations

import pytest
from dida_adapter_opus.adapter import OpusAdapter
from dida_core import Command, CommandRejected

CLIP = "https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=hr&q=Zvono"


class Answer:
    def __init__(self, status=200, body=b""):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def content(self):
        answer = self

        class Content:
            async def read(self, n):
                return answer.body[:n]
        return Content()

    async def text(self):
        return self.body.decode()


class Session:
    def __init__(self, sound=b"ID3 speech", opus=200):
        self.sound, self.opus = sound, opus
        self.fetched, self.posted = [], []

    def get(self, url):
        self.fetched.append(url)
        return Answer(200, self.sound)

    def post(self, url, data=None, headers=None, json=None):
        self.posted.append((url, data, headers))
        return Answer(self.opus, b"MPD refused playid" if self.opus >= 400 else b'{"ok":true}')


def adapter(session):
    a = OpusAdapter()
    a._session = session

    async def where():
        return "http://192.168.1.103:8098", "house-token"
    a._where = where
    return a


def say(entity_id):
    return Command(entity_id=entity_id, capability="media_transport", command="play_media", ts_ns=1,
                   args={"uri": CLIP, "mime": "audio/mpeg", "announce": True}, source="automation:56:DOORBELL-Ring")


@pytest.mark.asyncio
async def test_the_dac_is_handed_the_sound_not_the_address():
    session = Session()
    await adapter(session).handle_command(say("opus:dac"))
    assert session.fetched == [CLIP]
    ((url, data, headers),) = session.posted
    assert url == "http://192.168.1.103:8098/api/dac/announce"
    assert data == b"ID3 speech"
    assert headers["X-OPUS-Token"] == "house-token" and headers["Content-Type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_the_television_does_not_speak_over_its_programme():
    session = Session()
    with pytest.raises(CommandRejected):
        await adapter(session).handle_command(say("opus:tv"))
    assert session.fetched == [] and session.posted == []


@pytest.mark.asyncio
async def test_a_refusal_from_the_player_is_said_not_swallowed():
    with pytest.raises(RuntimeError, match="502"):
        await adapter(Session(opus=502)).handle_command(say("opus:dac"))


@pytest.mark.asyncio
async def test_no_sound_is_nothing_to_hand_the_dac():
    session = Session(sound=b"")
    with pytest.raises(CommandRejected):
        await adapter(session).handle_command(say("opus:dac"))
    assert session.posted == []
