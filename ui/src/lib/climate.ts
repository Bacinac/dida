// Climate projection over the canonical capability group. An AC is one device
// whose caps include `hvac_mode`; this module reads the scalar climate caps and
// wraps the command calls. DeviceCard recomposes them into a single climate
// card — the core stays generic (same pattern as media.ts).

import { api } from "$lib/api";
import type { Device } from "$lib/store.svelte";

// Caps rendered by the ClimateCard instead of the generic capability list.
export const CLIMATE_CAPS = new Set([
  "hvac_mode", "target_temperature", "fan_mode", "temperature",
]);

export function isClimate(d: Device): boolean {
  return "hvac_mode" in d.caps;
}

// Standard mode/fan vocabularies. A device may not support every one; an
// unsupported value just no-ops at the device and state reflects the truth.
export const HVAC_MODES = ["off", "cool", "heat", "auto", "dry", "fan_only"] as const;
export const FAN_MODES = ["auto", "low", "medium", "high", "silent", "max"] as const;

function str(d: Device, cap: string): string | null {
  const v = d.caps[cap]?.value;
  return typeof v === "string" && v.trim() ? v : null;
}
function num(d: Device, cap: string): number | null {
  const v = d.caps[cap]?.value;
  return typeof v === "number" ? v : null;
}

export const hvacMode = (d: Device): string => str(d, "hvac_mode") ?? "off";
export const targetTemp = (d: Device): number | null => num(d, "target_temperature");
export const currentTemp = (d: Device): number | null => num(d, "temperature");
export const fanMode = (d: Device): string => str(d, "fan_mode") ?? "auto";
export const isOff = (d: Device): boolean => hvacMode(d) === "off";

// Sensible AC setpoint bounds for the stepper (the capability allows 4..35).
export const TEMP_MIN = 16;
export const TEMP_MAX = 30;

export const setHvacMode = (id: string, value: string) =>
  api.sendCommand({ entity_id: id, capability: "hvac_mode", command: "set_hvac_mode", args: { value } });
export const setTargetTemp = (id: string, value: number) =>
  api.sendCommand({ entity_id: id, capability: "target_temperature", command: "set_temperature", args: { value } });
export const setFanMode = (id: string, value: string) =>
  api.sendCommand({ entity_id: id, capability: "fan_mode", command: "set_fan_mode", args: { value } });
