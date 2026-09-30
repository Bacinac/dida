"""Regression tests for the Harmony hub <-> canonical mapping.

Run inside the harmony adapter image (dida_adapter_harmony installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-harmony:latest \
      -c "python -m pytest tests/test_harmony_mapping.py"

Covers activity_options (PowerOff-leads-the-list ordering), _function_devices (the
raw per-activity function->deviceId scrape: malformed action JSON, non-string
action, missing deviceId/name, and the setdefault dedup across control groups),
and build_button_map (the REMOTE_KEYS priority-candidate resolution, the
str(activity_id) comparison quirk, and the empty-map cases for PowerOff / unknown
activities).
"""
from dida_adapter_harmony.mapping import (
    _function_devices,
    activity_options,
    build_button_map,
    button_entity,
)


# --- button_entity -----------------------------------------------------------
def test_button_entity():
    assert button_entity("ok") == "harmony:hub_ok", "suffix appended to the hub entity id"
    assert button_entity("vol_up") == "harmony:hub_vol_up", "suffix appended to the hub entity id"


# --- activity_options: hub activities -> (label->id map, ordered options) ----
def test_activity_options():
    config = {
        "activity": [
            {"id": -1, "label": "PowerOff"},
            {"id": 1, "label": "Watch TV"},
            {"id": 2, "label": "Listen to Music"},
        ]
    }
    by_label, options = activity_options(config)
    assert by_label == {"PowerOff": -1, "Watch TV": 1, "Listen to Music": 2}, "label -> id map"
    assert options == ["PowerOff", "Watch TV", "Listen to Music"], "PowerOff leads, rest in hub order"

    # a hub that doesn't define a PowerOff activity gets no synthetic "off" entry
    by_label, options = activity_options({"activity": [{"id": 1, "label": "Watch TV"}]})
    assert options == ["Watch TV"], "PowerOff omitted entirely when the hub doesn't define it"

    assert activity_options(None) == ({}, []), "missing config -> empty map and options"
    assert activity_options({}) == ({}, []), "config without an 'activity' key -> empty map and options"


# --- _function_devices: raw per-activity function -> deviceId scrape --------
def test_function_devices():
    activity = {
        "controlGroup": [
            {
                "function": [
                    {"name": "DirectionUp", "action": '{"deviceId": "dev-tv"}'},
                    {"name": "BadJson", "action": "not-json"},
                    {"name": "NonStringAction", "action": {"deviceId": "dev-x"}},
                    {"name": "NoDeviceId", "action": "{}"},
                    {"action": '{"deviceId": "dev-noname"}'},
                ]
            },
            {
                "function": [
                    # same function name as the first control group -> first device wins
                    {"name": "DirectionUp", "action": '{"deviceId": "dev-other"}'},
                ]
            },
        ]
    }
    fn_dev = _function_devices(activity)
    assert fn_dev == {"DirectionUp": "dev-tv"}, (
        "malformed JSON / non-string action / missing deviceId / missing name are all "
        "ignored; a duplicate function name across control groups keeps the FIRST device"
    )
    assert _function_devices({}) == {}, "activity without a controlGroup -> empty map"


# --- build_button_map: REMOTE_KEYS candidate resolution ----------------------
def test_build_button_map():
    activity = {
        "id": 1,
        "controlGroup": [
            {
                "function": [
                    {"name": "DirectionUp", "action": '{"deviceId": "dev-tv"}'},
                    {"name": "OK", "action": '{"deviceId": "dev-tv"}'},
                    {"name": "Select", "action": '{"deviceId": "dev-avr"}'},
                    {"name": "VolumeUp", "action": '{"deviceId": "dev-avr"}'},
                ]
            },
        ],
    }
    config = {"activity": [{"id": -1, "label": "PowerOff"}, activity]}

    m = build_button_map(config, 1)
    assert m["up"] == ("dev-tv", "DirectionUp"), "single-candidate key resolves directly"
    # both "Select" and "OK" are present for the "ok" suffix — Select is the
    # higher-priority candidate (listed first) and wins even though OK also matches.
    assert m["ok"] == ("dev-avr", "Select"), "first matching candidate in priority order wins"
    assert m["vol_up"] == ("dev-avr", "VolumeUp"), "vol_up maps through"
    assert "down" not in m, "a key absent from this activity's functions is simply not in the map"

    # activity_id is compared via str(), so an int id and a string id both match.
    assert build_button_map(config, "1") == build_button_map(config, 1), "activity_id matched by str()"

    # PowerOff activity has no controlGroup at all -> empty map, not an error
    assert build_button_map(config, -1) == {}, "PowerOff activity has no functions -> empty map"
    # unknown activity id -> no active activity -> empty map
    assert build_button_map(config, 999) == {}, "unknown activity_id -> empty map"
    assert build_button_map(None, 1) == {}, "missing config -> empty map"


# --- reachability: the verdict from the source, edge-triggered --------------------


class _ReachBus:
    def __init__(self) -> None:
        self.reach: list = []

    async def publish_reachability(self, event) -> None:
        self.reach.append(event)

    async def publish_state(self, update) -> None:
        pass

    async def publish_entity(self, info) -> None:
        pass


def _rkeys(bus):
    return [(e.device_key, e.reachable) for e in bus.reach]


async def test_reachability_lands_on_the_hub_device():
    from dida_adapter_harmony.adapter import HUB_ENTITY, HarmonyAdapter

    a = HarmonyAdapter()
    a._bus = _ReachBus()
    await a._publish_reach(False, "hub disconnected")
    await a._publish_reach(False, "hub disconnected")
    assert _rkeys(a._bus) == [(HUB_ENTITY, False)]
