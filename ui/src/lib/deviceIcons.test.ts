// The icon set has one failure mode and it is silent: a device or a source asks for
// an icon that is not there and the tile renders an empty box. Nothing throws,
// nothing logs — the row just looks broken.
//
// The invariant is ONE-DIRECTIONAL, which took measuring to establish rather than
// assuming. Every entry the picker offers must have an SVG. The reverse is not
// required: `ICONS` is also indexed by the backend's canonical device type
// (`ICONS[type]` in DeviceIcon.svelte), so an icon can legitimately exist without
// being manually pickable — `button` is exactly that, a canonical device type the
// picker does not list.
import { describe, expect, it } from "vitest";
import { ICONS, ICON_LIST } from "$lib/deviceIcons";

describe("the icon set", () => {
  it("every icon the picker offers has an SVG", () => {
    // The direction that matters: a picked icon with no path renders blank, and the
    // user has no way to tell it apart from a rendering bug.
    const missing = ICON_LIST.filter((k) => !(k in ICONS));
    expect(missing).toEqual([]);
  });

  it("the picker offers no duplicates", () => {
    // A duplicate reads as two different choices that do the same thing.
    expect(new Set(ICON_LIST).size).toBe(ICON_LIST.length);
  });

  it("the fallbacks the components rely on exist", () => {
    // DeviceIcon does `ICONS[type] ?? ICONS.other`; MediaHub does `?? ICONS.media`.
    // Remove either and the fallback path renders `undefined`.
    expect(ICONS.other).toBeTruthy();
    expect(ICONS.media).toBeTruthy();
  });

  it("every icon is drawable markup, not an empty string", () => {
    const empty = Object.entries(ICONS).filter(([, v]) => !v || !v.trim().startsWith("<"));
    expect(empty.map(([k]) => k)).toEqual([]);
  });
});

describe("the icons chosen in code", () => {
  it("the media-source fallbacks exist", () => {
    // `sourceIcon` (mediaSources.ts) falls back to "tv" for an activity and
    // "player" for a player. Those two names live in code, so nothing else would
    // notice them going stale — but mediaSources itself cannot be imported here:
    // it reaches SvelteKit's `$app/environment` through the output store, which the
    // plain-node vitest run has no shim for. Asserting the icons it names is the
    // half that IS reachable, and it is the half that renders blank when wrong.
    expect(ICONS.tv).toBeTruthy();
    expect(ICONS.player).toBeTruthy();
  });
});
