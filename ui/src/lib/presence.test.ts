// Where the house thinks people are. The rule worth pinning is a distinction the
// UI cannot make on its own: "away" is a NETWORK VERDICT, not a place. The
// adapters overwrite the live location with it the moment a phone drops off wifi,
// so treating it as a location would replace "Seaside, 09:28" — a fact — with
// "Vani", which says nothing and is often wrong. Everything else here is a label
// nobody would notice was mistaken until they relied on it.
import { describe, expect, it } from "vitest";
import {
  LAST_ZONE_MAX_MS, PRESENCE_STALE_MS, lastKnownColor, locationLabel, persons,
  presenceStatus,
} from "$lib/presence";

const NOW = new Date("2026-08-08T14:00:00").getTime();

const cap = (v: unknown, updatedAt = NOW) => ({ value: v, unit: null, updatedAt });
const person = (over: Partial<Record<string, unknown>> = {}) =>
  ({ entityId: "presence:marko", name: "Marko", location: "Seaside", lat: null, lon: null,
     battery: null, updatedAt: NOW, stale: false, ...over }) as never;

function dev(entityId: string, name: string, caps: Record<string, unknown>, at = NOW) {
  return { entityId, name, hiddenCaps: [], lastSeen: at,
           caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, cap(v, at)])) } as never;
}

describe("who counts as a person", () => {
  it("only entities that report a location", () => {
    const list = persons([
      dev("presence:marko", "Marko", { location: "Seaside" }),
      dev("mqtt:lamp", "Lampa", { on_off: true }),
    ], NOW);
    expect(list.map((p) => p.entityId)).toEqual(["presence:marko"]);
  });

  it("sorted by name, so the list does not reshuffle between renders", () => {
    const list = persons([
      dev("presence:z", "Zoran", { location: "Kuća" }),
      dev("presence:a", "Ana", { location: "Kuća" }),
    ], NOW);
    expect(list.map((p) => p.name)).toEqual(["Ana", "Zoran"]);
  });

  it("a fix older than a day is STALE", () => {
    // A phone that stopped reporting yesterday is not evidence of where anyone is
    // now — and the map would otherwise show them standing there.
    const [p] = persons([dev("presence:marko", "Marko", { location: "Seaside" },
                             NOW - PRESENCE_STALE_MS - 1)], NOW);
    expect(p.stale).toBe(true);
  });

  it("a fresh fix is not", () => {
    const [p] = persons([dev("presence:marko", "Marko", { location: "Seaside" },
                             NOW - PRESENCE_STALE_MS + 60_000)], NOW);
    expect(p.stale).toBe(false);
  });
});

describe("away is a verdict, not a place", () => {
  it("a live zone is shown as itself", () => {
    const s = presenceStatus(person({ location: "Seaside" }), undefined, NOW);
    expect(s).toMatchObject({ place: "Seaside", tone: "present", at: "" });
  });

  it("with a known last zone, `away` NEVER stands in for it", () => {
    // The whole point. The network says "away"; history says Seaside at 09:28. The
    // second is a fact, the first is the absence of one.
    const s = presenceStatus(person({ location: "away" }),
                             { zone: "Seaside", ts: new Date("2026-08-08T09:28:00").getTime() }, NOW);
    expect(s.tone).toBe("lastKnown");
    expect(s.place).toBe("Seaside");
    expect(s.at).toBe("09:28");
  });

  it("a STALE fix falls back to the last known zone too", () => {
    // Stale means the value is old, not that the person went home.
    const s = presenceStatus(person({ location: "Seaside", stale: true }),
                             { zone: "Kuća", ts: NOW - 3_600_000 }, NOW);
    expect(s.tone).toBe("lastKnown");
    expect(s.place).toBe("Kuća");
  });

  it("with NO zone on record it admits it does not know", () => {
    // Not "Vani": nobody reported them anywhere, which is not evidence they left.
    const s = presenceStatus(person({ location: "away" }), undefined, NOW);
    expect(s.tone).toBe("unknown");
    expect(s.at).toBe("");
  });

  it("an empty location is not a zone either", () => {
    expect(presenceStatus(person({ location: "" }), undefined, NOW).tone).toBe("unknown");
  });

  it("a zone too old to mean anything is dropped, not shown", () => {
    // A phone that stopped reporting weeks ago left a real zone behind; rendering
    // it keeps a dead tracker looking alive. This is the case that put a person
    // at a house they had not been to in eight weeks.
    const s = presenceStatus(person({ location: "away" }),
                             { zone: "Seaside", ts: NOW - LAST_ZONE_MAX_MS - 60_000 }, NOW);
    expect(s.tone).toBe("unknown");
    expect(s.place).not.toBe("Seaside");
  });

  it("but the network catching them at home still wins", () => {
    // An association is proof they are on the house AP — the one thing a phone
    // with no GPS can still say truthfully.
    const s = presenceStatus(person({ location: "Home" }),
                             { zone: "Seaside", ts: NOW - LAST_ZONE_MAX_MS - 60_000 }, NOW);
    expect(s).toMatchObject({ place: "Home", tone: "present" });
  });
});

describe("when they were last seen there", () => {
  it("earlier today is just the clock time", () => {
    const s = presenceStatus(person({ location: "away" }),
                             { zone: "Kuća", ts: new Date("2026-08-08T09:05:00").getTime() }, NOW);
    expect(s.at).toBe("09:05");
  });

  it("an earlier day carries the date, so 20:00 is not read as tonight", () => {
    const s = presenceStatus(person({ location: "away" }),
                             { zone: "Kuća", ts: new Date("2026-08-07T20:00:00").getTime() }, NOW);
    expect(s.at).toBe("07. 08. 20:00");
  });
});

describe("the age gradient", () => {
  it("a fresh fix is gold-amber, an old one red and washed out", () => {
    const fresh = lastKnownColor(0);
    const old = lastKnownColor(24 * 3_600_000);
    const hue = (c: string) => Number(c.match(/hsl\((\d+)/)![1]);
    const sat = (c: string) => Number(c.match(/ (\d+)%/)![1]);
    expect(hue(fresh)).toBeGreaterThan(hue(old));
    expect(sat(fresh)).toBeGreaterThan(sat(old));
  });

  it("most of the travel happens in the first hours", () => {
    // Cube-root, deliberately: an hour's difference has to be obvious at a glance,
    // and a linear ramp made the first three hours indistinguishable.
    const hue = (ms: number) => Number(lastKnownColor(ms).match(/hsl\((\d+)/)![1]);
    const firstHour = hue(0) - hue(3_600_000);
    const twelfthHour = hue(11 * 3_600_000) - hue(12 * 3_600_000);
    expect(firstHour).toBeGreaterThan(twelfthHour);
  });

  it("a negative age is treated as fresh rather than producing a broken colour", () => {
    // Clock skew between the phone and the house is ordinary.
    expect(lastKnownColor(-5000)).toBe(lastKnownColor(0));
  });
});

describe("the away label", () => {
  it("a real zone name passes through untranslated", () => {
    expect(locationLabel("Seaside")).toBe("Seaside");
  });

  it("only the literal `away` becomes the translated word", () => {
    expect(locationLabel("away")).not.toBe("away");
  });
});
