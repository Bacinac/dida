// Heating projection. The controller decides everything and publishes what it
// decided as `heating:*` entities; this module only reads those from the live
// store and wraps the config calls. Deliberately no control logic here — a second
// implementation in the browser would drift from the one that drives the boiler,
// and the page would show a house that doesn't exist.

import { api, type HeatingRoom, type RoomHeatingConfig } from "$lib/api";
import type { Device } from "$lib/store.svelte";

export const PROFILES = ["comfort", "eco", "night", "away"] as const;
export type Profile = (typeof PROFILES)[number];
// "off" is a schedule STEP, not a setpoint: heating stands down and every room
// holds frost protection. Distinct from the master switch, which stops the
// controller writing at all.
export const SCHEDULE_PROFILES = [...PROFILES, "off"] as const;
export const MODES = ["auto", ...SCHEDULE_PROFILES] as const;

// Setpoints a fresh room starts from — the same ones the controller falls back to.
export const DEFAULT_TARGETS: Record<Profile, number> = {
  comfort: 21, eco: 19, night: 18, away: 15,
};

export const TARGET_MIN = 5;
export const TARGET_MAX = 30;
export const MAX_OFFSET = 5;
export const STEP = 0.5;

// How long a ± tap holds the room off its schedule.
export const BOOST_MINUTES = 60;

// The room's live view, as published by the controller.
export interface RoomLive {
  status: string;          // heating | idle | window | summer | stale | no_sensor | valve_error | off
  demand: boolean;
  temperature: number | null;
  target: number | null;
}

const num = (d: Device | undefined, cap: string): number | null => {
  const v = d?.caps[cap]?.value;
  return typeof v === "number" ? v : null;
};

export function roomLive(byId: Record<string, Device>, areaId: number): RoomLive {
  const d = byId[`heating:room:${areaId}`];
  const status = d?.caps["text"]?.value;
  return {
    status: typeof status === "string" ? status : "",
    demand: d?.caps["binary"]?.value === true,
    temperature: num(d, "temperature"),
    target: num(d, "target_temperature"),
  };
}

export function boilerRunning(byId: Record<string, Device>): boolean {
  return byId["heating:system"]?.caps["binary"]?.value === true;
}

export function roomsCalling(byId: Record<string, Device>): number {
  return num(byId["heating:system"], "number") ?? 0;
}

// Which entities are a room's thermometers and which are its valves is decided
// server-side (dida_core.heating.classify_entities) from what the room already
// contains, and arrives on each room. There is deliberately no client-side
// candidate list: a second classifier here would be a second answer to the same
// question, and the page would offer devices the controller never uses.

// One valve's live health, read off the sub-entities the MQTT adapter exposes.
export interface ValveLive {
  setpoint: number | null;
  position: number | null;   // % open; null when the valve doesn't report it
  batteryLow: boolean;
  windowOpen: boolean;
  reporting: boolean;        // does it echo a setpoint at all?
}

export function valveLive(byId: Record<string, Device>, valve: string): ValveLive {
  const head = byId[valve];
  return {
    setpoint: num(head, "target_temperature"),
    position: num(head, "open_close"),
    batteryLow: byId[`${valve}:battery_low`]?.caps["binary"]?.value === true,
    windowOpen: byId[`${valve}:window_open`]?.caps["binary"]?.value === true,
    reporting: head ? "target_temperature" in head.caps : false,
  };
}

export function emptyRoom(): RoomHeatingConfig {
  return {
    enabled: true,
    sensor: "",
    valves: [],
    offset: 0,
    schedule: [],
    window_pause: true,
    can_call_boiler: true,
    override_target: null,
    override_until: null,
  };
}

// What a room will actually run at, house setpoint plus its own trim — shown next
// to the offset so a number in °C never has to be worked out in the head.
export const effectiveTarget = (house: Record<string, number>, profile: Profile, offset: number): number =>
  Math.min(TARGET_MAX, Math.max(TARGET_MIN, (house[profile] ?? DEFAULT_TARGETS[profile]) + offset));

export const hasOverride = (r: HeatingRoom, now: number): boolean =>
  r.config.override_target !== null && (r.config.override_until ?? 0) * 1000 > now;

export const boost = (areaId: number, target: number, minutes = BOOST_MINUTES) =>
  api.boostHeatingRoom(areaId, target, minutes);
export const clearBoost = (areaId: number) => api.clearHeatingBoost(areaId);
