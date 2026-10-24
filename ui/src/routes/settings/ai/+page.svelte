<script lang="ts">
  import { onMount } from "svelte";
  import { AiProviderCard, Notice, type AiRole } from "#lib/kit/index.js";
  import { api, type AiSettings } from "#lib/api.js";
  import { errMsg } from "#lib/errors.js";
  import { t, type MessageKey } from "#lib/i18n/index.js";
  import { auth } from "#lib/auth.svelte.js";

  const ROLE_WORDS: Record<string, [MessageKey, MessageKey]> = {
    chat: ["settings.aiChat", "settings.aiChatHint"],
    author: ["settings.aiAuthor", "settings.aiAuthorHint"],
  };

  let ai = $state<AiSettings | null>(null);
  let err = $state<string | null>(null);

  const roles = $derived<AiRole[]>(
    (ai?.roles ?? []).map((key) => {
      const words = ROLE_WORDS[key];
      return words ? { key, label: t(words[0]), hint: t(words[1]) } : { key, label: key };
    }),
  );

  onMount(async () => {
    try {
      ai = await api.getAiSettings();
    } catch (e) {
      err = errMsg(e);
    }
  });
</script>

<svelte:head><title>{t("settings.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else if err}
  <Notice tone="err">{err}</Notice>
{:else if ai}
  <div class="space-y-4">
    {#each ai.providers as provider (provider)}
      <AiProviderCard {provider} {roles} stored={ai.settings.find((s) => s.provider === provider) ?? null} call={api.ai} />
    {/each}
  </div>
{/if}
