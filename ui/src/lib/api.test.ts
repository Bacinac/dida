// Every API call ends, and ends in words a person can read: a hung connection hits
// the deadline, a dead server says so, and a failed call never shows "HTTP 502".
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, Unauthorized } from "$lib/api";
import { i18n } from "$lib/kit";

function serve(status: number, body = "") {
  vi.stubGlobal("fetch", async () => new Response(body || null, { status }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  i18n.locale = "hr";
});

describe("the API client", () => {
  it("gives up on a server that never answers", async () => {
    vi.spyOn(AbortSignal, "timeout").mockReturnValue(AbortSignal.abort(new DOMException("", "TimeoutError")));
    vi.stubGlobal("fetch", (_url: string, init: RequestInit) =>
      new Promise((_, reject) => {
        if (init.signal?.aborted) reject(init.signal.reason);
        init.signal?.addEventListener("abort", () => reject(init.signal!.reason));
      }));
    await expect(api.health()).rejects.toThrow("Poslužitelj ne odgovara.");
  });

  it("says the server is unreachable instead of 'Failed to fetch'", async () => {
    vi.stubGlobal("fetch", async () => { throw new TypeError("Failed to fetch"); });
    await expect(api.health()).rejects.toThrow("Poslužitelj nije dostupan.");
  });

  it("names a bare failure by its status, in the active language", async () => {
    serve(502);
    await expect(api.health()).rejects.toThrow("Zahtjev nije uspio (502).");
    i18n.locale = "en";
    await expect(api.deleteDevice("x")).rejects.toThrow("The request failed (502).");
  });

  it("passes the server's own explanation on", async () => {
    serve(409, JSON.stringify({ detail: "device is busy" }));
    await expect(api.restoreDevice("x")).rejects.toThrow("device is busy");
  });

  it("turns a lapsed session into Unauthorized for the auth guard", async () => {
    serve(401);
    await expect(api.me()).rejects.toBeInstanceOf(Unauthorized);
  });
});
