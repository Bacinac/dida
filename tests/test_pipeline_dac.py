"""The signal path's reading of OPUS · Player's DAC: MPD's own format string and
the player's account of the DAC, turned into the nodes the card draws."""

import pytest
from dida_api import opus, pipeline


def test_mpd_formats_read_the_way_the_card_prints_them():
    assert pipeline._mpd_format("44100:16:2") == ("44.1 kHz / 16 bit", 2)
    assert pipeline._mpd_format("96000:24:2") == ("96 kHz / 24 bit", 2)
    assert pipeline._mpd_format("44100:f:2") == ("44.1 kHz / float", 2)
    assert pipeline._mpd_format("dsd64:2") == ("DSD64", 2)
    assert pipeline._mpd_format("") == (None, None)


@pytest.mark.asyncio
async def test_the_dac_without_a_mixer_is_exclusive(monkeypatch):
    async def dac_now(pool):
        return {"name": "iFi", "format": "44100:16:2", "bitrate": 697,
                "replay_gain": "off", "crossfade": 0}

    monkeypatch.setattr(opus, "dac_now", dac_now)
    live = await pipeline._measure_dac(None)
    assert live["dac"] == "iFi" and live["format"] == "44.1 kHz / 16 bit"
    assert live["bitrate"] == "697 kbps" and live["channels"] == 2
    assert live["exclusive"] and live["volume_control_disabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"replay_gain": "track"}, {"crossfade": 3}])
async def test_replay_gain_or_crossfade_is_not_exclusive(monkeypatch, changed):
    async def dac_now(pool):
        return {"name": "iFi", "format": "44100:16:2", "replay_gain": "off", "crossfade": 0, **changed}

    monkeypatch.setattr(opus, "dac_now", dac_now)
    live = await pipeline._measure_dac(None)
    assert not live["exclusive"] and live["volume_control_disabled"]


@pytest.mark.asyncio
async def test_a_software_volume_is_not_exclusive(monkeypatch):
    async def dac_now(pool):
        return {"name": "iFi", "format": "44100:16:2", "volume": 80, "replay_gain": "off", "crossfade": 0}

    monkeypatch.setattr(opus, "dac_now", dac_now)
    assert not (await pipeline._measure_dac(None))["exclusive"]


@pytest.mark.asyncio
async def test_the_players_playback_check_reaches_the_signal_path(monkeypatch):
    health = {"state": "recovering", "attempts": 2,
              "output": {"state": "RUNNING", "card": "Audio", "hw_ptr": 1}}

    async def dac_now(pool):
        return {"name": "iFi", "format": "44100:16:2", "replay_gain": "off", "crossfade": 0,
                "health": health}

    monkeypatch.setattr(opus, "dac_now", dac_now)
    assert (await pipeline._measure_dac(None))["health"] == health


@pytest.mark.asyncio
async def test_a_player_that_does_not_answer_is_no_measurement(monkeypatch):
    async def dac_now(pool):
        raise opus.OpusUnavailable("down")

    monkeypatch.setattr(opus, "dac_now", dac_now)
    assert await pipeline._measure_dac(None) is None


def test_the_colours_are_the_zen_dac_v2_led():
    assert pipeline._rate_class(44.1, None) == "r48"
    assert pipeline._rate_class(48, None) == "r48"
    assert pipeline._rate_class(96, None) == "hires"
    assert pipeline._rate_class(192, None) == "hires"
    assert pipeline._rate_class(384, None) == "hires"
    assert pipeline._rate_class(768, None) is None
    assert pipeline._rate_class(None, 64) == "dsd"
    assert pipeline._rate_class(None, 128) == "dsd"
    assert pipeline._rate_class(None, 256) == "dsd256"


def test_dsd_is_read_the_way_either_renderer_says_it():
    assert pipeline._dsd_multiple("DSD64") == 64
    assert pipeline._dsd_multiple("DSD256") == 256
    assert pipeline._dsd_multiple("2.82 MHz / 1 bit") == 64
    assert pipeline._dsd_multiple("5.64 MHz / 1 bit") == 128
    assert pipeline._dsd_multiple("44.1 kHz / 16 bit") is None


def test_format_match_compares_dsd_families_not_khz_bit_pairs():
    assert pipeline._format_match("DSD64", "DSD64") is True
    assert pipeline._format_match("DSD64", "DSD128") is False
    # a DSD source the renderer opened as plain PCM is a real mismatch, not
    # "unmeasured" — the numeric branch would wrongly find no numbers to compare
    assert pipeline._format_match("DSD64", "44.1 kHz / 16 bit") is False
    assert pipeline._format_match("FLAC 96kHz/24bit", "96 kHz / 24 bit") is True
    assert pipeline._format_match("FLAC 96kHz/24bit", "48 kHz / 24 bit") is False


def test_format_match_is_unmeasured_with_nothing_to_compare():
    assert pipeline._format_match("", "DSD64") is None
    assert pipeline._format_match("DSD64", None) is None
    assert pipeline._format_match("DSD64", "") is None
