// Which living-room source is live, and what picking one does. Harmony reports the
// activity it last started, not what the amplifier listens to: OPUS switching the
// Marantz to the Shield for a film left Harmony on ZEN, the hub showed the radio as
// playing, and play fed a DAC nobody was listening to.
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = { sendCommand: vi.fn() };
vi.mock("$lib/api", () => ({ api, stateWs: vi.fn(), Unauthorized: class Unauthorized extends Error {} }));
vi.mock("$lib/auth.svelte", () => ({ auth: { user: null } }));
vi.mock("$lib/translations.svelte", () => ({ tr: (s: string) => s, translations: { load: vi.fn() } }));

const { devices } = await import("$lib/store.svelte");
const { resolvedSources, selectSource } = await import("$lib/mediaSources");

const ZEN = {
  key: "zen", label: "MUSIC", kind: "player", remote: "harmony:hub", activity: "ZEN",
  avr: "denon:marantz_main", avr_input: "MUSIC", player: "opus:dac", nowplaying: "opus:dac",
};
const SHIELD = {
  key: "shield", label: "SHIELD", kind: "activity", remote: "harmony:hub",
  activity: "SHIELD TV", avr: "denon:marantz_main",
};

function seed(entityId: string, caps: Record<string, unknown>) {
  devices.byId[entityId] = {
    entityId, rawName: entityId, name: entityId, label: null, adapter: entityId.split(":")[0],
    deviceKey: null, hasOwnName: true, areaId: null, exposed: true, hiddenCaps: [],
    caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, { value: v, unit: null, updatedAt: 1 }])),
  } as never;
}

function house(harmony: string, amp: { on: boolean; input: string }, dac: string) {
  seed("harmony:hub", { source: harmony });
  seed("denon:marantz_main", { on_off: amp.on, source: amp.input });
  seed("opus:dac", { media_transport: dac });
}

const active = (key: string) => resolvedSources(17).find((s) => s.src.key === key)!.active;

beforeEach(() => {
  devices.byId = {};
  devices.areas = [{ id: 17, name: "Living Room", media_config: { sources: [ZEN, SHIELD] } }] as never;
  api.sendCommand.mockReset().mockResolvedValue(undefined);
});

describe("which living-room source is live", () => {
  it("Harmony still on ZEN while the amp listens to the Shield is not the radio", () => {
    house("ZEN", { on: true, input: "SHIELD" }, "playing");
    expect(active("zen")).toBe(false);
  });

  it("Harmony on ZEN with the amp on MUSIC is the radio, playing or not", () => {
    house("ZEN", { on: true, input: "MUSIC" }, "stopped");
    expect(active("zen")).toBe(true);
  });

  it("the amp on MUSIC with the DAC playing is the radio whatever Harmony says", () => {
    house("PowerOff", { on: true, input: "MUSIC" }, "playing");
    expect(active("zen")).toBe(true);
  });

  it("the amp on MUSIC with nothing playing is not the radio when Harmony is elsewhere", () => {
    house("PowerOff", { on: true, input: "MUSIC" }, "stopped");
    expect(active("zen")).toBe(false);
  });

  it("a source that declares no input still follows Harmony", () => {
    house("SHIELD TV", { on: true, input: "MUSIC" }, "stopped");
    expect(active("shield")).toBe(true);
  });
});

describe("picking a source", () => {
  it("routes the amp to the declared input, not only the Harmony activity", async () => {
    house("ZEN", { on: true, input: "SHIELD" }, "playing");
    await selectSource(resolvedSources(17).find((s) => s.src.key === "zen")!);
    expect(api.sendCommand.mock.calls.map(([c]) => c)).toEqual([
      { entity_id: "harmony:hub", capability: "source", command: "set_source", args: { value: "ZEN" } },
      { entity_id: "denon:marantz_main", capability: "on_off", command: "turn_on" },
      { entity_id: "denon:marantz_main", capability: "source", command: "set_source", args: { value: "MUSIC" } },
    ]);
  });

  it("a source without a declared input leaves the amp to Harmony", async () => {
    house("PowerOff", { on: false, input: "MUSIC" }, "stopped");
    await selectSource(resolvedSources(17).find((s) => s.src.key === "shield")!);
    expect(api.sendCommand.mock.calls.map(([c]) => c)).toEqual([
      { entity_id: "harmony:hub", capability: "source", command: "set_source", args: { value: "SHIELD TV" } },
    ]);
  });
});
