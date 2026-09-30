"""Unit test — the astro adapter's solar-phase boundaries.

`sun_state` exists because `sun_elevation` is a level and "at dawn" is an edge. A
rule written against the level has to equal a threshold, and a once-a-minute sample
rounded to 0.1° lands on exactly 0.0 on only about half of mornings — and when it
does, it fires again at sunset, because elevation crosses zero twice a day. These
tests pin the two properties that make the phase usable as a trigger instead:
every transition happens once a day, and dawn can never be dusk.

Pure — no astral, no bus, no clock. Runs in the api image via the working-tree path.
"""
from dida_adapter_astro.mapping import sun_phase
from dida_core import CAPABILITIES, CapabilityKind


def test_the_four_phases():
    assert sun_phase(12.0, rising=True) == "day"
    assert sun_phase(12.0, rising=False) == "day"      # noon is noon either way
    assert sun_phase(-30.0, rising=True) == "night"
    assert sun_phase(-3.0, rising=True) == "dawn"
    assert sun_phase(-3.0, rising=False) == "dusk"


def test_direction_is_what_separates_dawn_from_dusk():
    """The same elevations, traversed the other way, must be the other phase —
    this is precisely what a bare elevation trigger cannot express."""
    for elev in (-5.9, -3.0, -0.1, 0.0):
        assert sun_phase(elev, rising=True) == "dawn"
        assert sun_phase(elev, rising=False) == "dusk"


def test_boundaries():
    # Above the horizon is day; the horizon itself still belongs to twilight, so
    # "at sunrise" is the dawn->day edge rather than a value anyone must equal.
    assert sun_phase(0.0, rising=True) == "dawn"
    assert sun_phase(0.1, rising=True) == "day"
    # Civil twilight ends at -6: -6.0 is still twilight, below it is night.
    assert sun_phase(-6.0, rising=False) == "dusk"
    assert sun_phase(-6.1, rising=False) == "night"


def test_a_whole_day_visits_each_phase_exactly_once_in_order():
    """Walk elevation up and back down the way a real day does and collect the
    transitions. Four, in order — that is the guarantee `to: "dawn"` relies on."""
    rising = [round(-20 + i * 0.1, 1) for i in range(0, 401)]      # -20 -> +20
    falling = [round(20 - i * 0.1, 1) for i in range(0, 401)]      # +20 -> -20
    seen: list[str] = []
    for elev in rising:
        phase = sun_phase(elev, rising=True)
        if not seen or seen[-1] != phase:
            seen.append(phase)
    for elev in falling:
        phase = sun_phase(elev, rising=False)
        if seen[-1] != phase:
            seen.append(phase)
    assert seen == ["night", "dawn", "day", "dusk", "night"]


def test_registered_in_the_capability_model():
    """The phase must be a READ capability with a closed value set, so the engine
    rejects a typo at the boundary and the assistant's generated prompt lists it."""
    spec = CAPABILITIES[CapabilityKind.SUN_STATE]
    assert spec.choices == ("night", "dawn", "day", "dusk")
    assert spec.commands == ()
    for phase in spec.choices:
        assert spec.validate(phase) == phase
