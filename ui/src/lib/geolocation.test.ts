import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = { reportPresence: vi.fn() };
vi.mock("$lib/api", () => ({ api }));
const { geo } = await import("$lib/geolocation.svelte");

let watch: PositionCallback;
let visibility: () => void;
const beacon = vi.fn();

function position(accuracy: number, latitude = 45): GeolocationPosition {
  return { coords: { latitude, longitude: 16, accuracy }, timestamp: Date.now() } as GeolocationPosition;
}

function hide(): void {
  Object.assign(document, { visibilityState: "hidden" });
  visibility();
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-05T12:00:00+02:00"));
  vi.stubGlobal("window", { localStorage: { setItem: vi.fn() }, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  vi.stubGlobal("document", {
    visibilityState: "visible",
    addEventListener: vi.fn((name, fn) => { if (name === "visibilitychange") visibility = fn; }),
    removeEventListener: vi.fn(),
  });
  vi.stubGlobal("navigator", {
    sendBeacon: beacon,
    geolocation: {
      watchPosition: vi.fn((callback) => { watch = callback; return 7; }),
      clearWatch: vi.fn(), getCurrentPosition: vi.fn(),
    },
  });
  vi.clearAllMocks();
  api.reportPresence.mockReset();
  geo.setEnabled(true);
});

afterEach(() => {
  geo.setEnabled(false);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("location visibility flush", () => {
  it("does not beacon a coarse fix before the server answers or after rejection", async () => {
    let resolve!: (value: { accepted: boolean }) => void;
    api.reportPresence.mockReturnValue(new Promise((done) => { resolve = done; }));
    watch(position(5_000));
    hide();
    expect(beacon).not.toHaveBeenCalled();
    resolve({ accepted: false });
    await settle();
    hide();
    expect(beacon).not.toHaveBeenCalled();
    expect(geo.lastAt).toBeNull();
  });

  it("retains the accepted fix's accuracy and observation time when a later fix is rejected", async () => {
    api.reportPresence.mockResolvedValue({ accepted: true, zone: "Home" });
    const accepted = position(15);
    watch(accepted);
    await settle();
    vi.advanceTimersByTime(91_000);
    api.reportPresence.mockResolvedValue({ accepted: false });
    watch(position(5_000, 46));
    hide();
    const body = JSON.parse(await beacon.mock.calls[0][1].text());
    expect(body).toEqual({ latitude: 45, longitude: 16, accuracy: 15, tst: accepted.timestamp / 1000 });
    await settle();
    expect(geo.lastAt).toBe(accepted.timestamp);
    expect(geo.zone).toBe("Home");
  });

  it("reports a more accurate replacement even inside the throttle window", async () => {
    api.reportPresence.mockResolvedValue({ accepted: false });
    watch(position(5_000));
    await settle();
    watch(position(15));
    expect(api.reportPresence).toHaveBeenCalledTimes(2);
    expect(api.reportPresence).toHaveBeenLastCalledWith(45, 16, Date.now() / 1000, 15);
  });

  it("ignores old watch callbacks and responses after an account change", async () => {
    let resolve!: (value: { accepted: boolean; zone: string }) => void;
    api.reportPresence.mockReturnValue(new Promise((done) => { resolve = done; }));
    const oldWatch = watch;
    oldWatch(position(15));
    geo.stop();
    geo.start();
    oldWatch(position(15, 47));
    expect(api.reportPresence).toHaveBeenCalledTimes(1);
    resolve({ accepted: true, zone: "Private old zone" });
    await settle();
    hide();
    expect(beacon).not.toHaveBeenCalled();
    expect(geo.zone).toBeNull();
  });

  it("cannot regress to an older response that arrives after a newer fix", async () => {
    let resolve!: (value: { accepted: boolean; zone: string }) => void;
    api.reportPresence.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    watch(position(15));
    vi.advanceTimersByTime(1_000);
    api.reportPresence.mockResolvedValue({ accepted: true, zone: "New zone" });
    watch(position(15, 46));
    await settle();
    resolve({ accepted: true, zone: "Old zone" });
    await settle();
    expect(geo.zone).toBe("New zone");
  });
});
