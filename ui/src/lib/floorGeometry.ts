// Small 2D helpers for the floor plan. All coordinates are in % of the plan
// container (0–100), matching how markers and polygons are stored.
import type { FloorPoly } from "$lib/api";

// One light's contribution to the floor-plan glow overlay (see FloorGlow.svelte).
export interface GlowSpec {
  id: string;
  color: string;
  op: number;
  kind: "radius" | "poly" | "beams";
  x?: number;
  y?: number;
  r?: number;
  points?: string;
  rays?: { x2: number; y2: number; color: string }[]; // beams: coloured line endpoints from (x,y)
}

/** Ray-casting point-in-polygon. */
export function pointInPolygon(x: number, y: number, poly: FloorPoly): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Area-weighted centroid (for placing a room's hover label). */
export function polygonCentroid(poly: FloorPoly): { x: number; y: number } {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    const cross = xj * yi - xi * yj;
    a += cross;
    cx += (xi + xj) * cross;
    cy += (yi + yj) * cross;
  }
  if (a === 0) {
    // Degenerate → simple average.
    const n = poly.length || 1;
    return { x: poly.reduce((s, p) => s + p[0], 0) / n, y: poly.reduce((s, p) => s + p[1], 0) / n };
  }
  a *= 0.5;
  return { x: cx / (6 * a), y: cy / (6 * a) };
}

/** SVG `points` string from a polygon. */
export function toPoints(poly: FloorPoly): string {
  return poly.map(([x, y]) => `${x},${y}`).join(" ");
}
