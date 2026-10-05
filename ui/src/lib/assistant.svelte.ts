import { api, Unauthorized, type AssistantTurn } from "$lib/api";
import { errMsg } from "$lib/errors";
import { t, type MessageKey } from "$lib/i18n";
import { ui } from "$lib/shell.svelte";
import { speech } from "$lib/speech.svelte";

type Message = { role: "user" | "assistant"; content: string; actions?: { type: string }[] };

const STORE_KEY = "dida.assistant.chat";
const ACTING = new Set(["send_command", "recall_scene", "set_automation_enabled", "run_automation"]);

function phaseFor(tool: string): MessageKey {
  if (tool === "create_automation") return "assistant.busyAuthoring";
  return ACTING.has(tool) ? "assistant.busyActing" : "assistant.busyReading";
}

class AssistantStore {
  messages = $state<Message[]>([]);
  input = $state("");
  busy = $state(false);
  busyLabel = $state<MessageKey>("assistant.thinking");
  error = $state<string | null>(null);
  #userId: string | null = null;
  #pending: AbortController | null = null;
  #listening: object | null = null;

  get #storageKey(): string | null {
    return this.#userId === null ? null : `${STORE_KEY}:${encodeURIComponent(this.#userId)}`;
  }

  setIdentity(userId: string | null): void {
    try {
      sessionStorage.removeItem(STORE_KEY);
    } catch {}
    if (userId !== null && userId === this.#userId) return;
    this.clear();
    ui.assistantOpen = false;
    ui.pendingQuestion = null;
    this.#userId = userId;
    const key = this.#storageKey;
    if (!key) return;
    try {
      const saved = sessionStorage.getItem(key);
      if (!saved) return;
      const messages: unknown = JSON.parse(saved);
      if (!Array.isArray(messages) || !messages.every((message) =>
        message && (message.role === "user" || message.role === "assistant") &&
        typeof message.content === "string" &&
        (message.actions === undefined || (Array.isArray(message.actions) &&
          message.actions.every((action: unknown) => action && typeof action === "object" &&
            "type" in action && typeof action.type === "string"))),
      )) {
        sessionStorage.removeItem(key);
        return;
      }
      this.messages = messages;
    } catch {
      try {
        sessionStorage.removeItem(key);
      } catch {}
    }
  }

  #persist(): void {
    const key = this.#storageKey;
    if (!key) return;
    try {
      if (this.messages.length) sessionStorage.setItem(key, JSON.stringify(this.messages));
      else sessionStorage.removeItem(key);
    } catch {}
  }

  clear(): void {
    this.#pending?.abort();
    this.#pending = null;
    this.messages = [];
    this.input = "";
    this.busy = false;
    this.busyLabel = "assistant.thinking";
    this.error = null;
    this.stopListening();
    speech.silence();
    this.#persist();
  }

  async send(text: string, spoken = false): Promise<void> {
    text = text.trim();
    if (!text || this.busy || this.#userId === null) return;
    const pending = new AbortController();
    this.#pending = pending;
    this.input = "";
    this.error = null;
    speech.silence();
    const history: AssistantTurn[] = this.messages.map((message) => ({ role: message.role, content: message.content }));
    this.messages.push({ role: "user", content: text });
    this.#persist();
    this.busy = true;
    this.busyLabel = "assistant.thinking";
    try {
      const response = await api.askAssistant(text, history, (tool) => {
        if (this.#pending === pending) this.busyLabel = phaseFor(tool);
      }, pending.signal);
      if (this.#pending !== pending) return;
      this.messages.push({ role: "assistant", content: response.reply, actions: response.actions });
      this.#persist();
      if (spoken) speech.say(response.reply);
    } catch (error) {
      if (this.#pending !== pending || error instanceof Unauthorized) return;
      this.error = errMsg(error);
    } finally {
      if (this.#pending === pending) {
        this.#pending = null;
        this.busy = false;
      }
    }
  }

  stopListening(): void {
    this.#listening = null;
    speech.stop();
  }

  talk(): void {
    if (speech.listening) {
      this.stopListening();
      return;
    }
    if (this.#userId === null) return;
    this.error = null;
    const listening = {};
    this.#listening = listening;
    speech.start(
      (text) => {
        if (this.#listening !== listening) return;
        this.#listening = null;
        void this.send(text, true);
      },
      (code) => {
        if (this.#listening === listening) this.error = t("assistant.micError", { code });
      },
    );
  }
}

export const assistant = new AssistantStore();
