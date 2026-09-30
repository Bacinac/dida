<script lang="ts">
  // The assistant chat. Lives in a drawer over the current page rather than at its
  // own route: asking "which lights are on" should not cost you the floor plan you
  // were looking at, and on a phone it should not spend one of four tab slots.
  import { onMount } from "svelte";
  import { Button, Notice, Tag } from "$lib/kit";
  import { api, Unauthorized, type AssistantTurn } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t, type MessageKey } from "$lib/i18n";
  import { devices } from "$lib/store.svelte";
  import { cmdLabel } from "$lib/capabilities";
  import { speech } from "$lib/speech.svelte";

  type Msg = { role: "user" | "assistant"; content: string; actions?: { type: string }[] };

  // The conversation survives navigating away and back. sessionStorage, not the
  // database, is the right scope on purpose: this is a disposable command surface,
  // not a record — a turn is "dim the kitchen", worth keeping while the tab is open
  // and worth forgetting when it closes.
  const STORE_KEY = "dida.assistant.chat";

  let messages = $state<Msg[]>([]);
  let input = $state("");
  let busy = $state(false);
  let busyLabel = $state<MessageKey>("assistant.thinking");
  let error = $state<string | null>(null);

  const exampleKeys: MessageKey[] = ["assistant.ex1", "assistant.ex2", "assistant.ex3", "assistant.ex4"];

  // Which tool is running matters to the user only as a phase — "checking" vs
  // "doing" vs "writing a rule". Naming the tool itself would leak internals and
  // need a string per tool.
  const ACTING = new Set(["send_command", "recall_scene", "set_automation_enabled", "run_automation"]);
  function phaseFor(tool: string): MessageKey {
    if (tool === "create_automation") return "assistant.busyAuthoring";
    return ACTING.has(tool) ? "assistant.busyActing" : "assistant.busyReading";
  }

  onMount(() => {
    try {
      const saved = sessionStorage.getItem(STORE_KEY);
      if (saved) messages = JSON.parse(saved);
    } catch {
      /* corrupt or unavailable storage is not worth failing the page over */
    }
  });

  $effect(() => {
    const snapshot = JSON.stringify(messages);
    try {
      if (messages.length) sessionStorage.setItem(STORE_KEY, snapshot);
      else sessionStorage.removeItem(STORE_KEY);
    } catch {
      /* private mode / quota — the chat still works, it just won't be restored */
    }
  });

  /** Wipe the conversation. Exported because the button for it belongs in the
   *  drawer's header next to the close ✕, not buried above the transcript where it
   *  was easy to miss — and a control you cannot find is a control you do not have. */
  export function clearChat() {
    messages = [];
    error = null;
    speech.silence();
  }
  export function hasMessages(): boolean {
    return messages.length > 0;
  }
  /** Ask on someone else's behalf — the header's explain-this-page button. */
  export function ask(text: string) {
    send(text);
  }

  // Only a spoken question gets a spoken answer — typing and then being read to
  // out loud is startling, and on a shared surface it is worse than useless.
  let spokenTurn = $state(false);

  async function send(text: string, spoken = false) {
    text = text.trim();
    if (!text || busy) return;
    input = "";
    error = null;
    spokenTurn = spoken;
    speech.silence();
    const history: AssistantTurn[] = messages.map((m) => ({ role: m.role, content: m.content }));
    messages.push({ role: "user", content: text });
    busy = true;
    busyLabel = "assistant.thinking";
    try {
      const res = await api.askAssistant(text, history, (tool) => {
        busyLabel = phaseFor(tool);
      });
      messages.push({ role: "assistant", content: res.reply, actions: res.actions });
      // Spoken back in the language that was SPOKEN, which is also the language the
      // reply came in — the UI's locale has nothing to do with either.
      if (spokenTurn) speech.say(res.reply);
    } catch (e) {
      if (e instanceof Unauthorized) return;
      error = errMsg(e);
    } finally {
      busy = false;
    }
  }

  function talk() {
    if (speech.listening) {
      speech.stop();
      return;
    }
    error = null;
    speech.start(
      (text) => send(text, true),
      (code) => (error = t("assistant.micError", { code })),
    );
  }

  function actionLabel(a: { type: string; [k: string]: unknown }): string {
    if (a.type === "command") {
      // Device name, not entity_id. The prompt forbids the assistant from saying
      // "esphome:203_0_113_59:irrigation_valve_1" out loud; printing it right
      // underneath the reply made that rule pointless.
      const id = String(a.entity_id);
      return `⚡ ${devices.byId[id]?.name ?? id} · ${cmdLabel(String(a.command))}`;
    }
    if (a.type === "automation") return t("assistant.actionAutomation", { name: String(a.name) });
    if (a.type === "automation_enabled")
      return t(a.enabled ? "assistant.actionRuleOn" : "assistant.actionRuleOff", { name: String(a.name) });
    if (a.type === "automation_run") return t("assistant.actionRuleRun", { name: String(a.name) });
    if (a.type === "scene") return t("assistant.actionScene", { name: String(a.name) });
    return a.type;
  }
</script>

<div class="flex h-full flex-col">
  <div class="flex-1 space-y-3 overflow-auto pb-4">
    {#if messages.length === 0}
      <div class="rounded-lg border border-dashed border-dida-border p-6 text-center">
        <p class="text-dida-text-muted">{t("assistant.empty")}</p>
        <div class="mt-3 flex flex-wrap justify-center gap-2">
          {#each exampleKeys as ex (ex)}
            <Button size="small" onclick={() => send(t(ex))}>{t(ex)}</Button>
          {/each}
        </div>
      </div>
    {/if}

    {#each messages as m (m)}
      <div class="flex {m.role === 'user' ? 'justify-end' : 'justify-start'}">
        <div
          class="max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-m {m.role === 'user'
            ? 'bg-dida-accent-strong text-dida-on-accent'
            : 'border border-dida-border bg-dida-panel text-dida-text'}"
        >
          {m.content}
          {#if m.actions && m.actions.length}
            <div class="mt-2 flex flex-wrap gap-1">
              {#each m.actions as a (a)}
                <Tag tone="quiet">{actionLabel(a)}</Tag>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    {/each}

    {#if busy}
      <div class="flex justify-start">
        <div class="rounded-lg border border-dida-border bg-dida-panel px-3 py-2 text-m text-dida-text-faint">
          {t(busyLabel)}
        </div>
      </div>
    {/if}

    {#if error}
      <Notice tone="err">{error}</Notice>
    {/if}
  </div>

  <form class="flex gap-2 border-t border-dida-border pt-3" onsubmit={(e) => { e.preventDefault(); send(input); }}>
    {#if speech.available}
      <Button
        tone={speech.listening ? "danger" : "quiet"}
        onclick={talk}
        disabled={busy}
        label={t(speech.listening ? "assistant.micStop" : "assistant.micStart")}
        title={t(speech.listening ? "assistant.micStop" : "assistant.micStart")}
      >{speech.listening ? "■" : "🎤"}</Button>
      <!-- Which language is being LISTENED for. Visible because when it is wrong
           nothing works and there is no other clue why. -->
      <Button size="small"
        onclick={() => speech.setLang(speech.nextLang())}
        disabled={busy || speech.listening}
        title={t("assistant.micLang")}
      >{speech.lang.toUpperCase()}</Button>
    {/if}
    <input
      bind:value={input}
      placeholder={t(speech.listening ? "assistant.listening" : "assistant.placeholder")}
      disabled={busy}
      class="flex-1"
    />
    <Button tone="primary" type="submit" disabled={busy || !input.trim()}
    >{t("assistant.send")}</Button>
  </form>

  {#if !speech.available}
    <!-- Say WHY there is no mic. Silently omitting it reads as a DIDA bug, when
         the cause is the origin: the browser only exposes speech on https. -->
    <p class="pt-1 text-xs text-dida-text-faint">
      {t(speech.unavailableReason === "insecure" ? "assistant.micInsecure" : "assistant.micUnsupported")}
    </p>
  {/if}
</div>
