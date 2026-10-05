"""Canonical capability model — DIDA's rigid backbone contract.

This is the ONE thing every device, every adapter, and the engine agree on.
The engine never knows about Zigbee, Tuya, Modbus or any concrete device — it
only knows *capabilities*. Adapters translate a device's native protocol into
capability state updates, and translate capability commands back into native
protocol.

Why this is the robustness keystone:

  * Every state update from an adapter is validated against the capability spec
    *at the bus boundary* (`validate_state`). A malformed or out-of-range value
    is rejected and logged — it never reaches the engine's state, so a buggy
    adapter cannot corrupt the system. This is "fail loud": the bad value is
    dropped with a visible error, not silently coerced.
  * The capability set is the stable contract between adapter and engine. An
    adapter built today keeps working when the engine is upgraded, because the
    contract — not the engine's internals — is what it depends on.

Analogy: capabilities are to DIDA what ONNX is to BABA. Adapters are backends
that convert native <-> canonical; the bus is the isolation boundary; schema
validation at that boundary is the guarantee.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum, StrEnum

# A capability value is always one of these scalar types on the wire.
Value = bool | int | float | str


class CapabilityError(ValueError):
    """Raised when a state update or command violates the capability spec.

    The engine catches this at the bus boundary and rejects the offending
    message (logged + counted) instead of letting it into state.
    """


class Access(Enum):
    READ = "read"  # sensor: engine reads state, cannot command
    WRITE = "write"  # actuator-only: command, no readable state
    READ_WRITE = "read_write"  # actuator with reportable state


class ValueType(Enum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"


class CapabilityKind(StrEnum):
    """The canonical capability vocabulary.

    Deliberately small and additive: new device features map onto existing
    kinds where possible; genuinely new kinds are added here (a contract
    change, versioned), never invented ad-hoc by an adapter.
    """

    # --- actuators (commandable) ---
    ON_OFF = "on_off"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    COLOR_RGB = "color_rgb"  # RGB colour as a "#RRGGBB" hex string
    EFFECT = "effect"  # currently selected light effect (name)
    EFFECT_OPTIONS = "effect_options"  # JSON list of available effect names (metadata)
    OPEN_CLOSE = "open_close"  # covers / blinds, 0..100 % open
    LOCK = "lock"
    # --- generic state: virtual entities ("helpers") AND device modes ---
    # A "helper" in DIDA is just an entity whose owner is DIDA itself (the
    # `virtual` adapter); a device mode (e.g. a heater's Off/L1/L2/L3) is the
    # SAME enum capability living on the device entity, where it belongs.
    BOOLEAN = "boolean"  # generic on/off flag (virtual helper or device flag)
    ENUM = "enum"  # currently selected option (string)
    ENUM_OPTIONS = "enum_options"  # JSON list of allowed options (runtime metadata)
    NUMBER = "number"  # generic settable numeric value
    NUMBER_OPTIONS = "number_options"  # JSON {min,max,step,unit,mode} for a NUMBER (metadata)
    # --- climate (AC / heat pump / thermostat: one entity, several scalars) ---
    # Like media, a climate device decomposes into scalar capabilities on one
    # entity; the UI recomposes them into a climate card. Current temperature is
    # just the existing `temperature` (READ) capability on the same entity.
    HVAC_MODE = "hvac_mode"  # off / cool / heat / auto / dry / fan_only
    HVAC_MODE_OPTIONS = "hvac_mode_options"  # JSON list of modes this device supports (metadata)
    TARGET_TEMPERATURE = "target_temperature"  # the setpoint, °C
    FAN_MODE = "fan_mode"  # auto / low / medium / high / silent / max
    FAN_MODE_OPTIONS = "fan_mode_options"  # JSON list of fan modes this device supports (metadata)
    # --- fan (standalone fan: speed %; preset/oscillation/direction reuse enum/boolean) ---
    FAN_SPEED = "fan_speed"  # 0..100 % fan speed
    # --- robotic lawn mower ---
    MOWER = "mower"  # state string (idle/mowing/docked/returning/paused/error) + start/pause/dock
    MOWER_ERROR = "mower_error"  # last fault the mower reported, verbatim ("no error" when clear)
    # --- robotic vacuum ---
    VACUUM = "vacuum"
    VACUUM_MODE = "vacuum_mode"                 # what the robot does to the floor
    VACUUM_MODE_OPTIONS = "vacuum_mode_options" # JSON list of the modes this robot has  # state string (idle/cleaning/docked/returning/paused/error) + start/pause/dock
    # --- notifier (push without HA: ntfy / Gotify) ---
    # WRITE-only: an automation fires `notify` with args {message, title?, camera?}
    # and the notify adapter pushes it to a phone; `camera` (a camera entity, or any
    # of its children — a zone, the bell) makes the push carry that camera's frame.
    # No readable state.
    NOTIFY = "notify"
    # --- announce (spoken TTS on a speaker) ---
    # RW: command `say` with args {text, language?} speaks on a speaker; the
    # readable value is the last spoken text (so the entity registers + shows).
    ANNOUNCE = "announce"
    # --- media player (a renderer = one entity carrying this whole group) ---
    # A media player doesn't need a bespoke "domain" like Home Assistant's: it
    # decomposes into ordinary scalar capabilities on one entity. The transport
    # is the actuator; the rest are read-only now-playing metadata. The UI
    # recomposes them into a single media card.
    MEDIA_TRANSPORT = "media_transport"  # state string: playing/paused/stopped/idle/buffering
    VOLUME = "volume"  # 0..100 %
    MUTE = "mute"
    MEDIA_TITLE = "media_title"
    MEDIA_ARTIST = "media_artist"
    MEDIA_ALBUM = "media_album"
    MEDIA_ART = "media_art"  # album/station art URL
    MEDIA_QUALITY = "media_quality"  # human format string, e.g. "FLAC 44.1kHz/16bit"
    MEDIA_SOURCE = "media_source"  # origin of the current stream: tidal/library/radio/url/external
    MEDIA_QUEUE = "media_queue"  # JSON {items:[{title,artist,art}], current:int} — the play queue
    MEDIA_DURATION = "media_duration"  # seconds (0/absent → live stream)
    MEDIA_POSITION = "media_position"  # seconds; published sparsely, UI extrapolates
    MEDIA_FAVORITES = "media_favorites"  # JSON list of presets [{name,image,preset}] (runtime metadata)
    MEDIA_DISPLAY = "media_display"  # true → this player has a screen (can show video / the wall panel).
    # Static device property, not a live reading (a speaker never reports it). Lets the
    # UI offer only screen-capable players where a display is required (e.g. the wall panel).
    # --- selectable input (AV receiver source, etc.) ---
    # `source` is the current input; `source_options` carries the device's
    # available inputs (a JSON-encoded list) as runtime metadata, since the set
    # is device-specific and not part of the static contract. The UI renders a
    # dropdown from source_options and sets `source` via set_source.
    SOURCE = "source"
    SOURCE_OPTIONS = "source_options"
    # AV receiver video OUTPUT selection (which HDMI monitor + its resolution),
    # distinct from `source` (the input). Write-only control; the adapter maps a
    # small closed set ("tv" / "projector") onto the receiver's raw commands.
    VIDEO_OUTPUT = "video_output"
    # AV receiver Video Select: which INPUT's picture stays on the screen while the
    # ear is on another one. A receiver takes picture and sound off the same input,
    # so listening to a source wired in by analogue blanks whatever was showing;
    # this breaks that pair. Write-only; the value is a device input label, or
    # "off" to hand the pair back. Labels are device-specific, like `source`.
    VIDEO_SELECT = "video_select"
    CHANNEL = "channel"  # write-mostly: tune a TV app to a channel (e.g. by number)
    # --- sensors (read-only) ---
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    ILLUMINANCE = "illuminance"
    # --- air quality ---
    PM25 = "pm25"  # particulate matter ≤2.5µm, µg/m³
    VOC_INDEX = "voc_index"  # volatile organic compounds, unitless index (~1..500)
    # --- weather station (Ecowitt etc.) ---
    PRESSURE = "pressure"  # hPa
    WIND_SPEED = "wind_speed"  # m/s
    WIND_GUST = "wind_gust"  # m/s, the peak of the current window
    WIND_DIRECTION = "wind_direction"  # degrees clockwise from north
    DEW_POINT = "dew_point"  # °C — the temperature the air would have to reach to condense
    SOLAR_RADIATION = "solar_radiation"  # W/m²
    UV_INDEX = "uv_index"  # UV index (0..~12)
    RAIN_RATE = "rain_rate"  # mm/h
    RAIN_DAILY = "rain_daily"  # mm accumulated today (rain-skip gating for irrigation)
    POWER = "power"
    ENERGY = "energy"
    VOLTAGE = "voltage"
    CURRENT = "current"
    FREQUENCY = "frequency"
    POWER_FACTOR = "power_factor"
    BATTERY = "battery"
    SIGNAL = "signal"  # RSSI / link quality (usually diagnostic)
    DURATION = "duration"  # e.g. uptime, seconds (usually diagnostic)
    REMAINING = "remaining"  # seconds until an appliance job finishes (washer/dryer)
    SUN_ELEVATION = "sun_elevation"  # solar altitude in degrees (-90..90); a
    # source adapter (astro) computes it for the home's location. "Night" is
    # then a native condition (sun_elevation <= 0), no derived entity needed.
    NEXT_OCCURRENCE = "next_occurrence"  # ISO date (YYYY-MM-DD) of the next day a
    # recurring schedule fires, today included. `schedule_active` answers "is it
    # today"; this answers "when", which is the question actually asked about a bin
    # collection. An empty string clears a schedule with no next day; recurrence
    # maths stays in the shared scheduler, and this capability carries its result.
    SUN_STATE = "sun_state"  # the solar PHASE as a discrete event: night/dawn/day/dusk.
    # sun_elevation is a continuous float, which makes it a fine CONDITION and a
    # trap as a TRIGGER: a rule like "at dawn" written as `to: 0` only fires when a
    # once-a-minute sample rounds to exactly 0.0 (measured: it misses ~4 mornings in
    # 10) and, when it does fire, fires again at sunset because elevation crosses
    # zero twice a day. This capability is the edge instead of the level — each of
    # the four transitions happens exactly once per day, and dawn cannot be dusk
    # because direction is baked in. "At dawn" = `to: "dawn"`; "at sunrise" =
    # `to: "day"`. Boundaries are civil twilight: day > 0° > dawn/dusk > -6° > night.
    TIME_OF_DAY = "time_of_day"  # local wall-clock minutes since midnight (0..1439),
    # published each minute by the astro adapter. Lets automations gate on time
    # windows (06:30..23:30 -> cond >=390 and <=1410) and run on a 1-min heartbeat
    # (trigger with to=None), reusing the existing condition/trigger model as-is.
    TIME = "time"  # a SETTABLE wall-clock time (minutes since midnight, 0..1439) — the
    # writable sibling of TIME_OF_DAY. This is the value TYPE for a "time helper": a
    # per-household time a user edits in the UI (a quiet-hours boundary, a curfew),
    # which automations then compare against astro's live time_of_day. Same units as
    # time_of_day so the two compare directly; READ_WRITE because the user owns it.
    CONTACT = "contact"  # door/window open=true
    MOTION = "motion"
    OCCUPANCY = "occupancy"
    SMOKE = "smoke"  # smoke detector alarm (true = smoke detected)
    CONNECTIVITY = "connectivity"  # link/internet up (true) — e.g. WAN gateway online
    SCHEDULE_ACTIVE = "schedule_active"  # a user-defined calendar/season window is
    # currently active (true) — driven by the calendar adapter. A `cron` schedule
    # fires a momentary BUTTON tick instead; this cap is for range/window schedules.
    BUTTON = "button"  # momentary event source; value is the action string emitted
    PRESS = "press"  # commandable momentary button — press it (WRITE, no readable state)
    TEXT = "text"  # read-only text sensor (version string, status, …)
    # Generic read-only fallbacks so an adapter never has to drop a field it can
    # read but can't classify (a binary sensor / numeric sensor with no device_class).
    BINARY = "binary"  # generic read-only boolean state
    MEASUREMENT = "measurement"  # generic read-only numeric reading (unit carried per-value)

    # Presence: a tracked person's GPS-resolved whereabouts. `location` is the
    # headline (current zone name, or "away" when outside every zone); latitude/
    # longitude are the raw position (diagnostic, drive the map marker).
    LOCATION = "location"  # current zone name / "away"
    LATITUDE = "latitude"  # decimal degrees
    LONGITUDE = "longitude"  # decimal degrees

    # --- vision (BABA NVR) ---
    # BABA sees and hears; DIDA holds the house. The `baba` adapter mirrors
    # BABA's per-camera state snapshots (`baba.state.*`), doorbell presses,
    # places and identity roster onto these capabilities — the SAME
    # validate→project→persist path as any other adapter,
    # so a bad detection is rejected at the boundary, never corrupting the core.
    # Pixels never touch the bus: a camera entity carries a stream *descriptor*
    # (a URL set), and the UI pulls WebRTC/HLS straight from go2rtc. Video is not
    # state — putting it through NATS/Postgres/ClickHouse is the exact HA-recorder
    # mistake DIDA exists to avoid.
    CAMERA = "camera"  # JSON stream descriptor {stream, webrtc, hls, snapshot, mp4}
    PERSON_COUNT = "person_count"  # live count of people a camera currently tracks
    OBJECT_CLASS = "object_class"  # last object class seen (person/vehicle/animal/other)
    SCENE_STATE = "scene_state"  # a scene-region's evaluated label (gate open/closed, …)
    # How much light a CAMERA has, as the camera itself measures it (frame luma +
    # IR ratio, debounced by BABA and used there to switch detection profiles). Not
    # a lux reading and not interchangeable with `illuminance`: it answers "can this
    # camera still form a colour picture", which is the only question a floodlight
    # rule is really asking. The house's sky sensor cannot reach that far down —
    # measured, its lowest rung is 10 lux and the camera still has colour below it.
    LIGHT_CONDITION = "light_condition"
    PARKED_VEHICLE = "parked_vehicle"  # the named vehicle standing in the place a scene
    # region watches: a JSON descriptor {"name", "since"} while one is assigned, "" when
    # the place is empty. A separate capability rather than a composed display name,
    # because names load once while STATE flows live — the wall must relabel the moment
    # the plate sweep names the car, not on the next page load.
    IDENTITY_PRESENCE = "identity_presence"  # a recognised PERSON's presence at a camera,
    # folded with the recognition confidence: "absent" | "body" | "face". The sibling of
    # scene_state on the vision plane — BABA decides who is present and how surely (a
    # body-anchored re-ID gives "body" in real time; "face" once the face confirms),
    # DIDA mirrors it. A confidence LADDER so an automation gates on one value: `!=
    # "absent"` (present at all) for soft actions, `== "face"` (sure) for hard ones.
    IDENTITY_SINCE = "identity_since"  # ISO start of that person's CURRENT stay, "" when
    # absent. A footnote of identity_presence, never a row of its own — the surfaces hide
    # it and render it as "since 14:02" under the presence value. Its own capability
    # rather than a composed string for the same reason as parked_vehicle: the start time
    # must survive independently of the presence value that changes around it.
    OBJECT_PRESENCE = "object_presence"  # a recognised PET/VEHICLE's presence at a
    # camera: "absent" | "pet" | "vehicle" — the value carries the KIND when present.
    # The non-person sibling of identity_presence: these have no face, so BABA matches
    # body vs the operator's enrolled reference photos; DIDA mirrors the on-screen result.


@dataclass(slots=True, frozen=True)
class CapabilitySpec:
    """Declarative spec for one capability kind.

    `validate()` is the single chokepoint the engine runs on every inbound
    value. Keep the rules here, not scattered across services.
    """

    kind: CapabilityKind
    value_type: ValueType
    access: Access
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    commands: tuple[str, ...] = ()
    # STRING refinements (the "rigid backbone" applies to strings too, not just
    # numbers): a closed value set, a regex, or a JSON-blob check. Empty/None = no
    # extra rule. max_len bounds any string so a buggy adapter can't stream a
    # multi-MB value into current_state.
    choices: tuple[str, ...] = ()      # allowed values for an enum-like string
    pattern: str | None = None         # regex the whole string must match
    is_json: bool = False              # value must parse as JSON (metadata blobs)
    max_len: int = 4096

    def validate(self, value: Value) -> Value:
        """Return the value if valid for this capability, else raise.

        Booleans are checked before int because `bool` is a subclass of `int`
        in Python — `isinstance(True, int)` is True, so order matters.
        """
        vt = self.value_type
        if vt is ValueType.BOOL:
            if not isinstance(value, bool):
                raise CapabilityError(f"{self.kind.value}: expected bool, got {type(value).__name__}")
            return value
        if vt is ValueType.STRING:
            if not isinstance(value, str):
                raise CapabilityError(f"{self.kind.value}: expected str, got {type(value).__name__}")
            if len(value) > self.max_len:
                raise CapabilityError(f"{self.kind.value}: string too long ({len(value)} > {self.max_len})")
            if self.choices and value not in self.choices:
                raise CapabilityError(f"{self.kind.value}: {value!r} not one of {self.choices}")
            if self.pattern is not None and not re.fullmatch(self.pattern, value):
                raise CapabilityError(f"{self.kind.value}: {value!r} does not match {self.pattern}")
            if self.is_json and value != "":
                # "" is the established wire convention for "cleared" (adapters
                # blank stale metadata by publishing the empty string) — valid
                # for every JSON-carrying cap; anything else must parse.
                try:
                    json.loads(value)
                except (ValueError, TypeError) as exc:
                    raise CapabilityError(f"{self.kind.value}: invalid JSON ({exc})") from exc
            return value
        # numeric (INT or FLOAT)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CapabilityError(f"{self.kind.value}: expected number, got {type(value).__name__}")
        num = float(value)
        # Reject NaN/Inf at the boundary — a sensor with no reading reports NaN,
        # which is not valid JSON and must never reach current_state (fail loud).
        if not math.isfinite(num):
            raise CapabilityError(f"{self.kind.value}: non-finite value {value!r}")
        if vt is ValueType.INT and isinstance(value, float) and not value.is_integer():
            raise CapabilityError(f"{self.kind.value}: expected integer, got {value}")
        if self.minimum is not None and num < self.minimum:
            raise CapabilityError(f"{self.kind.value}: {num} < min {self.minimum}")
        if self.maximum is not None and num > self.maximum:
            raise CapabilityError(f"{self.kind.value}: {num} > max {self.maximum}")
        return int(num) if vt is ValueType.INT else num


# Registry of every known capability. The percent/Kelvin/Celsius ranges are
# the canonical units adapters MUST normalise to — e.g. a Zigbee bulb that
# reports brightness 0..254 is rescaled to 0..100 in the adapter, never here.
CAPABILITIES: dict[CapabilityKind, CapabilitySpec] = {
    CapabilityKind.ON_OFF: CapabilitySpec(
        CapabilityKind.ON_OFF, ValueType.BOOL, Access.READ_WRITE,
        commands=("turn_on", "turn_off", "toggle"),
    ),
    CapabilityKind.BRIGHTNESS: CapabilitySpec(
        CapabilityKind.BRIGHTNESS, ValueType.INT, Access.READ_WRITE, unit="%",
        minimum=0, maximum=100, commands=("set_brightness",),
    ),
    CapabilityKind.COLOR_TEMP: CapabilitySpec(
        CapabilityKind.COLOR_TEMP, ValueType.INT, Access.READ_WRITE, unit="K",
        minimum=1700, maximum=6535, commands=("set_color_temp",),
    ),
    # set_color carries args {value: "#RRGGBB"} (a hex colour).
    CapabilityKind.COLOR_RGB: CapabilitySpec(
        CapabilityKind.COLOR_RGB, ValueType.STRING, Access.READ_WRITE,
        commands=("set_color",), pattern=r"#[0-9a-fA-F]{6}",
    ),
    # set_effect carries args {value: "<effect name>"} from effect_options.
    CapabilityKind.EFFECT: CapabilitySpec(
        CapabilityKind.EFFECT, ValueType.STRING, Access.READ_WRITE,
        commands=("set_effect",),
    ),
    CapabilityKind.EFFECT_OPTIONS: CapabilitySpec(
        CapabilityKind.EFFECT_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.OPEN_CLOSE: CapabilitySpec(
        CapabilityKind.OPEN_CLOSE, ValueType.INT, Access.READ_WRITE, unit="%",
        minimum=0, maximum=100, commands=("open", "close", "stop", "set_position"),
    ),
    CapabilityKind.LOCK: CapabilitySpec(
        CapabilityKind.LOCK, ValueType.BOOL, Access.READ_WRITE,
        commands=("lock", "unlock"),
    ),
    # Generic state for virtual entities ("helpers") and device modes.
    CapabilityKind.BOOLEAN: CapabilitySpec(
        CapabilityKind.BOOLEAN, ValueType.BOOL, Access.READ_WRITE,
        commands=("turn_on", "turn_off", "toggle"),
    ),
    CapabilityKind.ENUM: CapabilitySpec(
        CapabilityKind.ENUM, ValueType.STRING, Access.READ_WRITE,
        commands=("set_option",),
    ),
    CapabilityKind.ENUM_OPTIONS: CapabilitySpec(
        CapabilityKind.ENUM_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.NUMBER: CapabilitySpec(
        CapabilityKind.NUMBER, ValueType.FLOAT, Access.READ_WRITE,
        commands=("set_value",),
    ),
    CapabilityKind.TIME: CapabilitySpec(
        CapabilityKind.TIME, ValueType.INT, Access.READ_WRITE, unit="min",
        minimum=0, maximum=1439, commands=("set_time",),
    ),
    CapabilityKind.NUMBER_OPTIONS: CapabilitySpec(
        CapabilityKind.NUMBER_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    # Climate (AC / heat pump / thermostat). Modes are a closed set (union of what
    # the midea + esphome adapters emit) — a typo like "colling" is rejected loud
    # rather than silently breaking a mode-comparing automation.
    CapabilityKind.HVAC_MODE: CapabilitySpec(
        CapabilityKind.HVAC_MODE, ValueType.STRING, Access.READ_WRITE,
        commands=("set_hvac_mode",),
        choices=("off", "cool", "heat", "auto", "dry", "fan_only", "heat_cool"),
    ),
    CapabilityKind.HVAC_MODE_OPTIONS: CapabilitySpec(
        CapabilityKind.HVAC_MODE_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.TARGET_TEMPERATURE: CapabilitySpec(
        CapabilityKind.TARGET_TEMPERATURE, ValueType.FLOAT, Access.READ_WRITE, unit="°C",
        minimum=4, maximum=35, commands=("set_temperature",),
    ),
    CapabilityKind.FAN_MODE: CapabilitySpec(
        CapabilityKind.FAN_MODE, ValueType.STRING, Access.READ_WRITE,
        commands=("set_fan_mode",),
        choices=("auto", "low", "medium", "high", "silent", "max",
                 "on", "off", "middle", "focus", "diffuse", "quiet", "custom"),
    ),
    CapabilityKind.FAN_MODE_OPTIONS: CapabilitySpec(
        CapabilityKind.FAN_MODE_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.FAN_SPEED: CapabilitySpec(
        CapabilityKind.FAN_SPEED, ValueType.INT, Access.READ_WRITE, unit="%",
        minimum=0, maximum=100, commands=("set_fan_speed",),
    ),
    CapabilityKind.MOWER: CapabilitySpec(
        CapabilityKind.MOWER, ValueType.STRING, Access.READ_WRITE,
        commands=("start", "pause", "dock"),
        # "charging" is a real landroid state (on-base, charging) — its absence
        # here had the engine rejecting every such report at the boundary.
        choices=("idle", "mowing", "docked", "returning", "paused", "error", "starting", "leaving", "charging"),
    ),
    CapabilityKind.MOWER_ERROR: CapabilitySpec(
        CapabilityKind.MOWER_ERROR, ValueType.STRING, Access.READ,
        # Deliberately unconstrained: the fault vocabulary is the vendor's and grows
        # with firmware. Pinning choices here would reject an unseen fault at the
        # boundary — precisely the report worth having.
    ),
    CapabilityKind.VACUUM: CapabilitySpec(
        CapabilityKind.VACUUM, ValueType.STRING, Access.READ_WRITE,
        # clean_area sends the robot to one spot on the FLOOR PLAN — args {x, y} in
        # plan percent, optional {size_m}. The plan is the only frame the house
        # speaks; translating it into whatever the robot uses is the adapter's job,
        # and it does that from the robot's own map laid over the plan.
        commands=("start", "pause", "dock", "clean_area"),
        choices=("idle", "cleaning", "docked", "returning", "paused", "error", "starting", "charging"),
    ),
    # Vacuuming and mopping are separate jobs on a robot that carries both, and which
    # ones it does is a setting in the same rank as the suction level — not a mode of
    # the run. Closed vocabulary; a robot advertises the subset it has in
    # VACUUM_MODE_OPTIONS, the way a climate device advertises its hvac modes.
    CapabilityKind.VACUUM_MODE: CapabilitySpec(
        CapabilityKind.VACUUM_MODE, ValueType.STRING, Access.READ_WRITE,
        commands=("set_vacuum_mode",),
        choices=("sweeping", "mopping", "sweeping and mopping", "mopping after sweeping"),
    ),
    CapabilityKind.VACUUM_MODE_OPTIONS: CapabilitySpec(
        CapabilityKind.VACUUM_MODE_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.NOTIFY: CapabilitySpec(
        CapabilityKind.NOTIFY, ValueType.STRING, Access.WRITE,
        commands=("notify",),
    ),
    CapabilityKind.ANNOUNCE: CapabilitySpec(
        CapabilityKind.ANNOUNCE, ValueType.STRING, Access.READ_WRITE,
        commands=("say",),
    ),
    # Media player. `play_media` carries args {uri, title?, art?, mime?} on the
    # Command — the URL/stream to cast; the adapter sets it on the renderer.
    CapabilityKind.MEDIA_TRANSPORT: CapabilitySpec(
        CapabilityKind.MEDIA_TRANSPORT, ValueType.STRING, Access.READ_WRITE,
        commands=("play", "pause", "play_pause", "stop", "next", "previous", "play_media", "play_preset", "play_queue", "play_index"),
        choices=("playing", "paused", "stopped", "idle", "buffering"),
    ),
    CapabilityKind.VOLUME: CapabilitySpec(
        CapabilityKind.VOLUME, ValueType.INT, Access.READ_WRITE, unit="%",
        minimum=0, maximum=100, commands=("set_volume", "volume_up", "volume_down"),
    ),
    CapabilityKind.MUTE: CapabilitySpec(
        CapabilityKind.MUTE, ValueType.BOOL, Access.READ_WRITE,
        commands=("mute", "unmute", "toggle"),
    ),
    CapabilityKind.MEDIA_TITLE: CapabilitySpec(
        CapabilityKind.MEDIA_TITLE, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_ARTIST: CapabilitySpec(
        CapabilityKind.MEDIA_ARTIST, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_ALBUM: CapabilitySpec(
        CapabilityKind.MEDIA_ALBUM, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_ART: CapabilitySpec(
        CapabilityKind.MEDIA_ART, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_QUALITY: CapabilitySpec(
        CapabilityKind.MEDIA_QUALITY, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_SOURCE: CapabilitySpec(
        CapabilityKind.MEDIA_SOURCE, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.MEDIA_QUEUE: CapabilitySpec(
        CapabilityKind.MEDIA_QUEUE, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.MEDIA_DURATION: CapabilitySpec(
        CapabilityKind.MEDIA_DURATION, ValueType.INT, Access.READ, unit="s", minimum=0,
    ),
    CapabilityKind.MEDIA_POSITION: CapabilitySpec(
        CapabilityKind.MEDIA_POSITION, ValueType.INT, Access.READ, unit="s", minimum=0,
    ),
    CapabilityKind.MEDIA_FAVORITES: CapabilitySpec(
        CapabilityKind.MEDIA_FAVORITES, ValueType.STRING, Access.READ, is_json=True,
    ),
    CapabilityKind.MEDIA_DISPLAY: CapabilitySpec(
        CapabilityKind.MEDIA_DISPLAY, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.SOURCE: CapabilitySpec(
        CapabilityKind.SOURCE, ValueType.STRING, Access.READ_WRITE,
        commands=("set_source",),
    ),
    CapabilityKind.VIDEO_OUTPUT: CapabilitySpec(
        CapabilityKind.VIDEO_OUTPUT, ValueType.STRING, Access.WRITE,
        commands=("set_video_output",), choices=("tv", "projector"),
    ),
    CapabilityKind.VIDEO_SELECT: CapabilitySpec(
        CapabilityKind.VIDEO_SELECT, ValueType.STRING, Access.WRITE,
        commands=("set_video_select",),
    ),
    CapabilityKind.SOURCE_OPTIONS: CapabilitySpec(
        CapabilityKind.SOURCE_OPTIONS, ValueType.STRING, Access.READ, is_json=True,
    ),
    # Tune a channel — the value is app-specific (a number for an IPTV app the
    # adapter types on the device). Options come from curated config, not the device.
    CapabilityKind.CHANNEL: CapabilitySpec(
        CapabilityKind.CHANNEL, ValueType.STRING, Access.READ_WRITE, commands=("set_channel",),
    ),
    CapabilityKind.TEMPERATURE: CapabilitySpec(
        CapabilityKind.TEMPERATURE, ValueType.FLOAT, Access.READ, unit="°C",
        minimum=-60, maximum=125,
    ),
    CapabilityKind.HUMIDITY: CapabilitySpec(
        CapabilityKind.HUMIDITY, ValueType.FLOAT, Access.READ, unit="%",
        minimum=0, maximum=100,
    ),
    CapabilityKind.ILLUMINANCE: CapabilitySpec(
        CapabilityKind.ILLUMINANCE, ValueType.FLOAT, Access.READ, unit="lx", minimum=0,
    ),
    CapabilityKind.PM25: CapabilitySpec(
        CapabilityKind.PM25, ValueType.FLOAT, Access.READ, unit="µg/m³", minimum=0,
    ),
    CapabilityKind.VOC_INDEX: CapabilitySpec(
        CapabilityKind.VOC_INDEX, ValueType.FLOAT, Access.READ, minimum=0,
    ),
    CapabilityKind.PRESSURE: CapabilitySpec(
        CapabilityKind.PRESSURE, ValueType.FLOAT, Access.READ, unit="hPa", minimum=0,
    ),
    CapabilityKind.WIND_GUST: CapabilitySpec(
        CapabilityKind.WIND_GUST, ValueType.FLOAT, Access.READ, unit="m/s", minimum=0,
        maximum=120,
    ),
    CapabilityKind.WIND_DIRECTION: CapabilitySpec(
        CapabilityKind.WIND_DIRECTION, ValueType.FLOAT, Access.READ, unit="°",
        minimum=0, maximum=360,
    ),
    CapabilityKind.DEW_POINT: CapabilitySpec(
        CapabilityKind.DEW_POINT, ValueType.FLOAT, Access.READ, unit="°C",
        minimum=-80, maximum=60,
    ),
    CapabilityKind.WIND_SPEED: CapabilitySpec(
        CapabilityKind.WIND_SPEED, ValueType.FLOAT, Access.READ, unit="m/s", minimum=0,
    ),
    CapabilityKind.SOLAR_RADIATION: CapabilitySpec(
        CapabilityKind.SOLAR_RADIATION, ValueType.FLOAT, Access.READ, unit="W/m²", minimum=0,
    ),
    CapabilityKind.UV_INDEX: CapabilitySpec(
        CapabilityKind.UV_INDEX, ValueType.FLOAT, Access.READ, minimum=0,
    ),
    CapabilityKind.RAIN_RATE: CapabilitySpec(
        CapabilityKind.RAIN_RATE, ValueType.FLOAT, Access.READ, unit="mm/h", minimum=0,
    ),
    CapabilityKind.RAIN_DAILY: CapabilitySpec(
        CapabilityKind.RAIN_DAILY, ValueType.FLOAT, Access.READ, unit="mm", minimum=0,
    ),
    CapabilityKind.POWER: CapabilitySpec(
        CapabilityKind.POWER, ValueType.FLOAT, Access.READ, unit="W",
    ),
    CapabilityKind.ENERGY: CapabilitySpec(
        CapabilityKind.ENERGY, ValueType.FLOAT, Access.READ, unit="kWh", minimum=0,
    ),
    CapabilityKind.VOLTAGE: CapabilitySpec(
        CapabilityKind.VOLTAGE, ValueType.FLOAT, Access.READ, unit="V", minimum=0,
    ),
    CapabilityKind.CURRENT: CapabilitySpec(
        CapabilityKind.CURRENT, ValueType.FLOAT, Access.READ, unit="A", minimum=0,
    ),
    CapabilityKind.FREQUENCY: CapabilitySpec(
        CapabilityKind.FREQUENCY, ValueType.FLOAT, Access.READ, unit="Hz", minimum=0,
    ),
    CapabilityKind.POWER_FACTOR: CapabilitySpec(
        CapabilityKind.POWER_FACTOR, ValueType.FLOAT, Access.READ, minimum=-1, maximum=1,
    ),
    CapabilityKind.BATTERY: CapabilitySpec(
        CapabilityKind.BATTERY, ValueType.FLOAT, Access.READ, unit="%",
        minimum=0, maximum=100,
    ),
    CapabilityKind.SIGNAL: CapabilitySpec(
        CapabilityKind.SIGNAL, ValueType.FLOAT, Access.READ, unit="dBm",
    ),
    CapabilityKind.DURATION: CapabilitySpec(
        CapabilityKind.DURATION, ValueType.FLOAT, Access.READ, unit="s", minimum=0,
    ),
    CapabilityKind.REMAINING: CapabilitySpec(
        CapabilityKind.REMAINING, ValueType.FLOAT, Access.READ, unit="s", minimum=0,
    ),
    CapabilityKind.SUN_ELEVATION: CapabilitySpec(
        CapabilityKind.SUN_ELEVATION, ValueType.FLOAT, Access.READ, unit="°",
        minimum=-90, maximum=90,
    ),
    CapabilityKind.SUN_STATE: CapabilitySpec(
        CapabilityKind.SUN_STATE, ValueType.STRING, Access.READ,
        choices=("night", "dawn", "day", "dusk"),
    ),
    CapabilityKind.NEXT_OCCURRENCE: CapabilitySpec(
        CapabilityKind.NEXT_OCCURRENCE, ValueType.STRING, Access.READ,
        pattern=r"(?:\d{4}-\d{2}-\d{2})?",
    ),
    CapabilityKind.TIME_OF_DAY: CapabilitySpec(
        CapabilityKind.TIME_OF_DAY, ValueType.INT, Access.READ, unit="min",
        minimum=0, maximum=1439,
    ),
    CapabilityKind.CONTACT: CapabilitySpec(
        CapabilityKind.CONTACT, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.MOTION: CapabilitySpec(
        CapabilityKind.MOTION, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.OCCUPANCY: CapabilitySpec(
        CapabilityKind.OCCUPANCY, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.SMOKE: CapabilitySpec(
        CapabilityKind.SMOKE, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.CONNECTIVITY: CapabilitySpec(
        CapabilityKind.CONNECTIVITY, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.SCHEDULE_ACTIVE: CapabilitySpec(
        CapabilityKind.SCHEDULE_ACTIVE, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.BUTTON: CapabilitySpec(
        CapabilityKind.BUTTON, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.PRESS: CapabilitySpec(
        CapabilityKind.PRESS, ValueType.STRING, Access.WRITE, commands=("press",),
    ),
    CapabilityKind.TEXT: CapabilitySpec(
        CapabilityKind.TEXT, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.BINARY: CapabilitySpec(
        CapabilityKind.BINARY, ValueType.BOOL, Access.READ,
    ),
    CapabilityKind.MEASUREMENT: CapabilitySpec(
        CapabilityKind.MEASUREMENT, ValueType.FLOAT, Access.READ,
    ),
    CapabilityKind.LOCATION: CapabilitySpec(
        CapabilityKind.LOCATION, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.LATITUDE: CapabilitySpec(
        CapabilityKind.LATITUDE, ValueType.FLOAT, Access.READ, unit="°",
        minimum=-90, maximum=90,
    ),
    CapabilityKind.LONGITUDE: CapabilitySpec(
        CapabilityKind.LONGITUDE, ValueType.FLOAT, Access.READ, unit="°",
        minimum=-180, maximum=180,
    ),
    # Vision (BABA). All READ — DIDA never commands the NVR (v1); it consumes.
    # `camera` is a JSON descriptor (max_len guards a buggy publisher). The
    # object-class set is canonical: the adapter maps BABA's raw COCO class_name
    # onto one of these at the boundary, so a novel class is folded to "other"
    # rather than rejected. "none" is the cleared state (nothing in view).
    CapabilityKind.CAMERA: CapabilitySpec(
        CapabilityKind.CAMERA, ValueType.STRING, Access.READ, is_json=True, max_len=2048,
    ),
    CapabilityKind.PERSON_COUNT: CapabilitySpec(
        CapabilityKind.PERSON_COUNT, ValueType.INT, Access.READ, minimum=0,
    ),
    CapabilityKind.OBJECT_CLASS: CapabilitySpec(
        CapabilityKind.OBJECT_CLASS, ValueType.STRING, Access.READ,
        choices=("person", "vehicle", "animal", "other", "none"),
    ),
    CapabilityKind.SCENE_STATE: CapabilitySpec(
        CapabilityKind.SCENE_STATE, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.LIGHT_CONDITION: CapabilitySpec(
        CapabilityKind.LIGHT_CONDITION, ValueType.STRING, Access.READ,
        choices=("ir", "dark", "dim", "normal", "bright"),
    ),
    CapabilityKind.PARKED_VEHICLE: CapabilitySpec(
        CapabilityKind.PARKED_VEHICLE, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.IDENTITY_PRESENCE: CapabilitySpec(
        CapabilityKind.IDENTITY_PRESENCE, ValueType.STRING, Access.READ,
        choices=("absent", "body", "face"),
    ),
    CapabilityKind.IDENTITY_SINCE: CapabilitySpec(
        CapabilityKind.IDENTITY_SINCE, ValueType.STRING, Access.READ,
    ),
    CapabilityKind.OBJECT_PRESENCE: CapabilitySpec(
        CapabilityKind.OBJECT_PRESENCE, ValueType.STRING, Access.READ,
        choices=("absent", "pet", "vehicle"),
    ),
}


def _spec(capability: str) -> CapabilitySpec:
    try:
        kind = CapabilityKind(capability)
    except ValueError as exc:
        raise CapabilityError(f"unknown capability {capability!r}") from exc
    return CAPABILITIES[kind]


def validate_state(capability: str, value: Value) -> Value:
    """Validate an inbound state update at the bus boundary.

    Returns the normalised value on success; raises `CapabilityError` on any
    unknown capability, wrong type, or out-of-range value. The engine drops
    rejected updates — they never enter `current_state`.
    """
    spec = _spec(capability)
    # WRITE-only capabilities (notify/press) have no readable state — a state
    # update for one is a contract violation, not a value to store.
    if spec.access is Access.WRITE:
        raise CapabilityError(f"{capability}: write-only, has no readable state")
    return spec.validate(value)


def validate_command(capability: str, command: str) -> None:
    """Validate an outbound command against the capability's command set."""
    spec = _spec(capability)
    if spec.access is Access.READ:
        raise CapabilityError(f"{capability}: read-only, no commands")
    if command not in spec.commands:
        raise CapabilityError(
            f"{capability}: unknown command {command!r}; allowed: {spec.commands}"
        )


def validate_runtime_options(capability: str, value: Value, options: str | None) -> None:
    """Range/choice-check a generic NUMBER or ENUM against the entity's OWN limits.

    `number` and `enum` are deliberately open at the spec level — they are the
    escape hatch for a device whose range or option list only exists at runtime,
    published alongside as `number_options` ({min,max,step,…}) / `enum_options`
    (a JSON list). That means the static spec alone accepts `set_value {value:
    1e308}` or an option the device never offered. Callers that hold the sibling
    metadata (the API and automation engine read current_state) pass it here so the
    boundary means what it says. No options published → nothing to check."""
    if not options:
        return
    try:
        parsed = json.loads(options)
    except (TypeError, ValueError):
        return  # malformed metadata is the adapter's bug, not a reason to block a command
    if capability == CapabilityKind.ENUM.value:
        if isinstance(parsed, list) and parsed and value not in parsed:
            raise CapabilityError(f"{capability}: {value!r} is not one of the device's options {parsed}")
        return
    if capability == CapabilityKind.NUMBER.value and isinstance(parsed, dict):
        if not isinstance(value, int | float) or isinstance(value, bool):
            return  # the spec check already rejected a non-number
        lo, hi = parsed.get("min"), parsed.get("max")
        if isinstance(lo, int | float) and value < lo:
            raise CapabilityError(f"{capability}: {value} is below the device minimum {lo}")
        if isinstance(hi, int | float) and value > hi:
            raise CapabilityError(f"{capability}: {value} is above the device maximum {hi}")


def validate_command_args(capability: str, command: str, args: dict | None,
                          options: str | None = None) -> None:
    """Validate a command AND its arguments before it's published to the bus.

    `validate_command` only checks the command name; this also range/type-checks
    the payload so garbage never reaches an adapter (`set_brightness {value:
    100000}`, `set_temperature {value: "high"}`, `set_hvac_mode {value:
    "colling"}` are all rejected). Every `set_<x>` command sets the capability's
    OWN value, so the value arg is validated against the capability's own spec;
    non-value commands (play/toggle/say/…) carry free-form args and are only
    name-checked.

    Every argument must be a scalar: msgspec encodes a list or dict happily, but
    adapters decode `Command.args` as scalars and drop the whole command as
    undecodable — the sender would report success for a command that evaporated."""
    validate_command(capability, command)
    for k, v in (args or {}).items():
        if not isinstance(v, bool | int | float | str):
            raise CapabilityError(
                f"argument {k!r} must be a scalar (bool/int/float/str), got {type(v).__name__}")
    if command.startswith("set_"):
        # Every set_<x> REQUIRES its value — an argless or mis-keyed setter
        # (set_brightness {}, set_position {position: 50}) must be rejected here,
        # not KeyError/silently no-op at the adapter. "garbage never reaches an
        # adapter" is the contract; a missing value is garbage too.
        if not (isinstance(args, dict) and "value" in args):
            raise CapabilityError(f"{capability}: {command} requires a 'value' argument")
        _spec(capability).validate(args["value"])
        # …and, for the open-by-design generic caps, against the device's own
        # published limits when the caller supplied them.
        validate_runtime_options(capability, args["value"], options)


def setter_command(capability: str, value: Value) -> tuple[str, dict] | None:
    """The (command, args) that drives `capability` back to `value`, or None if the
    capability has no settable state (a sensor, or a momentary verb like press /
    play / notify) and so doesn't belong in a scene snapshot.

    This is the canonical "how do I set X to V" lookup that scene recall uses to
    turn a captured state snapshot back into commands. It reads the same command
    vocabulary the spec declares:
      * an explicit value-setter (`set_brightness`, `set_temperature`, `set_color`,
        `set_position`, `set_hvac_mode`, …) → that command with {value};
      * otherwise a boolean on/off pair → turn_on/turn_off, lock/unlock, or
        open/close by the boolean;
      * anything else (press, play/pause, say, vacuum start/dock) → None (skip)."""
    try:
        spec = _spec(capability)
    except CapabilityError:
        return None
    if spec.access is Access.READ:
        return None  # a sensor — nothing to command
    # Boolean pairs FIRST: a capability can carry both a value-setter and an on/off
    # pair (open_close has set_position + open/close), and for a bool the pair is the
    # right answer — `set_position {value: True}` is what the set_-scan used to return
    # and validate_command_args rightly rejects it (position wants a number).
    if isinstance(value, bool):
        cmds = spec.commands
        if "turn_on" in cmds and "turn_off" in cmds:
            return ("turn_on" if value else "turn_off"), {}
        if "lock" in cmds and "unlock" in cmds:
            return ("lock" if value else "unlock"), {}
        if "open" in cmds and "close" in cmds:
            return ("open" if value else "close"), {}
    for command in spec.commands:
        if command.startswith("set_"):
            return command, {"value": value}
    return None


# ── LLM-facing rendering ─────────────────────────────────────────────────────
# The assistant (services/api/dida_api/assistant.py) has to tell a model which
# commands exist before it can drive anything. That list was hand-written in the
# prompt and covered 5 of the ~24 writable capabilities, so the model guessed
# command names for media/climate/mower and `validate_command` rejected them —
# loud, correct, and useless to the user. Render it from the registry instead:
# the prompt cannot drift from the vocabulary the boundary enforces.

# Capabilities whose value is a JSON list of the OPTIONS a sibling capability
# accepts (source_options carries what set_source will take). Derived, not
# listed — a new `<x>_options` capability joins automatically.
OPTION_CAPABILITIES: frozenset[str] = frozenset(
    kind.value for kind in CAPABILITIES if kind.value.endswith("_options")
)

# Everything whose VALUE enumerates the choices a sibling command will accept.
# `<x>_options` by name, plus media_favorites — the same idea under an older name:
# it is the list of presets `play_preset` takes, and a caller that cannot read it
# has to guess a preset number.
CHOICE_CAPABILITIES: frozenset[str] = OPTION_CAPABILITIES | {
    CapabilityKind.MEDIA_FAVORITES.value
}


def _value_hint(spec: CapabilitySpec) -> str:
    """How to describe a setter's `value` argument to a model."""
    if spec.choices:
        return "one of " + ", ".join(spec.choices)
    if spec.pattern is not None:
        return f"string matching {spec.pattern}"
    if spec.value_type is ValueType.BOOL:
        return "true or false"
    if spec.value_type in (ValueType.INT, ValueType.FLOAT):
        rng = ""
        if spec.minimum is not None and spec.maximum is not None:
            rng = f" {spec.minimum:g}..{spec.maximum:g}"
        kindname = "integer" if spec.value_type is ValueType.INT else "number"
        return f"{kindname}{rng}{(' ' + spec.unit) if spec.unit else ''}"
    return f"the {spec.kind.value} value (see <cap>_options if present)"


def command_vocabulary() -> str:
    """Every writable capability and its exact command set, one per line.

    The `set_<x>` value shape comes from the same spec `validate_command_args`
    checks against, so a range or choice list can never be stale here.
    """
    lines: list[str] = []
    for kind, spec in CAPABILITIES.items():
        if not spec.commands:
            continue
        line = f"  {kind.value} → {' / '.join(spec.commands)}"
        setter = next((c for c in spec.commands if c.startswith("set_")), None)
        if setter is not None:
            # Name the setter only when the capability has more than one command;
            # repeating it for a lone `set_x` is noise the model pays for.
            which = "" if len(spec.commands) == 1 else f"{setter} "
            line += f'  [{which}value: {_value_hint(spec)}]'
        lines.append(line)
    return "\n".join(lines)


def readable_capabilities() -> str:
    """Read-only capabilities (sensors + metadata), comma-separated.

    These accept no command — naming them stops a model from inventing one. Those
    with a CLOSED value set carry it, because a sensor is the most common thing to
    TRIGGER on and the trigger has to name the value it fires on: told only that
    `sun_state` exists, a model will happily invent `above_horizon` and be rejected
    at the boundary. Choices are pipe-separated so the list itself stays splittable
    on ", ".
    """
    parts = []
    for kind, spec in sorted(CAPABILITIES.items(), key=lambda kv: kv[0].value):
        if spec.access is not Access.READ:
            continue
        parts.append(
            f"{kind.value} ({'|'.join(spec.choices)})" if spec.choices else kind.value
        )
    return ", ".join(parts)


# ── Device type ──────────────────────────────────────────────────────────────
# The canonical KIND of a device, derived from the capabilities it exposes. This
# is DIDA's OWN classification — its single source of truth, seeded once and then
# owned by the user (see entities.device_type / the engine's seed-once upsert).
# The adapter's native type hint only *seeds* it; it never overrides it after.

# The vocabulary of entities.device_type. Mirrors the frontend DeviceType union
# (ui/src/lib/capabilities.ts) — keep the two in sync.
DEVICE_TYPES: frozenset[str] = frozenset(
    {"light", "switch", "cover", "lock", "media", "remote", "presence", "sensor", "button", "other"}
)

# Capabilities that alone mark an entity as a sensor — the catch-all AFTER the
# actuator checks below (a light also reports power, so on_off wins first).
# Mirrors SENSOR_CAPS in the frontend.
_SENSOR_CAPS: frozenset[str] = frozenset({
    "temperature", "humidity", "illuminance", "power", "energy", "voltage",
    "current", "frequency", "power_factor", "battery", "signal", "duration",
    "contact", "motion", "occupancy", "connectivity", "text", "light_condition",
    "wind_gust", "wind_direction", "dew_point",
})

# Adapter-native type hint → our canonical DeviceType. An on/off light and a
# relay both expose only `on_off`, so caps can't tell them apart — the adapter's
# hint can (ESPHome's `light` platform → "light"). Mirrors ETYPE_ICON.
_ADAPTER_TYPE: dict[str, str] = {
    "light": "light", "switch": "switch", "cover": "cover", "lock": "lock",
    "sensor": "sensor", "binary_sensor": "sensor", "button": "button",
    "media_player": "media", "media": "media", "remote": "remote",
    "presence": "presence", "other": "other",
}


def classify_device_type(capabilities: object) -> str:
    """DIDA's canonical device type from an entity's capability set (mirrors the
    frontend deviceType()). Order matters: actuators before the sensor catch-all.
    Always returns a concrete type ("other" as the last resort)."""
    caps = set(capabilities) if capabilities else set()
    if "media_transport" in caps:
        return "media"
    # A universal remote that carries both on ONE entity: an activity/source picker
    # + momentary `press` keys. (Harmony splits these across a hub + button siblings
    # and seeds "remote" from the adapter hint instead, so this is the fallback for a
    # future single-entity remote — not reachable from the split layout.)
    if "source" in caps and "press" in caps:
        return "remote"
    if "location" in caps:
        return "presence"
    if "lock" in caps:
        return "lock"
    if "open_close" in caps:
        return "cover"
    if "on_off" in caps:
        return "light" if ("brightness" in caps or "color_temp" in caps) else "switch"
    if "button" in caps:
        return "button"
    if caps & _SENSOR_CAPS:
        return "sensor"
    return "other"


def resolve_device_type(adapter_hint: str | None, capabilities: object) -> str:
    """The value to SEED entities.device_type with: the adapter's native type
    hint mapped onto our canonical vocabulary, else classified from capabilities.
    Never null — every entity gets a concrete type."""
    if adapter_hint:
        mapped = _ADAPTER_TYPE.get(adapter_hint)
        if mapped:
            return mapped
    return classify_device_type(capabilities)
