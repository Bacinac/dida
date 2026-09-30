"""The alarm's AUDIBLE last mile — the sibling of notify.

Same failure shape: everything upstream is observable (the command is in the
audit trail, the rule firing is in the journal), so when this breaks the house
simply stays quiet and every surface still reads healthy.

The chunking is the part with history. translate_tts rejects a query longer than
~200 characters with an HTTP 400, so a long announcement used to be built, sent,
refused, and SILENTLY never spoken — the one length of message you most want
spoken aloud (a full alarm sentence) was the one that could not be.
"""
from __future__ import annotations

import asyncio

import pytest
from dida_adapter_announce.adapter import _TTS_LIMIT, AnnounceAdapter, _parse_targets, _tts_chunks
from dida_core import Command, CommandRejected


def cmd(entity_id, command="say", capability="announce", source="user:marko", **args):
    return Command(entity_id=entity_id, capability=capability, command=command,
                   ts_ns=1, args=args, source=source)


def _adapter(targets=None):
    a = AnnounceAdapter()
    a._targets = targets if targets is not None else {}
    a._lang = "hr"
    a._bus = object()
    a.played, a.sequences, a.states = [], [], []

    async def play(speaker, lang, chunk, source):
        a.played.append((speaker, lang, chunk, source))

    async def play_seq(speaker, lang, chunks, source):
        a.sequences.append((speaker, lang, list(chunks), source))
        for c in chunks:
            await play(speaker, lang, c, source)

    async def pub(eid, text):
        a.states.append((eid, text))

    a._play, a._play_sequence, a._publish_state = play, play_seq, pub
    return a


# --- chunking: the bug this logic exists for ---------------------------------

def test_a_short_announcement_is_one_clip():
    assert _tts_chunks("Vrata su otvorena") == ["Vrata su otvorena"]


def test_a_long_announcement_is_split_instead_of_silently_refused():
    text = " ".join(["riječ"] * 80)          # ~480 chars
    chunks = _tts_chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= _TTS_LIMIT for c in chunks), "every clip fits the endpoint's limit"


def test_splitting_happens_on_WORD_boundaries_and_loses_nothing():
    text = " ".join(f"w{i}" for i in range(200))
    chunks = _tts_chunks(text)
    assert " ".join(chunks) == text, "the sentence survives the split intact"
    for c in chunks:
        assert not c.startswith(" ") and not c.endswith(" ")


def test_a_single_over_long_token_is_hard_split_as_a_last_resort():
    # No word boundary to use — better a clipped word than nothing spoken.
    word = "x" * (_TTS_LIMIT * 2 + 10)
    chunks = _tts_chunks(word)
    assert len(chunks) == 3
    assert all(len(c) <= _TTS_LIMIT for c in chunks)
    assert "".join(chunks) == word


def test_chunking_never_returns_an_empty_list():
    # An empty list downstream means the loop body never runs → silence.
    assert _tts_chunks("") == [""[:_TTS_LIMIT]] or _tts_chunks("") == [""]
    assert _tts_chunks("   ") != []


# --- targets ------------------------------------------------------------------

def test_targets_use_EQUALS_because_speaker_ids_contain_a_colon():
    assert _parse_targets("kitchen=cast:kitchen_speaker,bed=dlna:zen") == {
        "announce:kitchen": "cast:kitchen_speaker", "announce:bed": "dlna:zen"}


def test_a_malformed_target_is_skipped_not_crashed():
    assert _parse_targets("a=cast:x,,junk,=y,z=,b=cast:w") == {
        "announce:a": "cast:x", "announce:b": "cast:w"}


# --- routing ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_say_reaches_its_speaker_with_the_configured_language():
    a = _adapter({"announce:kitchen": "cast:kitchen_speaker"})
    await a.handle_command(cmd("announce:kitchen", text="Vrata su otvorena"))
    assert a.played == [("cast:kitchen_speaker", "hr", "Vrata su otvorena", "user:marko")]


@pytest.mark.asyncio
async def test_the_command_may_override_the_language():
    a = _adapter({"announce:k": "cast:s"})
    await a.handle_command(cmd("announce:k", text="Door open", language="en"))
    assert a.played[0][1] == "en"


@pytest.mark.asyncio
async def test_text_falls_back_to_value_for_the_generic_rule_editor():
    a = _adapter({"announce:k": "cast:s"})
    await a.handle_command(cmd("announce:k", value="from the generic form"))
    assert a.played[0][2] == "from the generic form"


@pytest.mark.asyncio
async def test_the_INITIATOR_is_propagated_not_the_relay():
    """Exercises the REAL _play against a stub BUS.

    An earlier version of this test used the same stubbed _play as the routing
    tests above — so it asserted what the stub had recorded and passed happily
    with `source="announce"` hardcoded in the real code. A test that cannot fail
    is worse than none: it reports coverage of a claim it never checks. The audit
    trail must keep WHO asked, not the relay that carried it.
    """
    class StubBus:
        def __init__(self):
            self.commands = []

        async def publish_command(self, c):
            self.commands.append(c)

    a = AnnounceAdapter()
    a._bus = StubBus()
    await a._play("cast:s", "hr", "hi", "automation:12:Evening")
    (sent,) = a._bus.commands
    assert sent.source == "automation:12:Evening", "the initiator survives the relay"
    assert sent.entity_id == "cast:s"
    assert sent.capability == "media_transport" and sent.command == "play_media"
    assert sent.args["uri"].startswith("https://translate.google.com/")
    assert "hi" in sent.args["uri"]


@pytest.mark.asyncio
async def test_a_long_announcement_plays_its_clips_IN_ORDER():
    a = _adapter({"announce:k": "cast:s"})
    text = " ".join(["riječ"] * 80)
    await a.handle_command(cmd("announce:k", text=text))
    # The multi-clip path is SPAWNED so the handler returns promptly — that is the
    # point of it — so the test has to let the loop run it.
    for _ in range(4):
        await asyncio.sleep(0)
    assert a.sequences, "several clips go through the sequencer, not a single play"
    spoken = " ".join(c for _s, _l, c, _src in a.played)
    assert spoken == text, "nothing is dropped between the clips"


# --- the failures that must stay LOUD or harmless ------------------------------

@pytest.mark.asyncio
async def test_an_unroutable_announcement_is_rejected_not_swallowed():
    a = _adapter({"announce:kitchen": "cast:s"})
    with pytest.raises(CommandRejected, match="no target"):
        await a.handle_command(cmd("announce:ghost", text="nobody hears this"))
    assert a.played == []


@pytest.mark.asyncio
async def test_an_empty_announcement_says_nothing_rather_than_an_empty_clip():
    a = _adapter({"announce:k": "cast:s"})
    await a.handle_command(cmd("announce:k", text="   "))
    assert a.played == []


@pytest.mark.asyncio
async def test_a_command_the_capability_model_rejects_never_speaks():
    a = _adapter({"announce:k": "cast:s"})
    await a.handle_command(cmd("announce:k", capability="on_off", command="turn_on", text="x"))
    assert a.played == []


@pytest.mark.asyncio
async def test_the_spoken_text_is_published_as_the_entity_value():
    # It is what makes announce:* register and appear in the editor at all.
    a = _adapter({"announce:k": "cast:s"})
    await a.handle_command(cmd("announce:k", text="Dobar dan"))
    assert a.states == [("announce:k", "Dobar dan")]
