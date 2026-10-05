import { afterEach, describe, expect, it, vi } from "vitest";
import { NativeClient, nativeMatches } from "./native";

const A = { provisioned: true, userId: "1", origin: "https://home.example" };

describe("native provisioning identity", () => {
  it("requires the authenticated user's stable ID", () => {
    expect(nativeMatches(A, "1", A.origin)).toBe(true);
    expect(nativeMatches(A, "2", A.origin)).toBe(false);
    expect(nativeMatches(A, undefined, A.origin)).toBe(false);
  });

  it("does not reuse another instance's provisioning, including a different port", () => {
    expect(nativeMatches(A, "1", "https://cabin.example")).toBe(false);
    expect(nativeMatches(A, "1", "https://home.example:8443")).toBe(false);
  });

  it("requires complete active provisioning", () => {
    expect(nativeMatches({ ...A, provisioned: false }, "1", A.origin)).toBe(false);
    expect(nativeMatches({ ...A, userId: null }, "1", A.origin)).toBe(false);
    expect(nativeMatches(null, "1", A.origin)).toBe(false);
  });
});

afterEach(() => vi.useRealTimers());

describe("native messaging", () => {
  it("matches replies to concurrent requests and rejects native errors", async () => {
    const transport = { postMessage: vi.fn(), onmessage: null as ((event: { data: string }) => void) | null };
    const bridge = new NativeClient(transport);
    const version = bridge.appVersion();
    const status = bridge.status();
    const requests = transport.postMessage.mock.calls.map(([data]) => JSON.parse(data));
    transport.onmessage!({ data: JSON.stringify({ id: requests[1].id, result: { provisioned: true } }) });
    transport.onmessage!({ data: JSON.stringify({ id: requests[0].id, result: "v1.0.17" }) });
    expect(await version).toBe("v1.0.17");
    expect(await status).toEqual({ provisioned: true });
    const update = bridge.checkUpdate();
    const request = JSON.parse(transport.postMessage.mock.calls[2][0]);
    transport.onmessage!({ data: JSON.stringify({ id: request.id, error: "install unavailable" }) });
    await expect(update).rejects.toThrow("install unavailable");
  });

  it("bounds a missing reply and clears requests on invalid JSON", async () => {
    vi.useFakeTimers();
    const transport = { postMessage: vi.fn(), onmessage: null as ((event: { data: string }) => void) | null };
    const bridge = new NativeClient(transport);
    const first = expect(bridge.status()).rejects.toThrow("Native request timed out");
    await vi.advanceTimersByTimeAsync(10_000);
    await first;
    const second = expect(bridge.appVersion()).rejects.toThrow("Invalid native response");
    transport.onmessage!({ data: "broken" });
    await second;
    expect(vi.getTimerCount()).toBe(0);
  });
});
