// The rules that decide what a reading MEANS on screen. 475 lines with no test
// until now, and the presence pip in particular encodes hard-won knowledge that
// reads as arbitrary if you meet it cold: a camera's `motion` latches on any
// tracked object, and an ESPHome binary that missed its clear edge stays "true"
// forever. Both of those turn a person icon green in an empty house, which is
// exactly the kind of wrong that gets believed.
import { describe, expect, it } from "vitest";
import {
  CAP_META, capMeta, capRank, formatDuration, hasPresenceCap, parkedFor,
  parkedSince, parkedVehicle, parkedVehicleLane, presenceOccupied, sceneLit,
} from "$lib/capabilities";

const cap = (v: unknown) => ({ value: v, unit: null, updatedAt: 1 });

describe("presence pip — precedence person_count ▸ occupancy ▸ motion", () => {
  it("a camera that sees a person is occupied", () => {
    expect(presenceOccupied([{ person_count: cap(2), motion: cap(false) }])).toBe(true);
  });

  it("a camera's motion alone does NOT turn the person pip green", () => {
    // `motion` latches on ANY tracked object — a car, an animal, a shadow. This is
    // the rule that stops "a cat walked past" reading as "someone is home".
    expect(presenceOccupied([{ person_count: cap(0), motion: cap(true) }])).toBe(false);
  });

  it("a stale motion latch cannot override occupancy's clear", () => {
    // ESPHome emits only on CHANGE, so a missed clear edge leaves `motion` true
    // indefinitely. The debounced occupancy is the managed truth.
    expect(presenceOccupied([{ occupancy: cap(false), motion: cap(true) }])).toBe(false);
  });

  it("occupancy true is occupied even when motion has gone quiet", () => {
    expect(presenceOccupied([{ occupancy: cap(true), motion: cap(false) }])).toBe(true);
  });

  it("a bare PIR falls through to motion, because that is all there is", () => {
    expect(presenceOccupied([{ motion: cap(true) }])).toBe(true);
    expect(presenceOccupied([{ motion: cap(false) }])).toBe(false);
  });

  it("precedence holds ACROSS entities folded into one marker", () => {
    // A marker commonly folds a camera and an mmWave board. person_count wins.
    const camera = { person_count: cap(0) };
    const board = { occupancy: cap(true), motion: cap(true) };
    expect(presenceOccupied([camera, board])).toBe(false);
  });

  it("no presence capability at all is not occupied", () => {
    expect(presenceOccupied([{ temperature: cap(21) }])).toBe(false);
    expect(presenceOccupied([])).toBe(false);
  });

  it("hasPresenceCap decides whether a pip is drawn at all", () => {
    expect(hasPresenceCap([{ motion: cap(false) }])).toBe(true);
    expect(hasPresenceCap([{ occupancy: cap(false) }])).toBe(true);
    expect(hasPresenceCap([{ person_count: cap(0) }])).toBe(true);
    expect(hasPresenceCap([{ temperature: cap(21) }])).toBe(false);
  });
});

describe("parked vehicle", () => {
  it("an occupied place with NO plate yet is still occupied", () => {
    // BABA reports "occupied, unidentified" from the moment a car stops until the
    // plate is read. Treating that as null once made the PREVIOUS car's name show
    // over a spot a different car was standing in.
    const v = parkedVehicle('{"name":"","since":"2026-08-07T16:46:00Z"}');
    expect(v).not.toBeNull();
    expect(v!.name).toBe("");
  });

  it("a named vehicle carries its name and arrival", () => {
    const v = parkedVehicle('{"name":"Golf","since":"2026-08-07T16:46:00Z"}');
    expect(v).toEqual({ name: "Golf", since: "2026-08-07T16:46:00Z" });
  });

  it("an empty place is null, not an empty vehicle", () => {
    expect(parkedVehicle("")).toBeNull();
  });

  it("a malformed payload degrades to null rather than breaking the surface", () => {
    expect(parkedVehicle("{not json")).toBeNull();
    expect(parkedVehicle('{"since":"x"}')).toBeNull();   // no name → not a vehicle
    expect(parkedVehicle(42)).toBeNull();
    expect(parkedVehicle(null)).toBeNull();
  });

  it("an unparseable timestamp yields an empty string, never NaN on screen", () => {
    expect(parkedSince("not a date")).toBe("");
    expect(parkedFor("not a date", Date.now())).toBe("");
  });

  it("history lanes follow who stood there, not the descriptor with its timestamp", () => {
    // Each stay carries its own `since`, so lanes keyed on the raw JSON gave every
    // stay a lane of its own, labelled with the JSON itself.
    expect(parkedVehicleLane('{"name":"Ana","since":"2026-09-11T15:53:30Z"}'))
      .toBe(parkedVehicleLane('{"name":"Ana","since":"2026-09-12T16:47:41Z"}'));
    expect(parkedVehicleLane('{"name":"","since":"2026-09-12T16:47:41Z"}')).toBe("unidentified");
    expect(parkedVehicleLane("")).toBe("vacant");
  });

  it("duration is measured against the caller's clock and never goes negative", () => {
    const t0 = "2026-08-07T16:00:00Z";
    const now = new Date("2026-08-07T18:10:00Z").getTime();
    expect(parkedFor(t0, now)).toMatch(/\d/);
    // a clock that has not caught up must not render "-5 min"
    expect(parkedFor(t0, new Date("2026-08-07T15:00:00Z").getTime())).not.toMatch(/-/);
  });
});

describe("display ordering and fallbacks", () => {
  it("actuators sort before sensors, sensors before diagnostics", () => {
    expect(capRank("on_off")).toBeLessThan(capRank("temperature"));
    expect(capRank("temperature")).toBeLessThan(capRank("battery"));
  });

  it("an unranked capability sorts last instead of jumping to the front", () => {
    // Array.indexOf returns -1 for a miss; used raw it would sort FIRST.
    expect(capRank("something_new")).toBeGreaterThan(capRank("duration"));
  });

  it("an unknown capability renders as a sensor rather than breaking", () => {
    expect(capMeta("brand_new_thing")).toEqual({ control: "sensor" });
  });

  it("every CAP_META entry declares a control kind", () => {
    for (const [name, meta] of Object.entries(CAP_META)) {
      expect(meta.control, `${name} has no control kind`).toBeTruthy();
    }
  });

  it("sceneLit reads the states that mean 'in use', case- and space-insensitively", () => {
    for (const s of ["open", "present", "occupied", " OPEN ", "Present"]) {
      expect(sceneLit(s), s).toBe(true);
    }
    for (const s of ["closed", "absent", "free", ""]) {
      expect(sceneLit(s), s).toBe(false);
    }
  });
});

describe("formatDuration past a day", () => {
  // "234 h 27 min" reads as nothing; past 24 h the unit people think in is days.
  it("switches to days + leftover hours", () => {
    expect(formatDuration(234 * 3600 + 27 * 60)).toBe("9 d 18 h");
    expect(formatDuration(48 * 3600)).toBe("2 d");
    expect(formatDuration(25 * 3600)).toBe("1 d 1 h");
  });
  it("stays on hours/minutes below a day", () => {
    expect(formatDuration(23 * 3600 + 59 * 60)).toBe("23 h 59 min");
    expect(formatDuration(90)).toBe("2 min");
    expect(formatDuration(45)).toBe("45 s");
  });
});
