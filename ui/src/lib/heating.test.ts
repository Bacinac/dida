// The heating page's arithmetic and its reads off the live entities. Every one of
// these is a number the house runs at, and every one of them fails quietly: a
// clamp that lets 34 °C through, an expired boost that never expires, a valve read
// off the wrong entity. Nothing throws — the room is simply the wrong temperature,
// and the only way to notice is to be cold.
import { describe, expect, it } from "vitest";
import {
  DEFAULT_TARGETS, MAX_OFFSET, TARGET_MAX, TARGET_MIN,
  boilerRunning, effectiveTarget, hasOverride, roomLive, roomsCalling, valveLive,
} from "$lib/heating";

const cap = (v: unknown) => ({ value: v, unit: null, updatedAt: 1 });
const dev = (entityId: string, caps: Record<string, unknown>) =>
  ({ entityId, name: entityId, hiddenCaps: [],
     caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, cap(v)])) }) as never;
const byId = (...ds: { entityId: string }[]) =>
  Object.fromEntries(ds.map((d) => [d.entityId, d])) as never;

const room = (over: Partial<{ override_target: number | null; override_until: number | null }>) =>
  ({ area_id: 1, name: "Soba", kind: null, sensors: [], valves: [],
     config: { enabled: true, sensor: "", valves: [], offset: 0, schedule: [],
               window_pause: true, can_call_boiler: true,
               override_target: null, override_until: null, ...over } }) as never;

describe("what a room will actually run at", () => {
  it("is the house setpoint plus the room's own trim", () => {
    expect(effectiveTarget({ comfort: 21 }, "comfort", 1.5)).toBe(22.5);
    expect(effectiveTarget({ comfort: 21 }, "comfort", -2)).toBe(19);
  });

  it("falls back to the built-in target when the house has no value for a profile", () => {
    // A profile the user has never set must not resolve to NaN and then to a valve
    // command nobody can read.
    expect(effectiveTarget({}, "eco", 0)).toBe(DEFAULT_TARGETS.eco);
  });

  it("clamps at both ends", () => {
    // The offset is capped at ±5 in the UI, but the house setpoint is not — and a
    // valve asked for 34 °C either refuses or runs flat out until someone notices.
    expect(effectiveTarget({ comfort: 29 }, "comfort", MAX_OFFSET)).toBe(TARGET_MAX);
    expect(effectiveTarget({ night: 6 }, "night", -MAX_OFFSET)).toBe(TARGET_MIN);
  });
});

describe("a boost that has run out is not a boost", () => {
  const now = 1_700_000_000_000;

  it("counts while it is still running", () => {
    expect(hasOverride(room({ override_target: 23, override_until: now / 1000 + 600 }), now)).toBe(true);
  });

  it("does NOT count once the deadline has passed", () => {
    // Otherwise the room stays boosted forever and the schedule never takes it back.
    expect(hasOverride(room({ override_target: 23, override_until: now / 1000 - 1 }), now)).toBe(false);
  });

  it("does not count when there is no target, whatever the deadline says", () => {
    expect(hasOverride(room({ override_target: null, override_until: now / 1000 + 600 }), now)).toBe(false);
  });

  it("treats a missing deadline as expired rather than as forever", () => {
    expect(hasOverride(room({ override_target: 23, override_until: null }), now)).toBe(false);
  });
});

describe("reading the live entities", () => {
  it("a room reads its own controller entity, not another room's", () => {
    const live = roomLive(byId(
      dev("heating:room:1", { text: "heating", binary: true, temperature: 19.5, target_temperature: 21 }),
      dev("heating:room:2", { text: "idle", binary: false, temperature: 24, target_temperature: 18 }),
    ), 1);
    expect(live).toEqual({ status: "heating", demand: true, temperature: 19.5, target: 21 });
  });

  it("a room with no controller yet is blank, not zero", () => {
    // Zero is a temperature. Blank is "we have not heard".
    expect(roomLive(byId(), 9)).toEqual({ status: "", demand: false, temperature: null, target: null });
  });

  it("the boiler is running only when it says so", () => {
    expect(boilerRunning(byId(dev("heating:system", { binary: true })))).toBe(true);
    expect(boilerRunning(byId(dev("heating:system", { binary: false })))).toBe(false);
    expect(boilerRunning(byId())).toBe(false);
  });

  it("rooms calling for heat is a count, and absent means none", () => {
    expect(roomsCalling(byId(dev("heating:system", { number: 3 })))).toBe(3);
    expect(roomsCalling(byId())).toBe(0);
  });
});

describe("a valve's health", () => {
  it("is read from the valve's own sub-entities", () => {
    const live = valveLive(byId(
      dev("mqtt:trv_bedroom", { target_temperature: 21, open_close: 45 }),
      dev("mqtt:trv_bedroom:battery_low", { binary: true }),
      dev("mqtt:trv_bedroom:window_open", { binary: false }),
    ), "mqtt:trv_bedroom");
    expect(live).toEqual({ setpoint: 21, position: 45, batteryLow: true, windowOpen: false, reporting: true });
  });

  it("a valve that echoes no setpoint is NOT reporting", () => {
    // The distinction that matters: a TRV present on the network but deaf to
    // commands looks identical to a working one until you ask this.
    const live = valveLive(byId(dev("mqtt:trv_hall", { open_close: 0 })), "mqtt:trv_hall");
    expect(live.reporting).toBe(false);
    expect(live.setpoint).toBeNull();
  });

  it("a valve that is not there at all is not reporting either", () => {
    expect(valveLive(byId(), "mqtt:gone").reporting).toBe(false);
  });

  it("a missing sub-entity is not read as a fault", () => {
    // Most TRVs publish no window sensor; treating absence as "window open" would
    // pause the room's heat permanently.
    const live = valveLive(byId(dev("mqtt:trv_x", { target_temperature: 20 })), "mqtt:trv_x");
    expect(live.batteryLow).toBe(false);
    expect(live.windowOpen).toBe(false);
  });
});
