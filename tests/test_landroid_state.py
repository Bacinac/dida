"""Landroid readings: what the mower reports must survive the trip into DIDA.

Two failures motivated these. The status map called ids 8/11/12 errors when Worx
means "mowing", "debug" and "remote control", so ordinary mowing raised an alert.
And the fault line itself (`dat.le`) was dropped on the floor, so a mower parked
by a broken boundary loop reported plain "idle" — indistinguishable from one that
had finished the lawn, and silent for as long as nobody walked outside.
"""
from dida_adapter_landroid.adapter import (
    MOWER_STATE,
    TORQUE_ENTITY,
    TORQUE_OPTIONS,
    _error_of,
    _state_of,
    _torque_of,
)
from dida_core.capabilities import validate_state


class StubBus:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.rows: list[tuple[str, str, object]] = []
        self.entities: list[object] = []

    async def publish_state(self, update) -> None:
        self.updates.append((update.capability, update.value))
        self.rows.append((update.entity_id, update.capability, update.value))

    async def publish_entity(self, info) -> None:
        self.entities.append(info)


class StubDevice:
    def __init__(self, status_id: int, error_id: int, error: str, percent: float = 50.0,
                 torque: int | None = None) -> None:
        self.status = {"id": status_id, "description": "whatever"}
        self.error = {"id": error_id, "description": error}
        self.battery = {"percent": percent, "charging": False}
        if torque is not None:
            self.torque = torque


def test_working_statuses_are_not_faults():
    # 8 = mowing, 11 = debug, 12 = remote control per pyworxcloud.
    for status_id in (7, 8, 11, 12):
        assert MOWER_STATE[status_id] != "error", f"status {status_id} is not a breakdown"


def test_blocked_statuses_are_faults():
    for status_id in (9, 10, 13):  # trapped, blade blocked, digital fence escape
        assert MOWER_STATE[status_id] == "error", f"status {status_id} must surface as error"


def test_every_mapped_state_is_a_legal_capability_value():
    for state in MOWER_STATE.values():
        validate_state("mower", state)  # raises if the engine would reject the report


def test_fault_line_is_read_verbatim():
    assert _error_of(StubDevice(0, 3, "wire missing")) == "wire missing"
    assert _error_of(StubDevice(7, 0, "no error")) == "no error"
    assert _error_of(object()) is None, "a device without the field must not fabricate one"


def test_a_lost_boundary_loop_is_distinguishable_from_a_finished_lawn():
    # Both park the mower at status 0; only the fault line tells them apart.
    broken = StubDevice(0, 3, "wire missing")
    done = StubDevice(0, 0, "no error")

    assert _state_of(broken) == _state_of(done) == "idle"
    assert _error_of(broken) != _error_of(done)
    validate_state("mower_error", _error_of(broken))


async def test_publish_carries_the_fault_alongside_state_and_battery():
    from dida_adapter_landroid.adapter import LandroidAdapter

    a = LandroidAdapter()
    a._bus = StubBus()
    await a._publish("Mower", StubDevice(0, 3, "wire missing", percent=100.0))

    assert a._bus.updates == [
        ("mower", "idle"),
        ("battery", 100.0),
        ("mower_error", "wire missing"),
    ]


def test_a_mower_without_the_setting_reports_no_torque():
    assert _torque_of(StubDevice(0, 0, "no error")) is None
    assert _torque_of(StubDevice(0, 0, "no error", torque=0)) == 0.0


async def test_torque_rides_its_own_entity_and_its_bounds_are_sent_once():
    from dida_adapter_landroid.adapter import LandroidAdapter

    a = LandroidAdapter()
    a._bus = StubBus()
    await a._publish("Mower", StubDevice(1, 0, "no error", torque=20))
    await a._publish("Mower", StubDevice(1, 0, "no error", torque=20))

    torque_rows = [r for r in a._bus.rows if r[0] == TORQUE_ENTITY]
    assert torque_rows == [
        (TORQUE_ENTITY, "number_options", TORQUE_OPTIONS),
        (TORQUE_ENTITY, "number", 20.0),
        (TORQUE_ENTITY, "number", 20.0),
    ], "bounds are a constant — announced once, not re-reported every cycle"
    assert [i.entity_id for i in a._bus.entities] == [TORQUE_ENTITY]
    assert a._bus.entities[0].device == "landroid:mower", "the trim belongs on the mower's card"


async def test_a_mower_without_the_setting_publishes_no_torque_entity():
    from dida_adapter_landroid.adapter import LandroidAdapter

    a = LandroidAdapter()
    a._bus = StubBus()
    await a._publish("Mower", StubDevice(1, 0, "no error"))

    assert [r for r in a._bus.rows if r[0] == TORQUE_ENTITY] == []
    assert a._bus.entities == [], "no field, no entity — DIDA does not invent a control"
