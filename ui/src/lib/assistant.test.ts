import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = { askAssistant: vi.fn() };
const speech = {
  listening: false, stop: vi.fn(), silence: vi.fn(), say: vi.fn(), start: vi.fn(),
};
vi.mock("$lib/api", () => ({ api, Unauthorized: class Unauthorized extends Error {} }));
vi.mock("$lib/speech.svelte", () => ({ speech }));

const { assistant } = await import("$lib/assistant.svelte");
const { ui } = await import("$lib/shell.svelte");
const { auth } = await import("$lib/auth.svelte");

const USER_A = { id: "user-a", username: "same-name", role: "admin" };
const USER_B = { id: "user-b", username: "same-name", role: "user" };
let stored: Map<string, string>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  stored = new Map();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
  });
  auth.user = null;
  vi.clearAllMocks();
  speech.listening = false;
});

afterEach(() => {
  auth.user = null;
  vi.unstubAllGlobals();
});

describe("assistant identity boundary", () => {
  it("restores only the authenticated ID's transcript and deletes the unscoped transcript", () => {
    stored.set("dida.assistant.chat", JSON.stringify([{ role: "assistant", content: "legacy private" }]));
    stored.set("dida.assistant.chat:user-a", JSON.stringify([{ role: "assistant", content: "A private" }]));
    stored.set("dida.assistant.chat:user-b", JSON.stringify([{ role: "assistant", content: "B private" }]));
    auth.user = USER_B as never;
    expect(assistant.messages.map((message) => message.content)).toEqual(["B private"]);
    expect(stored.has("dida.assistant.chat")).toBe(false);
  });

  it("closes an open drawer, discards the old state and sends no A history after A → B", async () => {
    auth.user = USER_A as never;
    api.askAssistant.mockResolvedValue({ reply: "A private answer", actions: [] });
    await assistant.send("A private question");
    expect(stored.has("dida.assistant.chat:user-a")).toBe(true);
    ui.assistantOpen = true;
    ui.pendingQuestion = "A queued question";
    assistant.input = "A draft";
    assistant.error = "A private error";
    auth.user = null;
    expect(ui.assistantOpen).toBe(false);
    expect(ui.pendingQuestion).toBeNull();
    expect(assistant.messages).toEqual([]);
    expect(assistant.input).toBe("");
    expect(assistant.error).toBeNull();
    expect(stored.has("dida.assistant.chat:user-a")).toBe(false);
    auth.user = USER_B as never;
    api.askAssistant.mockResolvedValue({ reply: "B answer", actions: [] });
    await assistant.send("B question");
    expect(api.askAssistant).toHaveBeenLastCalledWith("B question", [], expect.any(Function), expect.any(AbortSignal));
    expect(assistant.messages.map((message) => message.content)).toEqual(["B question", "B answer"]);
    expect(stored.get("dida.assistant.chat:user-b")).not.toContain("A private");
  });

  it("retains the conversation after the same ID's username changes", async () => {
    auth.user = USER_A as never;
    api.askAssistant.mockResolvedValue({ reply: "answer", actions: [] });
    await assistant.send("question");
    ui.assistantOpen = true;
    auth.user = { ...USER_A, username: "renamed" } as never;
    expect(ui.assistantOpen).toBe(true);
    expect(assistant.messages).toHaveLength(2);
  });

  it("aborts A's pending turn and ignores its late phases, reply, speech and finalizer while B is pending", async () => {
    auth.user = USER_A as never;
    const old = deferred<{ reply: string; actions: [] }>();
    api.askAssistant.mockReturnValueOnce(old.promise);
    const turnA = assistant.send("A private question", true);
    const [, , phaseA, signalA] = api.askAssistant.mock.calls[0];
    ui.assistantOpen = true;
    auth.user = USER_B as never;
    expect(signalA.aborted).toBe(true);
    expect(assistant.busy).toBe(false);
    expect(ui.assistantOpen).toBe(false);
    const current = deferred<{ reply: string; actions: [] }>();
    api.askAssistant.mockReturnValueOnce(current.promise);
    const turnB = assistant.send("B question");
    phaseA("send_command");
    old.resolve({ reply: "A private answer", actions: [] });
    await turnA;
    expect(assistant.messages.map((message) => message.content)).toEqual(["B question"]);
    expect(assistant.busyLabel).toBe("assistant.thinking");
    expect(assistant.busy).toBe(true);
    expect(speech.say).not.toHaveBeenCalled();
    current.resolve({ reply: "B answer", actions: [] });
    await turnB;
    expect(assistant.busy).toBe(false);
    expect(assistant.messages.map((message) => message.content)).toEqual(["B question", "B answer"]);
  });

  it("discards a late error from the previous account", async () => {
    auth.user = USER_A as never;
    const old = deferred<never>();
    api.askAssistant.mockReturnValueOnce(old.promise);
    const turn = assistant.send("A question");
    auth.user = USER_B as never;
    old.reject(new Error("A private failure"));
    await turn;
    expect(assistant.error).toBeNull();
    expect(assistant.messages).toEqual([]);
  });

  it("discards pending speech recognition from the previous account", () => {
    auth.user = USER_A as never;
    assistant.talk();
    const [onText, onError] = speech.start.mock.calls[0];
    auth.user = USER_B as never;
    onText("A spoken question");
    onError("A private error");
    expect(speech.stop).toHaveBeenCalled();
    expect(api.askAssistant).not.toHaveBeenCalled();
    expect(assistant.error).toBeNull();
  });

  it("cancels a cleared turn so its late reply cannot repopulate the new conversation", async () => {
    auth.user = USER_A as never;
    const old = deferred<{ reply: string; actions: [] }>();
    api.askAssistant.mockReturnValueOnce(old.promise);
    const turn = assistant.send("old question");
    const signal = api.askAssistant.mock.calls[0][3];
    assistant.clear();
    expect(signal.aborted).toBe(true);
    old.resolve({ reply: "old answer", actions: [] });
    await turn;
    expect(assistant.messages).toEqual([]);
    expect(assistant.busy).toBe(false);
    expect(stored.has("dida.assistant.chat:user-a")).toBe(false);
  });

  it("rejects malformed stored messages and never sends without an identity", async () => {
    stored.set("dida.assistant.chat:user-a", JSON.stringify([{ role: "assistant", content: "x", actions: [{}] }]));
    auth.user = USER_A as never;
    expect(assistant.messages).toEqual([]);
    expect(stored.has("dida.assistant.chat:user-a")).toBe(false);
    auth.user = null;
    await assistant.send("question");
    expect(api.askAssistant).not.toHaveBeenCalled();
  });
});
