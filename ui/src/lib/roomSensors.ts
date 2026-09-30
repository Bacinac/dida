// Room sensor aggregation: a room's readings are shown as ONE label — per capability,
// the arithmetic mean across the room's sensors (a two-thermometer living room reads
// one temperature). Pure, live-derivable from the store; the config decides which caps
// show and which sensor readings are excluded from a mean.

import type { SensorConfig } from "$lib/api";
import type { Device } from "$lib/store.svelte";

// Default order for a ROOM label: comfort first (temperature, humidity), then air
// quality, then weather — illuminance last (a light level, not a comfort headline).
// The user overrides per room via drag & drop (config.order).
const ROOM_ORDER = [
  "temperature", "humidity", "pm25", "voc_index",
  "pressure", "wind_speed", "solar_radiation", "uv_index", "rain_rate", "illuminance",
];

export interface RoomReading {
  entityId: string;
  name: string;       // the sensor's display name (which thermometer)
  value: number;
  included: boolean;  // effectively in the mean (type default ± user override)
  secondary: boolean; // a thermostat (AC/TRV), not a pure ambient sensor → default off
}
export interface RoomCap {
  cap: string;
  hidden: boolean;         // hidden from the label (still listed in edit)
  readings: RoomReading[]; // every eligible sensor reporting this cap
  mean: number | null;     // mean of the INCLUDED readings (null → none)
}

// Caps that mark a device as NOT a pure ambient sensor — an actuator or a thermostat
// (target_temperature = a setpoint). Such a device's temperature is device-adjacent
// (an AC probe is fine, a TRV by a radiator is biased, a relay's internal is junk),
// so it defaults OUT of the room mean; the user opts the good ones in.
const CONTROL_CAPS = new Set([
  "on_off", "open_close", "lock", "hvac_mode", "fan_speed", "media_transport", "target_temperature",
]);
export const isAmbientSensor = (d: Device): boolean => !Object.keys(d.caps).some((c) => CONTROL_CAPS.has(c));
// Offered in a room label: a pure ambient sensor, or a simple TRV (a setpoint but no
// full HVAC control). A full climate unit (AC / heat pump — has hvac_mode) is a CONTROL,
// shown as a glowing marker, not a room temperature source. A plain actuator with an
// incidental internal temperature (a relay's 54 °C, no setpoint) is never offered either.
export const roomEligible = (d: Device): boolean =>
  hasRoomReadings(d) && (isAmbientSensor(d) || ("target_temperature" in d.caps && !("hvac_mode" in d.caps)));

// A room label shows AMBIENT readings only — the room's own environmental state.
// Allowlist, deliberately: anything NOT here (electrical power/energy/voltage/current/…
// incl. any future apparent/reactive power, plus battery, uptime, the astro sun angle)
// is a DEVICE or GLOBAL metric, not a room property, so it is never offered. A room's
// "power" would be a misleading partial sum of whatever happens to be metered.
const AMBIENT_CAPS = new Set([
  "temperature", "humidity", "illuminance", "pm25", "voc_index",
  "pressure", "wind_speed", "solar_radiation", "uv_index", "rain_rate",
]);

// A sensor's room-eligible readings: an ambient cap, not hidden by the user, numeric.
export function numericCaps(d: Device): string[] {
  return Object.keys(d.caps).filter(
    (c) => AMBIENT_CAPS.has(c) && !d.hiddenCaps.includes(c) && typeof d.caps[c].value === "number",
  );
}

// True if a device contributes ANY reading to a room label.
export const hasRoomReadings = (d: Device): boolean => numericCaps(d).length > 0;

export function roomAggregate(sensors: Device[], config: SensorConfig | null): RoomCap[] {
  const hidden = new Set(config?.hidden ?? []);
  const excluded = new Set(config?.excluded ?? []);
  const included = new Set(config?.included ?? []);
  const byCap = new Map<string, { entityId: string; name: string; value: number; pure: boolean }[]>();
  for (const d of sensors) {
    const pure = isAmbientSensor(d);
    for (const cap of numericCaps(d)) {
      const arr = byCap.get(cap) ?? [];
      arr.push({ entityId: d.entityId, name: d.name, value: d.caps[cap].value as number, pure });
      byCap.set(cap, arr);
    }
  }
  const out: RoomCap[] = [];
  for (const [cap, raw] of byCap) {
    const hasPure = raw.some((r) => r.pure);
    const readings: RoomReading[] = raw.map((r) => {
      const key = `${r.entityId}:${cap}`;
      // Pure sensors are in by default; a thermostat is in only when it's the SOLE
      // source (no pure sensor for this cap). The user overrides either way.
      const dflt = r.pure || !hasPure;
      const inc = dflt ? !excluded.has(key) : included.has(key);
      return { entityId: r.entityId, name: r.name, value: r.value, included: inc, secondary: !r.pure };
    });
    const on = readings.filter((r) => r.included);
    const mean = on.length ? on.reduce((s, r) => s + r.value, 0) / on.length : null;
    out.push({ cap, hidden: hidden.has(cap), readings, mean });
  }
  // Custom drag-&-drop order first (config.order), then the comfort-first default.
  const order = config?.order ?? [];
  const defRank = (cap: string): number => {
    const i = ROOM_ORDER.indexOf(cap);
    return i >= 0 ? i : ROOM_ORDER.length;
  };
  const rank = (cap: string): number => {
    const i = order.indexOf(cap);
    return i >= 0 ? i : order.length + defRank(cap);
  };
  return out.sort((a, b) => rank(a.cap) - rank(b.cap));
}

// The effective default inclusion of a reading (before the user's override) — the
// toggle needs it to store the MINIMAL override (drop it when the choice equals default).
export function readingDefaultIn(rc: RoomCap, entityId: string): boolean {
  const r = rc.readings.find((x) => x.entityId === entityId);
  if (!r) return false;
  return !r.secondary || !rc.readings.some((x) => !x.secondary); // pure, or the sole source
}

// The collapsed value = the FIRST value in the (custom or default) order that is shown
// and has a mean. So dragging a value to the top makes it the headline on the plan.
// (caps arrive already sorted by roomAggregate.)
export function primaryCap(caps: RoomCap[]): RoomCap | null {
  return caps.find((c) => !c.hidden && c.mean != null) ?? null;
}
