// The laser raycast is the single most expensive thing the floorplan does: 54 rays
// × ~99 radial steps × a point-in-polygon each, and buildGlows re-runs on EVERY
// light-capability change (20×/s during replay). The geometry it solves depends
// only on the marker position and the room outline — never on the live state — so
// it must be solved once and reused. These tests hold that line.
import { beforeEach, describe, expect, it, vi } from "vitest";

const pointInPolygon = vi.fn((x: number, y: number) => Math.hypot(x - 50, y - 50) < 40);
vi.mock("$lib/floorGeometry", () => ({
  pointInPolygon,
  polygonCentroid: () => ({ x: 0, y: 0 }),
  toPoints: () => "",
}));

const { buildGlows } = await import("$lib/floorplanItems");

function laserAt(x: number, y: number) {
  const dev = {
    entityId: "test:laser", caps: { on_off: { value: true, unit: null, updatedAt: 1 } },
    fpStyle: { effect: "laser", color: "#43f08e" }, fpGlow: null,
  };
  return { it: { key: "k1", type: "light", toggle: dev } as never, p: { x, y } };
}
const area = (poly: unknown) => ({ id: 1, kind: "living", fp_poly: poly, name: "Living" }) as never;

beforeEach(() => vi.clearAllMocks());

describe("laser ray memoisation", () => {
  it("solves the rays once and reuses them for an unchanged marker + outline", () => {
    const poly = [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }, { x: 0, y: 100 }];
    const areas = [area(poly)];
    const first = buildGlows([laserAt(50, 50)], areas, new Map(), "ground");
    const castCalls = pointInPolygon.mock.calls.length;
    expect(castCalls).toBeGreaterThan(500); // the real work happened

    pointInPolygon.mockClear();
    const second = buildGlows([laserAt(50, 50)], areas, new Map(), "ground");
    // The rays are geometry, not state: a state tick must not re-cast them.
    expect(pointInPolygon.mock.calls.length).toBeLessThan(castCalls / 10);
    expect(JSON.stringify(second)).toBe(JSON.stringify(first)); // and the result is identical
  });

  it("re-solves when the room outline is edited (a NEW polygon object)", () => {
    const areas1 = [area([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }])];
    buildGlows([laserAt(30, 30)], areas1, new Map(), "ground");
    pointInPolygon.mockClear();
    // The store hands out a new object when a polygon is edited — that must miss.
    const areas2 = [area([{ x: 0, y: 0 }, { x: 90, y: 0 }, { x: 90, y: 90 }])];
    buildGlows([laserAt(30, 30)], areas2, new Map(), "ground");
    expect(pointInPolygon.mock.calls.length).toBeGreaterThan(500);
  });

  it("re-solves when the marker moves", () => {
    const poly = [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }, { x: 0, y: 100 }];
    const areas = [area(poly)];
    buildGlows([laserAt(40, 40)], areas, new Map(), "ground");
    pointInPolygon.mockClear();
    buildGlows([laserAt(41, 40)], areas, new Map(), "ground");
    expect(pointInPolygon.mock.calls.length).toBeGreaterThan(500);
  });
});
