// Behavioural tests for the live device store — the first UI tests in the repo.
//
// Target: the OPTIMISTIC-UPDATE paths. Every `set*` writes the new value into the
// store before the API call and must put the old one back if that call fails,
// otherwise the UI shows a change the server rejected — a lie that survives until
// the next reload. That revert had no coverage at all.
import { beforeEach, describe, expect, it, vi } from "vitest";

// The store imports the real HTTP client at module load; stub it so nothing here
// touches the network. Each test installs its own behaviour per method.
const api = {
  setEntityExposed: vi.fn(),
  setEntityType: vi.fn(),
  renameEntity: vi.fn(),
  setEntityGlow: vi.fn(),
  listEntities: vi.fn(),
  listState: vi.fn(),
  listAreas: vi.fn(),
  listDevices: vi.fn(),
};
vi.mock("$lib/api", () => ({
  api,
  stateWs: vi.fn(),
  Unauthorized: class Unauthorized extends Error {},
}));
vi.mock("$lib/auth.svelte", () => ({ auth: { user: null } }));
vi.mock("$lib/translations.svelte", () => ({ tr: (s: string) => s, translations: { load: vi.fn() } }));

const { devices, prettify } = await import("$lib/store.svelte");

function seed(entityId: string, patch: Record<string, unknown> = {}) {
  devices.byId[entityId] = {
    entityId, rawName: entityId, name: entityId, label: null, adapter: "test",
    deviceKey: null, hasOwnName: true, areaId: null, caps: {}, exposed: true,
    voiceExposed: null, voiceEffective: false, deviceType: "sensor",
    diagnostic: false, category: "control", fpGlow: null, fpStyle: null,
    ...patch,
  } as never;
  return devices.byId[entityId];
}

beforeEach(() => {
  vi.clearAllMocks(); // these are hand-rolled vi.fn()s, not spies — restoreMocks doesn't reset their history
  for (const k of Object.keys(devices.byId)) delete devices.byId[k];
});

describe("optimistic updates", () => {
  it("applies immediately and keeps the value when the API succeeds", async () => {
    const d = seed("test:lamp", { exposed: false });
    api.setEntityExposed.mockResolvedValue(undefined);
    await devices.setExposed("test:lamp", true);
    expect(d.exposed).toBe(true);
    expect(api.setEntityExposed).toHaveBeenCalledWith("test:lamp", true);
  });

  it("reverts `exposed` and rethrows when the API rejects", async () => {
    const d = seed("test:lamp", { exposed: false });
    api.setEntityExposed.mockRejectedValue(new Error("boom"));
    await expect(devices.setExposed("test:lamp", true)).rejects.toThrow("boom");
    expect(d.exposed).toBe(false); // the UI must not keep a change the server refused
  });

  it("reverts `deviceType` to the previous value on failure", async () => {
    const d = seed("test:lamp", { deviceType: "sensor" });
    api.setEntityType.mockRejectedValue(new Error("nope"));
    await expect(devices.setEntityType("test:lamp", "light")).rejects.toThrow();
    expect(d.deviceType).toBe("sensor");
  });

  it("is a no-op (no throw) for an entity the store doesn't hold", async () => {
    api.setEntityExposed.mockResolvedValue(undefined);
    await expect(devices.setExposed("test:ghost", true)).resolves.toBeUndefined();
  });
});

describe("replay projection", () => {
  it("writes replayed values and deletes a capability sent as null", () => {
    const d = seed("test:sensor", { caps: { temperature: { value: 20, unit: "°C", updatedAt: 1 } } });
    devices.applyReplay(1234, { "test:sensor": { temperature: 21.5 } });
    expect(d.caps.temperature.value).toBe(21.5);
    expect(devices.replayAt).toBe(1234);
    devices.applyReplay(1235, { "test:sensor": { temperature: null } });
    expect(d.caps.temperature).toBeUndefined();
  });

  it("ignores values for entities that aren't loaded", () => {
    expect(() => devices.applyReplay(1, { "test:ghost": { on_off: true } })).not.toThrow();
  });
});

describe("prettify", () => {
  it("turns a slug into words", () => {
    expect(prettify("living-room-strip")).toBe("Living Room Strip");
    expect(prettify("pmis_bea_room")).toBe("Pmis Bea Room");
  });
  it("leaves an address-like string alone", () => {
    expect(prettify("192.168.1.40")).toBe("192.168.1.40");
  });
});

describe("device list", () => {
  it("sorts by name with Croatian collation", () => {
    seed("test:b", { name: "Žarulja" });
    seed("test:a", { name: "Ante" });
    seed("test:c", { name: "Čep" });
    expect(devices.list.map((d) => d.name)).toEqual(["Ante", "Čep", "Žarulja"]);
  });

  it("reflects an added device on the next read", () => {
    seed("test:a", { name: "Ante" });
    expect(devices.list).toHaveLength(1);
    seed("test:z", { name: "Zoran" });
    expect(devices.list.map((d) => d.name)).toEqual(["Ante", "Zoran"]);
  });
  // NOTE: `list` is backed by a $derived so components reading it inside their own
  // $derived/effect don't re-sort the whole house on every tick. That caching only
  // happens inside a reactive root, so it is deliberately NOT asserted here — a
  // plain node read recomputes, and a test claiming otherwise would be false.
});

describe("snapshot load", () => {
  it("is single-flight: concurrent callers share one in-flight load", async () => {
    // The layout's two effects and the WS onopen all call load() at login; without
    // the guard that was three overlapping snapshots (12 parallel requests) racing
    // each other's deletion reconcile.
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => { release = r; });
    api.listEntities.mockImplementation(async () => { await gate; return []; });
    api.listState.mockResolvedValue([]);
    api.listAreas.mockResolvedValue([]);
    api.listDevices.mockResolvedValue([]);

    const all = Promise.all([devices.load(), devices.load(), devices.load()]);
    release();
    await all;
    expect(api.listEntities).toHaveBeenCalledTimes(1);
  });

  it("starts a fresh load once the previous one settled", async () => {
    api.listEntities.mockResolvedValue([]);
    api.listState.mockResolvedValue([]);
    api.listAreas.mockResolvedValue([]);
    api.listDevices.mockResolvedValue([]);
    await devices.load();
    await devices.load();
    expect(api.listEntities).toHaveBeenCalledTimes(2);
  });
});
