// What a room's label is allowed to say. The rules read as fussy until you see the
// readings they exclude: a TRV strapped to a radiator reporting 32 °C as "the
// bedroom", a relay's 54 °C internal, or a room "power" that is a partial sum of
// whatever happens to be metered. Each of those is a plausible-looking number that
// is simply not a property of the room.
import { describe, expect, it } from "vitest";
import {
  hasRoomReadings, isAmbientSensor, numericCaps, primaryCap,
  readingDefaultIn, roomAggregate, roomEligible,
} from "$lib/roomSensors";

const cap = (v: unknown) => ({ value: v, unit: null, updatedAt: 1 });
function dev(entityId: string, caps: Record<string, unknown>, hiddenCaps: string[] = []) {
  return {
    entityId, name: entityId, hiddenCaps,
    caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, cap(v)])),
  } as never;
}

describe("what counts as a room reading", () => {
  it("ambient caps are offered; device and global metrics never are", () => {
    const d = dev("s:1", { temperature: 21, humidity: 50, power: 900, battery: 80, energy: 12 });
    expect(numericCaps(d).sort()).toEqual(["humidity", "temperature"]);
  });

  it("a non-numeric reading is not a mean candidate", () => {
    expect(numericCaps(dev("s:2", { temperature: "warm" }))).toEqual([]);
  });

  it("a capability the user hid is dropped from the label", () => {
    expect(numericCaps(dev("s:3", { temperature: 21, humidity: 50 }, ["humidity"]))).toEqual(["temperature"]);
  });

  it("hasRoomReadings is false for a device with nothing ambient", () => {
    expect(hasRoomReadings(dev("plug:1", { on_off: true, power: 900 }))).toBe(false);
  });
});

describe("which devices may speak for a room", () => {
  it("a pure ambient sensor is eligible", () => {
    const s = dev("th:1", { temperature: 21, humidity: 50 });
    expect(isAmbientSensor(s)).toBe(true);
    expect(roomEligible(s)).toBe(true);
  });

  it("a simple TRV is eligible — a setpoint, but no full HVAC control", () => {
    const trv = dev("trv:1", { temperature: 22, target_temperature: 21 });
    expect(isAmbientSensor(trv)).toBe(false);
    expect(roomEligible(trv)).toBe(true);
  });

  it("a full climate unit is a CONTROL, not a room temperature source", () => {
    // An AC / heat pump has hvac_mode. It is a glowing marker, not the room's reading.
    const ac = dev("ac:1", { temperature: 24, target_temperature: 22, hvac_mode: "cool" });
    expect(roomEligible(ac)).toBe(false);
  });

  it("a plain actuator with an incidental internal temperature is never offered", () => {
    // A relay reporting its own 54 °C would otherwise become "the room".
    const relay = dev("relay:1", { temperature: 54, on_off: true });
    expect(roomEligible(relay)).toBe(false);
  });
});

describe("the mean, and which readings default into it", () => {
  it("two thermometers in one room read as ONE temperature", () => {
    const caps = roomAggregate([dev("a", { temperature: 20 }), dev("b", { temperature: 22 })], null);
    const t = caps.find((c) => c.cap === "temperature")!;
    expect(t.mean).toBe(21);
    expect(t.readings).toHaveLength(2);
  });

  it("a thermostat defaults OUT when a pure sensor is present", () => {
    // A TRV by a radiator is biased; the ambient sensor is the honest source.
    const caps = roomAggregate(
      [dev("th", { temperature: 20 }), dev("trv", { temperature: 30, target_temperature: 21 })], null);
    const t = caps.find((c) => c.cap === "temperature")!;
    expect(t.mean).toBe(20);
    expect(readingDefaultIn(t, "trv")).toBe(false);
    expect(readingDefaultIn(t, "th")).toBe(true);
  });

  it("…but a thermostat IS the reading when it is the room's only source", () => {
    // Better a biased number than no number: the alternative is a blank room.
    const caps = roomAggregate([dev("trv", { temperature: 30, target_temperature: 21 })], null);
    const t = caps.find((c) => c.cap === "temperature")!;
    expect(t.mean).toBe(30);
    expect(readingDefaultIn(t, "trv")).toBe(true);
  });

  it("the user can override a default-out reading back IN", () => {
    const caps = roomAggregate(
      [dev("th", { temperature: 20 }), dev("trv", { temperature: 30, target_temperature: 21 })],
      { included: ["trv:temperature"] });
    expect(caps.find((c) => c.cap === "temperature")!.mean).toBe(25);
  });

  it("…and override a default-in reading OUT", () => {
    const caps = roomAggregate(
      [dev("a", { temperature: 20 }), dev("b", { temperature: 30 })],
      { excluded: ["b:temperature"] });
    expect(caps.find((c) => c.cap === "temperature")!.mean).toBe(20);
  });

  it("excluding every reading leaves no mean rather than a zero", () => {
    const caps = roomAggregate([dev("a", { temperature: 20 })], { excluded: ["a:temperature"] });
    expect(caps.find((c) => c.cap === "temperature")!.mean).toBeNull();
  });
});

describe("the collapsed headline", () => {
  it("comfort leads by default — temperature before illuminance", () => {
    const caps = roomAggregate([dev("a", { illuminance: 300, temperature: 21 })], null);
    expect(caps.map((c) => c.cap)).toEqual(["temperature", "illuminance"]);
    expect(primaryCap(caps)!.cap).toBe("temperature");
  });

  it("dragging a value to the top makes it the headline", () => {
    const caps = roomAggregate([dev("a", { illuminance: 300, temperature: 21 })],
                               { order: ["illuminance"] });
    expect(primaryCap(caps)!.cap).toBe("illuminance");
  });

  it("a hidden capability is never the headline", () => {
    const caps = roomAggregate([dev("a", { temperature: 21, humidity: 50 })],
                               { hidden: ["temperature"] });
    expect(primaryCap(caps)!.cap).toBe("humidity");
  });

  it("a room with nothing to say has no headline instead of an empty pill", () => {
    expect(primaryCap([])).toBeNull();
  });
});
