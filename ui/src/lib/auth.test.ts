// The /auth/me probe decides whether the layout guard bounces to /login, so the
// distinction between "the server says you're not logged in" and "the server is
// momentarily unreachable" is the whole ballgame: getting it wrong logs a valid
// session out on every deploy.
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = { me: vi.fn(), login: vi.fn(), logout: vi.fn() };
const assistant = { setIdentity: vi.fn() };
class Unauthorized extends Error {}
vi.mock("$lib/api", () => ({ api, Unauthorized }));
vi.mock("$lib/kit", () => ({ theme: { setTheme: vi.fn() }, i18n: { set: vi.fn() } }));
vi.mock("$lib/assistant.svelte", () => ({ assistant }));

const { auth } = await import("$lib/auth.svelte");

const ME = { id: "user-a", username: "marko", role: "admin", allowed_pages: null, can_control: true };

beforeEach(() => {
  auth.user = null;
  auth.checked = false;
  vi.clearAllMocks();
});

describe("auth identity lifecycle", () => {
  it("uses the stable user ID to reset the assistant", () => {
    auth.user = ME as never;
    expect(assistant.setIdentity).toHaveBeenLastCalledWith("user-a");
    auth.user = { ...ME, username: "renamed" } as never;
    expect(assistant.setIdentity).toHaveBeenLastCalledWith("user-a");
    auth.user = null;
    expect(assistant.setIdentity).toHaveBeenLastCalledWith(null);
  });

  it("clears the identity as soon as logout begins, even while the server is pending", async () => {
    let complete!: () => void;
    api.logout.mockReturnValue(new Promise<void>((resolve) => { complete = resolve; }));
    auth.user = ME as never;
    const logout = auth.logout();
    expect(auth.user).toBeNull();
    expect(assistant.setIdentity).toHaveBeenLastCalledWith(null);
    complete();
    await logout;
  });

  it("cannot restore an old identity from a probe that finishes after logout", async () => {
    let complete!: (user: typeof ME) => void;
    api.me.mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    api.logout.mockResolvedValue(undefined);
    auth.user = ME as never;
    const load = auth.load();
    await auth.logout();
    complete(ME);
    await load;
    expect(auth.user).toBeNull();
  });

  it("ignores a late rejection for the previous account after another login", async () => {
    let reject!: (error: Error) => void;
    api.me.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    auth.user = ME as never;
    const load = auth.load();
    const next = { ...ME, id: "user-b" };
    api.login.mockResolvedValue(next);
    await auth.login("other", "password");
    reject(new Unauthorized("401"));
    await load;
    expect(auth.user).toEqual(next);
    expect(assistant.setIdentity).toHaveBeenLastCalledWith("user-b");
  });
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
