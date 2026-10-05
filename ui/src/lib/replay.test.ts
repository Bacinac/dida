import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = {
  replayBundle: vi.fn(), listEntities: vi.fn(), listState: vi.fn(),
  listAreas: vi.fn(), listDevices: vi.fn(),
};
const auth = { user: { id: "a" } as { id: string } | null, revision: 0 };
class Unauthorized extends Error {}
const ws = { readyState: 1, close: vi.fn(), send: vi.fn(), onmessage: null as ((event: { data: string }) => void) | null };
vi.mock("$lib/api", () => ({ api, Unauthorized, stateWs: () => ws }));
vi.mock("$lib/auth.svelte", () => ({ auth }));
vi.mock("$lib/i18n", () => ({ t: (key: string) => key }));
vi.mock("$lib/translations.svelte", () => ({ tr: (s: string) => s, translations: { load: vi.fn() } }));
const { replay } = await import("$lib/replay.svelte");
const { devices } = await import("$lib/store.svelte");

const ID = "test:sensor";
function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function bundle(value = 16) {
  const now = Date.now();
  return { frm: now - 20_000, to: now, truncated: false, tracks: [
    { e: ID, c: "on_off", k: "s", at: false, p: [[now - 200, true], [now - 100, false]] },
    { e: ID, c: "temperature", k: "n", at: value, p: [[now - 100, value]] },
  ] };
}
function rows() {
  return [
    { entity_id: ID, capability: "on_off", value: false, unit: null, updated_at: new Date(Date.now() - 100).toISOString() },
    { entity_id: ID, capability: "temperature", value: 23, unit: "°C", updated_at: new Date(Date.now() - 100).toISOString() },
  ];
}
function emitTemperature(value: number) {
  ws.onmessage?.({ data: JSON.stringify({ entity_id: ID, capability: "temperature", value, unit: "°C", ts_ns: (Date.now() + 1) * 1e6 }) });
}
beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-05T12:00:00+02:00"));
  vi.stubGlobal("document", { addEventListener: vi.fn(), removeEventListener: vi.fn(), visibilityState: "visible" });
  vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn() });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("requestAnimationFrame", vi.fn().mockReturnValue(1));
  devices.stop();
  await replay.close();
  for (const fn of Object.values(api)) fn.mockReset();
  auth.user = { id: "a" };
  auth.revision++;
  api.listEntities.mockResolvedValue([{ entity_id: ID, capabilities: ["on_off", "temperature"], name: "Sensor" }]);
  api.listState.mockImplementation(async () => rows());
  api.listAreas.mockResolvedValue([]);
  api.listDevices.mockResolvedValue([]);
  api.replayBundle.mockImplementation(async () => bundle());
  await devices.load();
});
afterEach(async () => {
  devices.stop();
  await replay.close();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("replay and live projection", () => {
  it("restores a held pulse and numeric track even when their live timestamps precede the cursor", async () => {
    await replay.open([ID]);
    replay.seek(replay.to);
    expect(devices.byId[ID].caps.on_off.value).toBe(true);
    expect(devices.byId[ID].caps.temperature.value).toBe(16);
    await replay.close();
    expect(devices.replaying).toBe(false);
    expect(devices.byId[ID].caps.on_off.value).toBe(false);
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });

  it("keeps a newer WS delta received while the exit snapshot is loading", async () => {
    devices.start();
    await replay.open([ID]);
    replay.seek(replay.to);
    const state = deferred();
    api.listState.mockReturnValueOnce(state.promise);
    const closing = replay.close();
    emitTemperature(29);
    state.resolve(rows());
    await closing;
    expect(devices.byId[ID].caps.temperature.value).toBe(29);
  });

  it("discards a live snapshot that was started before entering replay", async () => {
    const state = deferred();
    api.listState.mockReturnValueOnce(state.promise);
    const loading = devices.load();
    await replay.open([ID]);
    replay.seek(replay.to);
    state.resolve(rows());
    await loading;
    expect(devices.byId[ID].caps.on_off.value).toBe(true);
    expect(devices.byId[ID].caps.temperature.value).toBe(16);
    const calls = api.listState.mock.calls.length;
    await devices.load();
    expect(api.listState).toHaveBeenCalledTimes(calls);
    await replay.close();
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });

  it("starts a fresh exit snapshot instead of sharing a pre-replay pending load", async () => {
    const state = deferred();
    api.listState.mockReturnValueOnce(state.promise);
    const oldLoad = devices.load();
    await replay.open([ID]);
    replay.seek(replay.to);
    await replay.close();
    state.resolve([{ ...rows()[1], value: 99 }]);
    await oldLoad;
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });
});

describe("replay request lifecycle", () => {
  it("aborts a pending open on close and ignores a transport that still returns history", async () => {
    const pending = deferred();
    api.replayBundle.mockReturnValueOnce(pending.promise);
    const opening = replay.open([ID]);
    const signal = api.replayBundle.mock.calls[0][3] as AbortSignal;
    await replay.close();
    expect(signal.aborted).toBe(true);
    expect(replay.loading).toBe(false);
    pending.resolve(bundle());
    await opening;
    expect(replay.active).toBe(false);
    expect(devices.replaying).toBe(false);
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });

  it("keeps the newer open pending when an aborted older one finishes", async () => {
    const first = deferred();
    const second = deferred();
    api.replayBundle.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const oldOpen = replay.open([ID], 6);
    const newOpen = replay.open([ID], 24);
    first.resolve(bundle(11));
    await oldOpen;
    expect(replay.loading).toBe(true);
    second.resolve(bundle(31));
    await newOpen;
    expect(replay.active).toBe(true);
    expect(devices.byId[ID].caps.temperature.value).toBe(31);
  });

  it("returns to live when an active replay is reopened with an empty window", async () => {
    await replay.open([ID]);
    api.replayBundle.mockResolvedValueOnce({ ...bundle(), tracks: [] });
    await replay.reopen(6);
    expect(replay.error).toBe("replay.noHistory");
    expect(replay.active).toBe(false);
    expect(devices.replaying).toBe(false);
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });

  it("ignores an old failure after a newer open has succeeded", async () => {
    const pending = deferred();
    api.replayBundle.mockReturnValueOnce(pending.promise);
    const oldOpen = replay.open([ID]);
    await replay.open([ID]);
    pending.reject(new Error("old failure"));
    await oldOpen;
    expect(replay.error).toBeNull();
    expect(replay.active).toBe(true);
  });

  it("cannot apply the previous account's history after its session revision changes", async () => {
    const pending = deferred();
    api.replayBundle.mockReturnValueOnce(pending.promise);
    const opening = replay.open([ID]);
    auth.user = { id: "b" };
    auth.revision++;
    pending.resolve(bundle(99));
    await opening;
    expect(replay.active).toBe(false);
    expect(devices.replaying).toBe(false);
    expect(devices.byId[ID].caps.temperature.value).toBe(23);
  });
});
