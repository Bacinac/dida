<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Notice, Tag } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { devices } from "$lib/store.svelte";
  import { cmdLabel } from "$lib/capabilities";
  import { speech } from "$lib/speech.svelte";
  import { assistant } from "$lib/assistant.svelte";

  const exampleKeys: MessageKey[] = ["assistant.ex1", "assistant.ex2", "assistant.ex3", "assistant.ex4"];

  onMount(() => {
    return () => assistant.stopListening();
  });

  export function clearChat() {
    assistant.clear();
  }
  export function hasMessages(): boolean {
    return assistant.messages.length > 0;
  }
  export function ask(text: string) {
    void assistant.send(text);
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
    {#if assistant.messages.length === 0}
      <div class="rounded-lg border border-dashed border-dida-border p-6 text-center">
        <p class="text-dida-text-muted">{t("assistant.empty")}</p>
        <div class="mt-3 flex flex-wrap justify-center gap-2">
          {#each exampleKeys as ex (ex)}
            <Button size="small" onclick={() => assistant.send(t(ex))}>{t(ex)}</Button>
          {/each}
        </div>
      </div>
    {/if}

    {#each assistant.messages as m (m)}
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

    {#if assistant.busy}
      <div class="flex justify-start">
        <div class="rounded-lg border border-dida-border bg-dida-panel px-3 py-2 text-m text-dida-text-faint">
          {t(assistant.busyLabel)}
        </div>
      </div>
    {/if}

    {#if assistant.error}
      <Notice tone="err">{assistant.error}</Notice>
    {/if}
  </div>

  <form class="flex gap-2 border-t border-dida-border pt-3" onsubmit={(e) => { e.preventDefault(); void assistant.send(assistant.input); }}>
    {#if speech.available}
      <Button
        tone={speech.listening ? "danger" : "quiet"}
        onclick={() => assistant.talk()}
        disabled={assistant.busy}
        label={t(speech.listening ? "assistant.micStop" : "assistant.micStart")}
        title={t(speech.listening ? "assistant.micStop" : "assistant.micStart")}
      >{speech.listening ? "■" : "🎤"}</Button>
      <!-- Which language is being LISTENED for. Visible because when it is wrong
           nothing works and there is no other clue why. -->
      <Button size="small"
        onclick={() => speech.setLang(speech.nextLang())}
        disabled={assistant.busy || speech.listening}
        title={t("assistant.micLang")}
      >{speech.lang.toUpperCase()}</Button>
    {/if}
    <input
      bind:value={assistant.input}
      placeholder={t(speech.listening ? "assistant.listening" : "assistant.placeholder")}
      disabled={assistant.busy}
      class="flex-1"
    />
    <Button tone="primary" type="submit" disabled={assistant.busy || !assistant.input.trim()}
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
