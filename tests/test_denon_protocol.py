"""Regression tests for the Denon/Marantz Telnet protocol parsing. The volume math
shipped a real bug: DIDA divided the master volume by MVMAX to fake a percentage, so
its number ran ahead of the receiver whenever a Volume Limit lowered MVMAX below the
0–98 scale.

Pure stdlib functions, so no image/DB needed — runs in the denon adapter image:
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-denon:latest \
      -c "python -m pytest tests/test_denon_protocol.py"

The receiver reports volume on its own absolute 0–98 scale (what it displays when
Volume Scale = "0-98"), so DIDA shows that number verbatim — no invented percentage.
Covers the half-step MV parse, the pass-through display, the outgoing clamp to MVMAX
(the Volume Limit), and the SSFUN/SSSOD source-table merge.
"""
from dida_adapter_denon.protocol import (
    build_sources,
    parse_volume,
    pct_to_raw,
    vol_to_pct,
)


# --- parse_volume --------------------------------------------------------------
def test_parse_volume():
    assert parse_volume("40") == 40.0, "two digits -> whole step"
    assert parse_volume("405") == 40.5, "third digit is the half-step (0.5)"
    assert parse_volume("MVMAX 80") == 80.0, "non-digits stripped, whitespace tolerated"
    assert parse_volume("985") == 98.5, "three digits -> 98.5, not 985"
    assert parse_volume("5") == 5.0, "single digit"
    assert parse_volume("") is None, "no digits -> None"
    assert parse_volume("ON") is None, "letters only -> None"


# --- vol_to_pct: shows the receiver's own 0–98 value, never a computed percentage -
def test_vol_to_pct_is_the_native_scale_value():
    assert vol_to_pct(0) == 0, "silence -> 0"
    assert vol_to_pct(98) == 98, "top of the 0–98 scale stays 98 (not rescaled to 100)"
    # The bug scenario: a Marantz with Limit 80 (MVMAX 80) sitting at raw 40. The unit
    # shows 40; DIDA now shows 40 too. (Old code did 40/80·100 = 50.)
    assert vol_to_pct(40) == 40, "raw 40 -> 40, exactly what the receiver displays"
    assert vol_to_pct(44) == 44, "raw 44 -> 44 (was 55 against MVMAX 80)"
    assert vol_to_pct(80) == 80, "raw 80 (the Limit ceiling) -> 80, not a fake 100"
    assert vol_to_pct(40.5) in (40, 41), "half-step rounds to nearest int"
    assert vol_to_pct(200) == 100, "absurd value still capped at 100 (0–100 UI range)"


# --- pct_to_raw: the UI value is already a 0–98 point; MVMAX only clamps -----------
def test_pct_to_raw_passthrough_and_clamp():
    # No Volume Limit (MVMAX 98): the value passes straight through, round-trips exactly.
    for v in (0, 25, 40, 50, 60, 98):
        raw = pct_to_raw(v, 98.0)
        assert vol_to_pct(raw) == v, f"{v} passes through unchanged (raw {raw})"
    # Volume Limit 80 (MVMAX 80): below the ceiling passes through; above it snaps down.
    assert pct_to_raw(50, 80.0) == 50, "50 is below the limit -> unchanged"
    assert pct_to_raw(90, 80.0) == 80, "90 clamped to the Volume Limit 80"
    assert pct_to_raw(100, 80.0) == 80, "100 request snaps to the real ceiling 80"
    assert vol_to_pct(pct_to_raw(100, 80.0)) == 80, "and the slider settles at 80, matching the unit"


def test_pct_to_raw_bounds():
    assert pct_to_raw(0, 98.0) == 0, "0 -> raw 0"
    assert pct_to_raw(-10, 98.0) == 0, "negative clamps to 0"
    assert pct_to_raw(200, 98.0) == 98, "over-scale clamps to MVMAX"


# --- build_sources: SSFUN (rename) x SSSOD (enabled) merge ---------------------
def test_build_sources_merge():
    ssfun = {"BD": "Blu-ray", "SAT/CBL": "Kabel", "CD": "CD"}
    sssod = {"BD": "USE", "SAT/CBL": "USE", "CD": "DEL"}
    options, l2c, c2l = build_sources(ssfun, sssod)
    assert options == ["Blu-ray", "Kabel"], "only USE inputs, in SSSOD order; DEL dropped"
    assert l2c["Blu-ray"] == "BD", "label -> code, to send SI<code>"
    assert c2l["SAT/CBL"] == "Kabel", "code -> renamed label, to display current SI<code>"
    assert "CD" not in c2l, "explicitly DEL'd input is not offered"


def test_build_sources_unrenamed_falls_back_to_code():
    # Enabled but not renamed -> the code itself is the label.
    options, l2c, _c2l = build_sources({}, {"GAME": "USE"})
    assert options == ["GAME"], "no custom name -> code doubles as the label"
    assert l2c == {"GAME": "GAME"}


def test_build_sources_renamed_not_in_sssod_kept():
    # Renamed but absent from SSSOD (firmware didn't list it) -> kept, not dropped.
    options, _l2c, c2l = build_sources({"TV": "Televizor"}, {})
    assert c2l == {"TV": "Televizor"}, "renamed input absent from SSSOD is still offered"
    assert options == ["Televizor"]


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


async def test_reachability_follows_the_telnet_connection():
    from dida_adapter_denon.adapter import DenonAdapter

    a = DenonAdapter()
    a._bus = _ReachBus()
    await a._publish_reach("192.168.1.50", True)
    await a._publish_reach("192.168.1.50", False, "connection lost")
    await a._publish_reach("192.168.1.50", False, "connection lost")
    assert _rkeys(a._bus) == [("192.168.1.50", True), ("192.168.1.50", False)]


# --- a receiver still booting after a power cut -------------------------------------


class _StateBus(_ReachBus):
    def __init__(self) -> None:
        super().__init__()
        self.states: list = []

    async def publish_state(self, update) -> None:
        self.states.append((update.entity_id, update.capability, update.value))


class _BootingReceiver:
    """A telnet writer standing in for a Marantz that takes the connection while it
    is still booting: the first source-table queries get no answer, later ones the
    real table (input CD renamed MUSIC)."""

    def __init__(self, adapter, empty_tables: int) -> None:
        self.adapter = adapter
        self.empty_tables = empty_tables
        self.sent: list[str] = []
        self._pending: list[str] = []

    def write(self, data: bytes) -> None:
        cmd = data.decode().strip()
        self.sent.append(cmd)
        booted = self.sent.count("SSSOD ?") > self.empty_tables
        if cmd == "SSFUN ?" and self.sent.count("SSFUN ?") > self.empty_tables:
            self._pending += ["SSFUNCD MUSIC", "SSFUNSAT/CBL SHIELD", "SSFUN END"]
        elif cmd == "SSSOD ?" and booted:
            self._pending += ["SSSODCD USE", "SSSODSAT/CBL USE", "SSSOD END"]
        elif cmd == "SI?":
            self._pending.append("SICD")

    async def drain(self) -> None:
        frames, self._pending = self._pending, []
        for frame in frames:
            await self.adapter._on_line(frame)


def _booting_adapter(monkeypatch, empty_tables: int):
    import dida_adapter_denon.adapter as mod

    monkeypatch.setattr(mod, "SOURCE_RETRY_SECONDS", 0)
    monkeypatch.setattr(mod, "SOURCE_SETTLE_SECONDS", 0)
    a = mod.DenonAdapter()
    a._bus = _StateBus()
    a._main = mod.Zone("denon:marantz_main", "Marantz")
    a._zone2 = mod.Zone("denon:marantz_zone2", "Marantz Zone 2")
    a._writer = _BootingReceiver(a, empty_tables)
    return a


async def test_a_source_table_missed_while_booting_is_asked_for_again(monkeypatch):
    a = _booting_adapter(monkeypatch, empty_tables=2)
    await a._interrogate()
    assert a._writer.sent.count("SSFUN ?") == 3
    assert a._source_options == ["MUSIC", "SHIELD"]
    sources = [v for e, c, v in a._bus.states if e == "denon:marantz_main" and c == "source"]
    assert sources == ["CD", "MUSIC"], "the bare code is corrected once the table lands"


async def test_the_retry_stops_with_the_connection(monkeypatch):
    import dida_adapter_denon.adapter as mod

    a = _booting_adapter(monkeypatch, empty_tables=10**6)
    receiver = a._writer

    async def connection_drops(_seconds):
        a._writer = None

    monkeypatch.setattr(mod.asyncio, "sleep", connection_drops)
    await a._interrogate()
    assert receiver.sent.count("SSFUN ?") == 1, "a dead connection is not asked again"


# --- mute dropped in standby ---------------------------------------------------------


class _UnmutedReceiver:
    """A Marantz whose zones came out of standby unmuted without pushing MUOFF."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.sent: list[str] = []
        self._pending: list[str] = []

    def write(self, data: bytes) -> None:
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if cmd in ("MU?", "Z2MU?"):
            self._pending.append(cmd[:-1] + "OFF")

    async def drain(self) -> None:
        frames, self._pending = self._pending, []
        for frame in frames:
            await self.adapter._on_line(frame)


async def test_a_waking_zone_reports_the_mute_it_really_has(monkeypatch):
    import asyncio

    import dida_adapter_denon.adapter as mod

    monkeypatch.setattr(mod, "MUTE_REQUERY_SECONDS", 0)
    a = mod.DenonAdapter()
    a._bus = _StateBus()
    a._main = mod.Zone("denon:marantz_main", "Marantz")
    a._zone2 = mod.Zone("denon:marantz_zone2", "Marantz Zone 2")
    a._writer = _UnmutedReceiver(a)
    a._pw_on = True
    for zone in (a._main, a._zone2):
        zone.last.update(on_off=False, mute=True)

    await a._on_line("ZMON")
    await a._on_line("Z2ON")
    await a._on_line("PWON")
    await asyncio.sleep(0.05)

    assert a._writer.sent == ["MU?", "Z2MU?"], "only a zone that just woke is asked"
    assert a._main.last["mute"] is False and a._zone2.last["mute"] is False
