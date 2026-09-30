"""Frigate's MQTT surface → canonical capabilities.

785 lines running the cameras, with no test at all. The translation is where this
adapter earns its keep: Frigate publishes a discovery-shaped topic tree (per-object
counts, feature toggles, thresholds, per-camera fps) and the adapter decides, from
the topic SHAPE and the value TYPE alone, what becomes an entity and which
capability it carries. Get that wrong and a camera reports the wrong thing, or
nothing, and the only symptom is a tile that never moves.

Everything here drives the REAL `_handle` dispatcher against a stub bus, not the
per-topic helpers directly: routing is half the logic, and a test that calls
`_on_motion` proves the translation while leaving the routing to it untested.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from dida_adapter_frigate.adapter import FrigateAdapter


class StubBus:
    def __init__(self) -> None:
        self.states: list[tuple[str, str, object]] = []
        self.entities: list[object] = []

    async def publish_state(self, update) -> None:
        self.states.append((update.entity_id, update.capability, update.value))

    async def publish_entity(self, info) -> None:
        self.entities.append(info)

    async def publish_command(self, command) -> None:
        pass


@pytest.fixture
def adapter():
    a = FrigateAdapter()
    a._bus = StubBus()
    return a


async def _feed(adapter, topic: str, payload) -> list[tuple[str, str, object]]:
    """Push one MQTT message through the real dispatcher and let the fire-and-forget
    publishes run — `_pub` spawns a task, so a synchronous assertion right after
    would read an empty bus and pass for the wrong reason."""
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    await adapter._handle("home", "frigate", topic, payload)
    for _ in range(8):
        await asyncio.sleep(0)
    return adapter._bus.states


def _find(states, capability):
    return [(e, v) for e, c, v in states if c == capability]


async def test_motion_becomes_a_boolean(adapter):
    states = await _feed(adapter, "frigate/driveway/motion", "ON")
    assert _find(states, "motion") == [("frigate:driveway", True)]


async def test_motion_off_is_false_not_absent(adapter):
    """`OFF` must publish False. Dropping it would leave the last True latched and
    a camera permanently 'seeing' motion."""
    await _feed(adapter, "frigate/driveway/motion", "ON")
    states = await _feed(adapter, "frigate/driveway/motion", "OFF")
    assert ("frigate:driveway", False) in _find(states, "motion")


async def test_a_person_count_is_the_camera_hero_not_a_measurement(adapter):
    states = await _feed(adapter, "frigate/driveway/person", "2")
    assert _find(states, "person_count") == [("frigate:driveway", 2)]
    assert not _find(states, "measurement"), "person must not also be a measurement"


async def test_any_other_object_count_becomes_a_measurement_entity(adapter):
    states = await _feed(adapter, "frigate/driveway/car", "3")
    assert _find(states, "measurement") == [("frigate:driveway:count:car", 3.0)]


async def test_an_object_count_entity_is_announced_as_diagnostic(adapter):
    """Every Frigate object would otherwise flood the UI with tiles nobody asked
    for; they are announced hidden and curated in Settings."""
    await _feed(adapter, "frigate/driveway/car", "1")
    announced = [e for e in adapter._bus.entities
                 if e.entity_id == "frigate:driveway:count:car"]
    assert announced and announced[0].diagnostic is True


async def test_every_entity_of_a_camera_names_the_location_it_stands_at(adapter):
    await _feed(adapter, "frigate/driveway/motion", "ON")
    await _feed(adapter, "frigate/driveway/car", "1")
    await _feed(adapter, "frigate/driveway/detect/state", "ON")
    for _ in range(8):
        await asyncio.sleep(0)
    assert adapter._bus.entities
    assert {e.site for e in adapter._bus.entities} == {"home"}


async def test_an_event_publishes_the_object_class(adapter):
    states = await _feed(adapter, "frigate/events", {
        "type": "new", "after": {"id": "e1", "camera": "driveway", "label": "person"}})
    assert ("frigate:driveway", "person") in _find(states, "object_class")


async def test_a_vehicle_label_is_folded_into_one_class(adapter):
    """The canonical model has classes, not Frigate's label vocabulary — a rule
    written for 'vehicle' must fire for a truck."""
    states = await _feed(adapter, "frigate/events", {
        "type": "new", "after": {"id": "e2", "camera": "gate", "label": "truck"}})
    assert ("frigate:gate", "vehicle") in _find(states, "object_class")


async def test_the_last_event_ending_clears_the_class(adapter):
    """Without this a camera keeps reporting the object that left an hour ago."""
    await _feed(adapter, "frigate/events", {
        "type": "new", "after": {"id": "e3", "camera": "yard", "label": "person"}})
    states = await _feed(adapter, "frigate/events", {
        "type": "end", "after": {"id": "e3", "camera": "yard", "label": "person"}})
    assert ("frigate:yard", "none") in _find(states, "object_class")


async def test_one_event_ending_while_another_runs_does_NOT_clear(adapter):
    """Two people in frame, one leaves: the camera still sees a person."""
    for eid in ("a", "b"):
        await _feed(adapter, "frigate/events", {
            "type": "new", "after": {"id": eid, "camera": "patio", "label": "person"}})
    adapter._bus.states.clear()
    states = await _feed(adapter, "frigate/events", {
        "type": "end", "after": {"id": "a", "camera": "patio", "label": "person"}})
    assert ("frigate:patio", "none") not in _find(states, "object_class")


async def test_an_undecodable_event_payload_is_ignored_not_raised(adapter):
    """A malformed retained message must not take the consume loop down with it."""
    assert await _feed(adapter, "frigate/events", "{not json") == []


async def test_a_feature_state_topic_becomes_a_switch(adapter):
    states = await _feed(adapter, "frigate/driveway/detect/state", "ON")
    assert _find(states, "on_off") == [("frigate:driveway:detect", True)]


async def test_the_write_sibling_is_not_mirrored_as_state(adapter):
    """Frigate publishes `/set` as the write side. Reading it back as state would
    make a command look like a confirmation it never received."""
    assert await _feed(adapter, "frigate/driveway/detect/set", "ON") == []


async def test_a_snapshot_topic_is_not_an_entity(adapter):
    assert await _feed(adapter, "frigate/driveway/snapshot", b"\x89PNG") == []


async def test_a_topic_from_a_different_prefix_is_ignored(adapter):
    """Two Frigate sites share one broker in this installation; a topic belonging
    to the other prefix must not be attributed to this one."""
    assert await _feed(adapter, "othersite/driveway/motion", "ON") == []


async def test_a_repeated_value_is_published_once(adapter):
    """Frigate republishes on every frame; without the dedup the state firehose
    would carry the same value thousands of times an hour."""
    await _feed(adapter, "frigate/driveway/motion", "ON")
    adapter._bus.states.clear()
    assert await _feed(adapter, "frigate/driveway/motion", "ON") == []


def test_a_site_with_go2rtc_offers_the_full_stream_as_mp4():
    """Frigate's own MJPEG is its detect stream; the full-resolution live view
    exists only where the site's go2rtc is reachable."""
    from dida_adapter_frigate.adapter import _descriptor

    bare = json.loads(_descriptor("https://nvr.example", "", "drive"))
    assert "mp4" not in bare
    full = json.loads(_descriptor("https://nvr.example", "https://g.example", "drive"))
    assert full["mp4"] == "https://g.example/api/stream.mp4?src=drive"


def test_a_learned_camera_id_is_still_shown_as_a_name(adapter):
    """The id Frigate reports is kept verbatim for its API; people read a name."""
    adapter._cam_name["front_door"] = "front_door"
    adapter._cam_name["terrace"] = "terrace"
    assert adapter._camera_name("front_door") == "Front Door"
    assert adapter._camera_name("terrace") == "Terrace"
