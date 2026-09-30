"""The OPUS adapter's translation between OPUS · Player's account of its
outputs and the house's media capabilities, and back into orders."""

import json

from dida_adapter_opus.mapping import (
    CONTROLS,
    order_from_address,
    order_from_media,
    order_from_queue,
    quality,
    read_dac,
    read_now,
)


def test_a_television_that_is_not_listening_is_idle_with_nothing_on_it():
    said = read_now({"listening": False, "transport": "playing", "title": "Stale"})
    assert said["media_transport"] == "idle"
    assert said["media_title"] == "" and said["media_source"] == ""


def test_the_player_open_with_nothing_on_is_stopped_not_idle():
    # the house offers to put something on only where something can be sent
    said = read_now({"listening": True, "transport": "idle", "title": "Left over"})
    assert said["media_transport"] == "stopped"
    assert said["media_title"] == ""


def test_a_song_on_the_television_is_the_librarys():
    said = read_now({"listening": True, "transport": "playing", "kind": "track",
                     "title": "Hotel California", "artist": "Eagles", "album": "Hotel California",
                     "cover_url": "https://cdn/x.jpg", "position": 12.7, "duration": 391})
    assert said == {
        "media_transport": "playing", "media_title": "Hotel California", "media_artist": "Eagles",
        "media_album": "Hotel California", "media_art": "https://cdn/x.jpg",
        "media_source": "library", "media_position": 12, "media_duration": 391,
    }


def test_a_station_is_not_the_houses_radio():
    # "radio" would send next/previous to the house's tuner, which plays elsewhere
    said = read_now({"listening": True, "transport": "playing", "kind": "station", "title": "Yammat FM"})
    assert said["media_source"] == "url"


def test_a_film_is_neither_a_record_nor_a_station():
    said = read_now({"listening": True, "transport": "paused", "kind": "movie", "title": "Heat",
                     "position": None, "duration": "not a number"})
    assert said["media_source"] == "external"
    assert said["media_transport"] == "paused"
    assert said["media_position"] == 0 and said["media_duration"] == 0


def test_an_unknown_transport_is_not_passed_through():
    assert read_now({"listening": True, "transport": "rewinding", "kind": "track"})["media_transport"] == "stopped"


def test_a_queue_is_played_by_the_librarys_ids_not_by_its_tickets():
    queue = [
        {"id": 11731, "uri": "http://x/api/play/track/11731/stream?ticket=t", "title": "A",
         "artist": "B", "album": "C", "art": "https://cdn/a.jpg", "codec": "flac"},
        {"id": 11732, "uri": "http://x/api/play/track/11732/stream?ticket=t", "title": "D"},
    ]
    order = order_from_queue({"queue": json.dumps(queue), "start": 1})
    assert order == {"tracks": [
        {"id": 11731, "title": "A", "artist": "B", "album": "C", "cover_url": "https://cdn/a.jpg",
         "codec": "flac", "url": None},
        {"id": 11732, "title": "D", "artist": "", "album": "", "cover_url": None, "codec": None, "url": None},
    ], "start": 1}


def test_a_ticket_without_an_id_is_refused_rather_than_guessed():
    queue = [{"uri": "http://x/api/play/track/9/stream?ticket=t", "title": "No id"}]
    assert order_from_queue({"queue": json.dumps(queue)}) is None


def test_a_queue_row_with_only_an_address_is_a_station():
    queue = [{"uri": "https://stream.yammat.fm/radio/8000/yammat.mp3", "title": "Yammat FM"}]
    order = order_from_queue({"queue": json.dumps(queue), "start": 9})
    assert order["tracks"][0]["url"].startswith("https://stream.yammat.fm")
    assert order["tracks"][0]["id"] < 0
    assert order["start"] == 0


def test_a_broken_queue_is_nothing_to_play():
    assert order_from_queue({"queue": "not json"}) is None
    assert order_from_queue({"queue": "{}"}) is None
    assert order_from_queue({}) is None


def test_a_station_played_as_media_carries_its_address_and_face():
    order = order_from_media({"uri": "https://s/x.mp3", "title": "Radio 101", "art": "https://s/logo.png"})
    assert order == {"tracks": [{"id": -1, "title": "Radio 101", "artist": "", "album": "",
                                 "cover_url": "https://s/logo.png", "codec": None,
                                 "url": "https://s/x.mp3"}], "start": 0}
    assert order_from_media({"uri": "file:///etc/passwd"}) is None


def test_the_controls_are_what_the_mailbox_obeys():
    assert {"play", "pause", "play_pause", "stop", "next", "previous"} == CONTROLS


def test_the_dac_has_no_idle_only_stopped():
    said = read_dac({"transport": "stopped", "title": "Left over"})
    assert said["media_transport"] == "stopped"
    assert said["media_title"] == "" and said["media_quality"] == ""


def test_a_song_on_the_dac_carries_the_files_measured_quality():
    said = read_dac({"transport": "playing", "kind": "track", "title": "Wherever I Go",
                     "artist": "2Cellos", "album": "Dedicated", "cover_url": "https://cdn/c.jpg",
                     "position": 61.5, "duration": 204, "codec": "flac",
                     "sample_rate_hz": 44100, "bit_depth": 16, "format": "44100:16:2"})
    assert said == {
        "media_transport": "playing", "media_title": "Wherever I Go", "media_artist": "2Cellos",
        "media_album": "Dedicated", "media_art": "https://cdn/c.jpg", "media_source": "library",
        "media_position": 61, "media_duration": 204, "media_quality": "FLAC 44.1kHz/16bit",
    }


def test_a_station_on_the_dac_is_the_houses_radio():
    # the tuner plays here, so next/previous belong to it
    said = read_dac({"transport": "playing", "kind": "station", "title": "Placebo - Shout",
                     "artist": "Yammat FM", "format": "44100:16:2"})
    assert said["media_source"] == "radio"
    assert said["media_quality"] == ""


def test_quality_reads_like_the_other_renderers():
    assert quality("flac", 96000, 24) == "FLAC 96kHz/24bit"
    assert quality("flac", 88200, 24) == "FLAC 88.2kHz/24bit"
    assert quality(None, None, None) == ""


def test_radio_quality_uses_measured_codec_not_the_misleading_url_or_pcm_depth():
    said = read_dac({"transport": "playing", "kind": "station", "format": "44100:16:2",
                     "bitrate": 322, "stream": {"codec": "aac", "sample_rate_hz": 44100}})
    assert said["media_quality"] == "AAC 44.1kHz · 322 kb/s"
    assert read_dac({"transport": "playing", "kind": "station", "bitrate": 320})["media_quality"] == "320 kb/s"


def test_stuck_radio_is_not_reported_as_playing_or_stopped_during_recovery():
    for state in ("recovering", "failed", "unavailable"):
        said = read_dac({"transport": "playing", "kind": "station", "title": "Yammat",
                         "health": {"state": state}})
        assert said["media_transport"] == "buffering"
        assert said["media_title"] == "Yammat"
    said = read_dac({"transport": "stopped", "kind": "station", "error": "network error",
                     "health": {"state": "recovering"}})
    assert said["media_transport"] == "buffering"
    assert read_dac({"transport": "paused", "kind": "station"})["media_transport"] == "paused"
    assert read_dac({"transport": "paused", "kind": "station", "error": "old error"})["media_transport"] == "paused"


def test_quality_states_the_dsd_family_not_a_khz_bit_pair():
    assert quality("dsf", 2822400, 1) == "DSD64"
    assert quality("dff", 5644800, 1) == "DSD128"
    assert quality("dsf", 11289600, 1) == "DSD256"
    # a family the rate does not land on cleanly is still DSD, not silence
    assert quality("dsf", None, 1) == "DSD"


def test_a_dsd_song_on_the_dac_carries_the_family_as_quality():
    said = read_dac({"transport": "playing", "kind": "track", "title": "Closer To The Music",
                     "artist": "Stockfisch", "codec": "dsf", "sample_rate_hz": 2822400,
                     "bit_depth": 1, "format": "dsd64:2"})
    assert said["media_quality"] == "DSD64"


def test_an_opus_address_names_what_only_the_television_does():
    said = {
        "opus:photos?person=Ema": ("/show", {"person": "Ema"}),
        "opus:photos?family": ("/show", {"family": True}),
        "opus:photos": ("/show", {}),
        "opus:continue": ("/resume", {}),
        "opus:next-episode": ("/control", {"command": "next_episode"}),
    }
    for uri, asked in said.items():
        assert order_from_address({"uri": uri}) == asked
    assert order_from_address({"uri": "opus:photos?person="}) is None
    assert order_from_address({"uri": "opus:settings"}) is None
    assert order_from_address({"uri": "http://yammat.fm/stream"}) is None
