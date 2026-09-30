// UI-side projection of the canonical capability model. The backbone contract
// lives in core/src/dida_core/capabilities.py — this mirrors it for display and
// decides which control to render. Labels are translated via i18n (cap.* / cmd.*);
// this file only holds STRUCTURE (control kind, units, ranges, command ids).

import { formatNumber } from "$lib/kit";
import { t, type MessageKey } from "$lib/i18n";

import { clock } from "$lib/dt";
import { tr } from "$lib/translations.svelte";

export type CapabilityKind =
  | "on_off" | "brightness" | "color_temp" | "color_rgb" | "effect" | "open_close" | "lock"
  | "boolean" | "enum" | "number" | "press" | "fan_speed" | "mower" | "mower_error" | "vacuum"
  | "vacuum_mode"
  | "hvac_mode" | "fan_mode" | "target_temperature" | "announce" | "notify"
  | "temperature" | "humidity" | "illuminance" | "pm25" | "voc_index" | "power" | "energy"
  | "pressure" | "wind_speed" | "wind_gust" | "wind_direction" | "dew_point"
  | "solar_radiation" | "uv_index" | "rain_rate" | "rain_daily"
  | "voltage" | "current" | "frequency" | "power_factor" | "signal" | "duration" | "remaining"
  | "battery" | "contact" | "motion" | "occupancy" | "smoke" | "connectivity" | "schedule_active" | "binary" | "button" | "text"
  | "measurement" | "location" | "latitude" | "longitude" | "source"
  | "camera" | "person_count" | "object_class" | "scene_state" | "parked_vehicle" | "light_condition"
  | "identity_presence" | "identity_since" | "object_presence";

// How the UI renders / drives a capability.
//   toggle  — boolean actuator (on_off, lock)
//   slider  — 0..max numeric actuator (brightness, color_temp, open_close)
//   sensor  — read-only numeric value with a unit
//   binary  — read-only boolean state shown as a labelled badge
//   event   — momentary; show the last action string
//   enum    — pick one option from a list (device mode, virtual select helper)
//   mower   — robotic mower: state badge + start/dock/pause actions
//   camera  — live camera view: a stream descriptor rendered as a snapshot/MJPEG image
export type ControlKind = "toggle" | "slider" | "sensor" | "binary" | "event" | "enum" | "mower" | "color" | "number" | "press" | "camera";

export interface CapMeta {
  control: ControlKind;
  unit?: string;            // fallback unit if a value carries none
  min?: number;
  max?: number;
  step?: number;
  cmd?: string;             // the write command for slider/number/enum controls
}

export const CAP_META: Record<CapabilityKind, CapMeta> = {
  on_off:      { control: "toggle" },
  lock:        { control: "toggle" },
  boolean:     { control: "toggle" },
  enum:        { control: "enum", cmd: "set_option" },
  source:      { control: "enum", cmd: "set_source" },  // AV/remote input picker; options from source_options
  number:      { control: "number", cmd: "set_value" },
  press:       { control: "press", cmd: "press" },
  fan_speed:   { control: "slider", unit: "%", min: 0, max: 100, step: 1, cmd: "set_fan_speed" },
  hvac_mode:   { control: "enum", cmd: "set_hvac_mode" },
  fan_mode:    { control: "enum", cmd: "set_fan_mode" },
  target_temperature: { control: "number", unit: "°C", min: 4, max: 35, step: 0.5, cmd: "set_temperature" },
  mower:       { control: "mower" },
  mower_error: { control: "sensor" },
  vacuum:      { control: "mower" },  // same robot control: state badge + start/pause/dock
  vacuum_mode: { control: "enum", cmd: "set_vacuum_mode" },  // options from vacuum_mode_options
  announce:    { control: "event" },  // command-only TTS (driven from Settings → TTS); no device-card control
  notify:      { control: "event" },  // command-only push (ntfy / web push); targets never carry state
  brightness:  { control: "slider", unit: "%", min: 0, max: 100, step: 1, cmd: "set_brightness" },
  color_temp:  { control: "slider", unit: "K", min: 1700, max: 6535, step: 50, cmd: "set_color_temp" },
  color_rgb:   { control: "color" },
  effect:      { control: "enum", cmd: "set_effect" },
  open_close:  { control: "slider", unit: "%", min: 0, max: 100, step: 1, cmd: "set_position" },
  binary:      { control: "binary" },
  measurement: { control: "sensor" },
  temperature: { control: "sensor", unit: "°C" },
  humidity:    { control: "sensor", unit: "%" },
  illuminance: { control: "sensor", unit: "lx" },
  pm25:        { control: "sensor", unit: "µg/m³" },
  voc_index:   { control: "sensor" },
  pressure:        { control: "sensor", unit: "hPa" },
  wind_speed:      { control: "sensor", unit: "m/s" },
  wind_gust:       { control: "sensor", unit: "m/s" },
  wind_direction:  { control: "sensor", unit: "°" },
  dew_point:       { control: "sensor", unit: "°C" },
  solar_radiation: { control: "sensor", unit: "W/m²" },
  uv_index:        { control: "sensor" },
  rain_rate:       { control: "sensor", unit: "mm/h" },
  rain_daily:      { control: "sensor", unit: "mm" },
  power:       { control: "sensor", unit: "W" },
  energy:      { control: "sensor", unit: "kWh" },
  voltage:     { control: "sensor", unit: "V" },
  current:     { control: "sensor", unit: "A" },
  frequency:   { control: "sensor", unit: "Hz" },
  power_factor: { control: "sensor" },
  signal:      { control: "sensor", unit: "dBm" },
  duration:    { control: "sensor", unit: "" }, // formatValue embeds the unit (min / h)
  remaining:   { control: "sensor", unit: "" }, // appliance time-left; formatValue → "45 min"
  battery:     { control: "sensor", unit: "%" },
  contact:     { control: "binary" },
  motion:      { control: "binary" },
  occupancy:   { control: "binary" },
  smoke:       { control: "binary" },
  connectivity: { control: "binary" },
  schedule_active: { control: "binary" },
  button:      { control: "event" },
  text:        { control: "sensor" },
  // Vision (BABA): a camera view + read-only detection sensors.
  camera:       { control: "camera" },
  person_count: { control: "sensor" },
  object_class: { control: "sensor" },
  scene_state:  { control: "sensor" },
  light_condition: { control: "sensor" },  // how much light the camera itself has: ir|dark|dim|normal|bright
  parked_vehicle: { control: "sensor" },  // the named vehicle standing in the scene's place; "" when empty
  identity_presence: { control: "sensor" },  // a recognised person at a camera: absent|body|face
  identity_since: { control: "sensor" },  // ISO start of that person's current stay; "" when absent — footnote of identity_presence, never its own row
  object_presence: { control: "sensor" },  // a recognised pet/vehicle at a camera: absent|pet|vehicle
  location:    { control: "sensor" },
  latitude:    { control: "sensor", unit: "°" },
  longitude:   { control: "sensor", unit: "°" },
};

export function capMeta(capability: string): CapMeta {
  return CAP_META[capability as CapabilityKind] ?? { control: "sensor" };
}

// Numeric sensor caps kept OFF the floor-plan value label: battery / signal are
// diagnostic noise; person_count is a camera metric surfaced via the person pip (and
// CameraWall), never a bare "0/1" on an ambient value pill. Not ambient readings.
export const PLAN_HIDDEN_READINGS = new Set<string>(["battery", "signal", "person_count"]);

// The option blobs are metadata feeding a control — a slider's bounds, an enum's
// list. They are never a reading, so nothing that shows values shows these.
export const OPTION_CAPS = new Set<string>([
  "enum_options", "effect_options", "number_options",
  "hvac_mode_options", "fan_mode_options", "vacuum_mode_options", "source_options",
]);

/** Translated capability label, e.g. "Napajanje" / "Power". */
export function capLabel(capability: string): string {
  return t(`cap.${capability}` as MessageKey);
}

/** Translated robot run-state ("docked" → "Na bazi"). State-keyed, so the mower
 *  and the vacuum share it ("cleaning" is the vacuum's "mowing"). Shared by the
 *  control row and the state-timeline chart so the two never drift. */
/** How a closed vocabulary reads in a picker: localised if the dictionary has it,
 *  and starting with a capital either way. The value sent stays the raw token —
 *  adapters emit English, only the label is for people. */
export function optionLabel(o: string): string {
  const s = tr(o);
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function mowerStateLabel(s: string): string {
  switch (s) {
    case "docked": return t("mower.docked");
    case "charging": return t("mower.charging");
    case "mowing": return t("mower.mowing");
    case "cleaning": return t("vacuum.cleaning");
    case "paused": return t("mower.paused");
    case "returning": return t("mower.returning");
    case "starting": return t("mower.starting");
    case "error": return t("mower.error");
    default: return t("mower.idle");
  }
}

/** Translated mower fault ("wire missing" → "ne vidi graničnu žicu"). The vendor
 *  keeps adding codes, so an untranslated one falls through as its English text
 *  rather than disappearing behind a placeholder. */
export function mowerErrorLabel(s: string): string {
  const key = `mowererr.${s}`;
  const label = t(key as MessageKey);
  return label !== key ? label : s;
}

// Canonical y-axis order for the robot state timeline: rest at the bottom,
// activity on top, error above everything (it should jump out of the chart).
export const MOWER_STATE_ORDER = ["docked", "charging", "idle", "paused", "returning", "starting", "mowing", "cleaning", "error"];

// Capabilities whose binary state has dedicated on/off labels.
const BADGE_CAPS = new Set<string>(["lock", "contact", "motion", "occupancy", "connectivity", "schedule_active", "smoke"]);

/** Translated badge for a binary/lock state (true/false). */
export function capBadge(capability: string, on: boolean): string {
  if (BADGE_CAPS.has(capability)) {
    return t(`cap.${capability}.${on ? "on" : "off"}` as MessageKey);
  }
  return t(on ? "bool.yes" : "bool.no");
}

// ── scene regions (BABA places: a parking spot, a gate) ───────────────────────
// The state vocabulary is BABA's, operator-defined per region, so these read it
// rather than mapping it: `sceneLit` says the region is live (gate open, place
// taken), which is what colours a chip on the camera wall and a marker on the
// plan. One definition, so the two surfaces cannot disagree.
export const sceneLit = (state: string): boolean => /^(open|present|occupied)$/i.test(state.trim());

export interface ParkedVehicle { name: string; since: string }

/** {"name","since"} while a vehicle stands in the region's place, "" when not.
 *  `name` may be empty: BABA's registry says "occupied, unidentified" from the
 *  moment a car parks until its plate is read, and that must stay distinct from
 *  an empty place — treating it as null made the memory of the PREVIOUS car
 *  show over a spot something else was standing in.
 *  A malformed payload degrades to null — no chip, never a broken surface. */
export function parkedVehicle(raw: unknown): ParkedVehicle | null {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const v = JSON.parse(raw);
    return typeof v?.name === "string" ? { name: v.name, since: String(v.since ?? "") } : null;
  } catch { return null; }
}

/** The history lane a parked-vehicle reading belongs to: who stood there. Keyed
 *  on the raw descriptor, every stay was a lane of its own, since `since` differs. */
export function parkedVehicleLane(raw: string): string {
  if (!raw) return "vacant";
  const v = parkedVehicle(raw);
  return v?.name ? `parked:${v.name}` : "unidentified";
}

export function parkedVehicleLaneLabel(lane: string): string {
  if (lane === "vacant") return t("cap.parkedVehicle.vacant");
  if (lane === "unidentified") return t("cap.parkedVehicle.unknown");
  return lane.slice("parked:".length);
}

/** The local clock time a vehicle took the place ("16:46"); "" if unparseable. */
export function parkedSince(iso: string): string {
  const d = new Date(iso);
  return clock(d);
}

/** How long the vehicle has stood there ("2 h 10 min"), against a caller-supplied
 *  now (clock.now) so the reading ages with the shared minute hand. */
export function parkedFor(iso: string, now: number): string {
  const t0 = new Date(iso).getTime();
  return isNaN(t0) ? "" : formatDuration(Math.max(0, (now - t0) / 1000));
}

// ── presence pip (floor-plan markers) ─────────────────────────────────────────
// A marker may fold several presence entities (a camera's motion + person_count, a
// mmWave board's raw mmwave/pir binaries + its debounced occupancy). Read the pip
// from the CONSIDERED verdict and ignore the raw latches below it — those hold
// "true" on any object or miss their clear edge and go stale.
type CapsLike = Record<string, { value: unknown } | undefined>;

/** Does this marker carry any presence signal at all (→ it shows a person pip)? */
export function hasPresenceCap(capSets: CapsLike[]): boolean {
  return capSets.some((c) => "person_count" in c || "occupancy" in c || "motion" in c);
}

/** Is the marker occupied? Precedence `person_count` ▸ `occupancy` ▸ `motion`:
 *   • person_count (a camera): green only when it sees a PERSON — its `motion` cap
 *     latches on ANY tracked object (car / animal / shadow), so it must not turn the
 *     person icon green alone.
 *   • occupancy (a mmWave/PIR board's debounced presence, or a BABA zone): the
 *     managed truth — a raw `motion` binary that stuck "true" (ESPHome only emits on
 *     change, so a missed clear edge goes stale) must NOT override its "clear".
 *   • motion only (a bare PIR): the raw trigger is all there is. */
export function presenceOccupied(capSets: CapsLike[]): boolean {
  if (capSets.some((c) => "person_count" in c))
    return capSets.some((c) => typeof c["person_count"]?.value === "number" && (c["person_count"]!.value as number) > 0);
  if (capSets.some((c) => "occupancy" in c))
    return capSets.some((c) => c["occupancy"]?.value === true);
  return capSets.some((c) => c["motion"]?.value === true);
}

// Stable display ordering: actuators first, then sensors, then events.
const ORDER: CapabilityKind[] = [
  "camera",
  "on_off", "brightness", "color_temp", "color_rgb", "effect", "open_close", "lock", "boolean", "source", "enum", "mower", "press",
  "temperature", "humidity", "illuminance",
  "pressure", "wind_speed", "wind_gust", "wind_direction", "dew_point",
  "solar_radiation", "uv_index", "rain_rate",
  "remaining", "power", "energy", "voltage", "current", "frequency", "power_factor",
  "battery", "contact", "motion", "occupancy", "connectivity",
  "person_count", "object_class", "scene_state", "parked_vehicle", "identity_presence", "object_presence", "button", "text",
  "light_condition",
  "location", "latitude", "longitude",
  "signal", "duration",
];

export function capRank(capability: string): number {
  const i = ORDER.indexOf(capability as CapabilityKind);
  return i === -1 ? ORDER.length : i;
}

// ── Domain grouping ───────────────────────────────────────────────────────────
// For admin surfaces that must list EVERY capability (retention capability→class
// map), a flat 50-item grid is unreadable. These groups give it structure.
// Presentational only — the canonical model in capabilities.py does not group.
// The list is deliberately broad (covers caps the backend reports that aren't in
// CAP_META: co2/gas/rain/production/sun_elevation/media_*/…); anything unlisted
// falls to the trailing "other" bucket, so a new capability never disappears.
export const CAP_GROUPS: { key: MessageKey; caps: string[] }[] = [
  { key: "capgroup.environment", caps: [
    "temperature", "humidity", "co2", "gas", "pm25", "voc_index", "pressure",
    "illuminance", "wind_speed", "wind_gust", "wind_direction", "dew_point",
    "rain", "rain_rate", "rain_daily", "uv_index",
    "solar_radiation", "sun_elevation", "sun_state"] },
  { key: "capgroup.energy", caps: [
    "power", "energy", "voltage", "current", "frequency", "power_factor", "production"] },
  { key: "capgroup.presence", caps: [
    "presence", "motion", "occupancy", "contact", "person_count", "object_class",
    "scene_state", "parked_vehicle", "identity_presence", "object_presence", "camera", "smoke",
    "light_condition"] },
  { key: "capgroup.media", caps: [
    "media_transport", "media_title", "media_artist", "media_album", "media_art",
    "media_display", "media_duration", "media_favorites", "media_position",
    "media_quality", "media_queue", "media_source", "channel", "source",
    "source_options", "mute"] },
  { key: "capgroup.controls", caps: [
    "on_off", "boolean", "brightness", "color_rgb", "color_temp", "effect",
    "open_close", "lock", "target_temperature", "hvac_mode", "hvac_mode_options",
    "fan_mode", "fan_mode_options", "fan_speed", "enum", "enum_options", "number",
    "number_options", "mower", "vacuum_mode", "vacuum_mode_options", "button", "press",
    "announce", "notify"] },
  { key: "capgroup.location", caps: [
    "location", "latitude", "longitude", "time_of_day", "remaining", "duration",
    "schedule_active"] },
  { key: "capgroup.diagnostics", caps: [
    "battery", "signal", "connectivity", "measurement", "binary", "text"] },
];

const CAP_GROUP_OF = new Map<string, number>();
CAP_GROUPS.forEach((g, i) => g.caps.forEach((c) => CAP_GROUP_OF.set(c, i)));

/** Group index for a capability; CAP_GROUPS.length = the trailing "other" bucket. */
export function capGroupIndex(cap: string): number {
  return CAP_GROUP_OF.get(cap) ?? CAP_GROUPS.length;
}

// Retention-class accent colours, keyed by the canonical class NAME (the fixed
// slug, not the user-editable label). Gives each capability row a left border
// tinting it by its class so same-class caps read as a cluster; a custom class
// falls back to a neutral grey.
const CLASS_COLOR: Record<string, string> = {
  default: "#6b7280", climate: "#34d399", presence: "#60a5fa",
  discrete: "#f59e0b", solar: "#fbbf24", utility: "#a78bfa",
};
export const classColor = (name: string): string => CLASS_COLOR[name] ?? "#6b7280";

// Commands per capability (mirrors core's command sets). A command with a
// `value` kind carries one argument the rule builder collects with a matching
// input: number, a colour picker, free text, or a dropdown of the entity's
// <cap>_options (effect_options / enum_options). `argKey` names that argument
// (default "value") — say carries {text}, notify carries {message}.
export type ActionValueKind = "number" | "color" | "text" | "option";
export interface CommandOpt {
  command: string;
  value?: ActionValueKind;
  argKey?: string;
}

export const COMMANDS: Partial<Record<CapabilityKind, CommandOpt[]>> = {
  on_off: [{ command: "turn_on" }, { command: "turn_off" }, { command: "toggle" }],
  boolean: [{ command: "turn_on" }, { command: "turn_off" }, { command: "toggle" }],
  mower: [{ command: "start" }, { command: "pause" }, { command: "dock" }],
  lock: [{ command: "lock" }, { command: "unlock" }],
  brightness: [{ command: "set_brightness", value: "number" }],
  color_temp: [{ command: "set_color_temp", value: "number" }],
  color_rgb: [{ command: "set_color", value: "color" }],
  effect: [{ command: "set_effect", value: "option" }],
  enum: [{ command: "set_option", value: "option" }],
  vacuum_mode: [{ command: "set_vacuum_mode", value: "option" }],
  number: [{ command: "set_value", value: "number" }],
  press: [{ command: "press" }],
  source: [{ command: "set_source", value: "option" }],
  fan_speed: [{ command: "set_fan_speed", value: "number" }],
  hvac_mode: [{ command: "set_hvac_mode", value: "option" }],
  fan_mode: [{ command: "set_fan_mode", value: "option" }],
  target_temperature: [{ command: "set_temperature", value: "number" }],
  announce: [{ command: "say", value: "text", argKey: "text" }],
  notify: [{ command: "notify", value: "text", argKey: "message" }],
  open_close: [
    { command: "open" }, { command: "close" }, { command: "stop" },
    { command: "set_position", value: "number" },
  ],
};

export function commandsFor(capability: string): CommandOpt[] {
  return COMMANDS[capability as CapabilityKind] ?? [];
}

/** Translated command label, e.g. "Uključi" / "Turn on". */
export function cmdLabel(command: string): string {
  return t(`cmd.${command}` as MessageKey);
}

export function isActuator(capability: string): boolean {
  return commandsFor(capability).length > 0;
}

// ── Device typing (derived) ──────────────────────────────────────────────
// The backend leaves device_type null, so we classify a device from the
// capabilities it exposes. Order matters: a light also reports power, so the
// actuator check wins before the sensor catch-all.
export type DeviceType =
  | "light" | "switch" | "cover" | "lock" | "media" | "remote" | "presence" | "mower" | "sensor" | "button" | "other";

// Filter/group display order.
export const DEVICE_TYPES: DeviceType[] = [
  "light", "switch", "cover", "lock", "media", "remote", "presence", "mower", "sensor", "button", "other",
];

const SENSOR_CAPS = new Set<string>([
  "temperature", "humidity", "illuminance", "power", "energy", "voltage",
  "current", "frequency", "power_factor", "battery", "signal", "duration",
  "contact", "motion", "occupancy", "connectivity", "text",
]);

export function deviceType(caps: Record<string, unknown>): DeviceType {
  const has = (c: string): boolean => c in caps;
  if (has("media_transport")) return "media";
  if (has("mower")) return "mower";
  if (has("location")) return "presence";
  if (has("lock")) return "lock";
  if (has("open_close")) return "cover";
  if (has("on_off")) return has("brightness") || has("color_temp") ? "light" : "switch";
  if (has("button")) return "button";
  if (Object.keys(caps).some((c) => SENSOR_CAPS.has(c))) return "sensor";
  return "other";
}

// Adapter-declared entity kind → our DeviceType. An on/off light and a relay both
// expose only `on_off`, so caps can't tell them apart — the adapter's device_type
// can (ESPHome maps the `light` platform → "light"). Used for per-entity icons.
const ETYPE_ICON: Record<string, DeviceType> = {
  light: "light", switch: "switch", cover: "cover", lock: "lock",
  sensor: "sensor", binary_sensor: "sensor", button: "button", media_player: "media",
  // User overrides (Settings → Adapters) store a DeviceType string directly;
  // self-map the ones the adapter vocabulary above doesn't already cover so the
  // override round-trips through entityType().
  media: "media", remote: "remote", presence: "presence", mower: "mower", other: "other",
};

/** Icon/type for a SINGLE entity, preferring the adapter-declared device_type,
 *  falling back to the capability-derived type when it's absent/unmapped. */
export function entityType(deviceTypeRaw: string | null, caps: Record<string, unknown>): DeviceType {
  if (deviceTypeRaw && deviceTypeRaw in ETYPE_ICON) return ETYPE_ICON[deviceTypeRaw];
  return deviceType(caps);
}

/** Translated device-type label, e.g. "Svjetla" / "Lights". */
export function typeLabel(type: string): string {
  return t(`devtype.${type}` as MessageKey);
}

// Adapter source labels — proper nouns, not translated.
const ADAPTER_LABELS: Record<string, string> = {
  esphome: "ESPHome",
  mqtt: "MQTT",
  ha: "Home Assistant",
  shelly: "Shelly",
  tuya: "Tuya",
  dlna: "DLNA",
  heos: "HEOS",
  volumio: "Volumio",
  denon: "Denon/Marantz",
  announce: "Announce",
  androidtv: "Android TV",
  samsungtv: "Samsung TV",
  smartthings: "SmartThings",
  homekit: "HomeKit",
  govee: "Govee",
  broadlink: "Broadlink",
  unifi: "UniFi",
};

export function adapterLabel(adapter: string): string {
  return ADAPTER_LABELS[adapter] ?? adapter;
}

/** The scalar kind a capability's value takes — drives the right form input. */
export function valueKind(capability: string): "bool" | "number" | "string" {
  const m = capMeta(capability);
  if (m.control === "toggle" || m.control === "binary") return "bool";
  if (m.control === "event") return "string";
  if (capability === "location" || capability === "text") return "string";
  return "number";
}

// Seconds → a compact human duration ("30 s", "45 min", "1 h 20 min") — a washer's
// time-left or a device's uptime reads naturally, never a raw "2700".
export function formatDuration(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 24) return rm ? `${h} h ${rm} min` : `${h} h`;
  // Past a day, hours-only stops meaning anything ("234 h") — say days, and keep
  // just the leftover hours; minutes are noise at that scale.
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d} d ${rh} h` : `${d} d`;
}

// Humidity reads as a whole percent everywhere (0.1 %RH is noise).
/** A device's one glanceable value — what its floor-plan marker and its folded row
 *  on the Devices page both show: position, setpoint, temperature, humidity, light
 *  level, power, in that order of preference. */
export function glance(caps: Record<string, { value: unknown } | undefined>): string | null {
  const num = (c: string): number | null => (typeof caps[c]?.value === "number" ? (caps[c]!.value as number) : null);
  const one = (v: number) => formatNumber(v, { maximumFractionDigits: 1 });
  if ("open_close" in caps) { const o = num("open_close"); return o != null ? `${one(o)}%` : null; }
  if ("target_temperature" in caps) { const t = num("target_temperature"); return t != null ? `${one(t)}°` : null; }
  const t = num("temperature"); if (t != null) return `${one(t)}°`;
  const h = num("humidity"); if (h != null) return `${formatValue("humidity", h)}%`;
  const lx = num("illuminance"); if (lx != null) return `${one(lx)} lx`;
  const p = num("power"); if (p != null) return `${one(p)}W`;
  return null;
}

export function formatValue(cap: string, v: number): string {
  if (cap === "duration" || cap === "remaining") return formatDuration(v);
  return formatNumber(cap === "humidity" ? Math.round(v) : v, { maximumFractionDigits: 1 });
}

// Comfort colour: a reading shaded against its own good→bad band (ideal green,
// drifting through yellow/amber to red; cool/dark measurements to blue/muted).
// Bright 300/400 tones — reads on a dark plan label.
export function stateColor(cap: string, v: number): string {
  switch (cap) {
    case "temperature":
      return v < 16 ? "text-sky-300" : v < 19 ? "text-cyan-300" : v <= 25 ? "text-emerald-300" : v <= 27 ? "text-amber-300" : "text-rose-400";
    case "humidity":
      return v < 30 ? "text-amber-300" : v < 40 ? "text-yellow-300" : v <= 60 ? "text-emerald-300" : v <= 70 ? "text-amber-300" : "text-sky-300";
    case "pm25":
      return v <= 12 ? "text-emerald-300" : v <= 35 ? "text-yellow-300" : v <= 55 ? "text-amber-300" : "text-rose-400";
    case "voc_index":
      return v <= 150 ? "text-emerald-300" : v <= 250 ? "text-yellow-300" : v <= 400 ? "text-amber-300" : "text-rose-400";
    case "uv_index":
      return v < 3 ? "text-emerald-300" : v < 6 ? "text-yellow-300" : v < 8 ? "text-amber-300" : "text-rose-400";
    case "wind_speed":
    case "wind_gust":
      return v < 5 ? "text-emerald-300" : v < 10 ? "text-yellow-300" : v < 14 ? "text-amber-300" : "text-rose-400";
    case "rain_rate":
      return v < 0.1 ? "text-white/60" : v < 2.5 ? "text-sky-300" : v < 10 ? "text-cyan-300" : "text-blue-400";
    case "pressure":
      return v < 1000 ? "text-amber-300" : v <= 1025 ? "text-emerald-300" : "text-sky-300";
    case "illuminance":
      return v < 10 ? "text-white/50" : v < 500 ? "text-sky-300" : v < 5000 ? "text-amber-300" : "text-yellow-300";
    case "solar_radiation":
      return v < 20 ? "text-white/50" : v < 300 ? "text-sky-300" : v < 600 ? "text-amber-300" : "text-yellow-300";
    default:
      return "text-white";
  }
}
