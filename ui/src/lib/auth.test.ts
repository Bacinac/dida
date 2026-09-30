// The /auth/me probe decides whether the layout guard bounces to /login, so the
// distinction between "the server says you're not logged in" and "the server is
// momentarily unreachable" is the whole ballgame: getting it wrong logs a valid
// session out on every deploy.
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = { me: vi.fn(), login: vi.fn() };
class Unauthorized extends Error {}
vi.mock("$lib/api", () => ({ api, Unauthorized }));
vi.mock("$lib/kit", () => ({ theme: { setTheme: vi.fn() }, i18n: { set: vi.fn() } }));

const { auth } = await import("$lib/auth.svelte");

const ME = { username: "marko", role: "admin", allowed_pages: null, can_control: true };

beforeEach(() => {
  auth.user = null;
  auth.checked = false;
});

describe("auth.load", () => {
  it("stores the user when the probe succeeds", async () => {
    api.me.mockResolvedValue(ME);
    await auth.load();
    expect(auth.user).toEqual(ME);
    expect(auth.checked).toBe(true);
  });

  it("clears the session on a 401 — a definitive rejection", async () => {
    auth.user = ME as never;
    api.me.mockRejectedValue(new Unauthorized("401"));
    await auth.load();
    expect(auth.user).toBeNull();
    expect(auth.checked).toBe(true);
  });

  it("KEEPS the session when the API is merely unreachable (502/network)", async () => {
    // The regression this guards: an API restart (every deploy) used to null the
    // user and bounce an open tab to /login with a still-valid cookie.
    auth.user = ME as never;
    api.me.mockRejectedValue(new Error("HTTP 502"));
    await auth.load();
    expect(auth.user).toEqual(ME);
    expect(auth.checked).toBe(true);
  });
});
