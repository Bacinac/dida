// Every picker in the app names a device through one projection. Eight hand-copied
// versions had drifted: half showed diagnostic entities among the controls, and the
// entry page typed an unmapped device as "other" where every other page read it
// from its capabilities.
import { describe, expect, it, vi } from "vitest";

vi.mock("$lib/api", () => ({ api: {}, stateWs: vi.fn(), Unauthorized: class Unauthorized extends Error {} }));
vi.mock("$lib/auth.svelte", () => ({ auth: { user: null } }));
vi.mock("$lib/translations.svelte", () => ({ tr: (s: string) => s, translations: { load: vi.fn() } }));

const { devices } = await import("$lib/store.svelte");
const { pickerItem } = await import("$lib/EntityPicker.svelte");

function device(patch: Record<string, unknown>) {
  return {
    entityId: "esphome:node:wifi", name: "Connected SSID", adapter: "esphome", areaId: null,
    deviceType: null, diagnostic: false, category: "control", caps: { on_off: { value: true, unit: null, updatedAt: 1 } },
    ...patch,
  } as never;
}

describe("a device in a picker", () => {
  it("a diagnostic entity sits behind show-all even when its category says control", () => {
    expect(pickerItem(device({ diagnostic: true })).category).toBe("diagnostic");
    expect(pickerItem(device({})).category).toBe("control");
  });

  it("an unmapped device is typed from its capabilities, not as other", () => {
    expect(pickerItem(device({})).type).not.toBe("other");
  });

  it("carries the room and the adapter's proper name", () => {
    devices.areas = [{ id: 3, name: "Kitchen" }] as never;
    const item = pickerItem(device({ areaId: 3 }));
    expect([item.id, item.area, item.hint]).toEqual(["esphome:node:wifi", devices.areaName(3), "ESPHome"]);
  });
});
