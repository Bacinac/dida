<script lang="ts">
  import { onMount } from "svelte";
  import { Button, SaveButton } from "$lib/kit";
  import { api, type Settings } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";

  // One uniform surface for every stored key: a configured badge, a Test button
  // and a result line — no provider is a second-class citizen. What Test means
  // is per-provider (LLMs: auth ping; OwnTracks: freshest received location, the
  // only honest end-to-end signal a symmetric payload key has).
  type Provider = "anthropic" | "openai" | "owntracks";
  type TestRes = { ok: boolean; detail: string };
  let settings = $state<Settings | null>(null);
  let anthropicKey = $state("");
  let openaiKey = $state("");
  let owntracksKey = $state("");
  let saving = $state(false);
  let saveMsg = $state<string | null>(null);
  let testing = $state<string | null>(null);
  let testResult = $state<Record<string, TestRes | undefined>>({});

  async function loadSettings() {
    try {
      settings = await api.getSettings();
    } catch { settings = null; }
  }

  // The typed (unsaved) values a test should validate, per provider. OwnTracks
  // passes nothing — its test reads the receive chain, not the input field.
  function typedFor(p: Provider): { key?: string; url?: string } {
    if (p === "anthropic") return { key: anthropicKey.trim() || undefined };
    if (p === "openai") return { key: openaiKey.trim() || undefined };
    return {};
  }
  async function testProvider(p: Provider) {
    testing = p;
    testResult[p] = undefined;
    const v = typedFor(p);
    try {
      testResult[p] = await api.testSettings(p, v.key, v.url);
    } catch (e) {
      testResult[p] = { ok: false, detail: errMsg(e) };
    } finally {
      testing = null;
    }
  }

  async function saveSettings() {
    saving = true; saveMsg = null;
    try {
      const body: { anthropic_api_key?: string; openai_api_key?: string; owntracks_secret?: string } = {};
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim();
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim();
      if (owntracksKey.trim()) body.owntracks_secret = owntracksKey.trim();
      if (Object.keys(body).length === 0) { saveMsg = t("settings.nothing"); return; }
      // Pre-test everything testable that's being CHANGED — a bad key/url must
      // fail the save, not get stored and break quietly later. (OwnTracks is
      // exempt: its chain can only be observed after a phone reports.)
      const pretest: [Provider, boolean][] = [
        ["anthropic", !!body.anthropic_api_key],
        ["openai", !!body.openai_api_key],
      ];
      for (const [provider, changed] of pretest) {
        if (!changed) continue;
        const v = typedFor(provider);
        const r = await api.testSettings(provider, v.key, v.url);
        testResult[provider] = r;
        if (!r.ok) { saveMsg = t("settings.keyBad", { provider, detail: r.detail }); return; }
      }
      await api.updateSettings(body);
      anthropicKey = ""; openaiKey = ""; owntracksKey = "";
      await loadSettings();
      saveMsg = t("settings.savedVerified");   // this page really does test the key first
    } catch (e) {
      saveMsg = errMsg(e);
    } finally {
      saving = false;
    }
  }
  onMount(loadSettings);
</script>

<svelte:head><title>{t("settings.title")}</title></svelte:head>

{#snippet badge(configured: boolean)}
  <span class="text-s {configured ? 'text-dida-ok' : 'text-dida-text-faint'}">
    {configured ? t("settings.configured") : t("settings.notSet")}
  </span>
{/snippet}

{#snippet testBtn(p: Provider)}
  <Button onclick={() => testProvider(p)} disabled={testing === p}>{testing === p ? "…" : t("common.test")}</Button>
{/snippet}

{#snippet testLine(p: Provider)}
  {#if testResult[p]}
    <p class="mt-1 text-s {testResult[p].ok ? 'text-dida-ok' : 'text-dida-danger'}">
      {testResult[p].ok
        ? (testResult[p].detail === "radi" ? t("settings.keyWorks") : testResult[p].detail)
        : t("settings.keyFails", { detail: testResult[p].detail })}
    </p>
  {/if}
{/snippet}

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else if settings}
  <div class="rounded-lg border border-dida-border bg-dida-panel p-4">
    <p class="mb-3 text-s text-dida-text-faint">{t("settings.intro")}</p>
    <div class="space-y-3">
      <div>
        <div class="mb-1 flex items-center justify-between">
          <label for="ak" class="text-m font-medium">Anthropic (Claude)</label>
          {@render badge(settings.anthropic_configured)}
        </div>
        <div class="flex gap-2">
          <input id="ak" type="password" bind:value={anthropicKey} autocomplete="new-password"
            placeholder={settings.anthropic_configured ? `sk-ant-••••••••••${settings.anthropic_hint}` : "sk-ant-…"}
            class="flex-1 font-mono" />
          {@render testBtn("anthropic")}
        </div>
        {@render testLine("anthropic")}
      </div>
      <div>
        <div class="mb-1 flex items-center justify-between">
          <label for="ok" class="text-m font-medium">OpenAI</label>
          {@render badge(settings.openai_configured)}
        </div>
        <div class="flex gap-2">
          <input id="ok" type="password" bind:value={openaiKey} autocomplete="new-password"
            placeholder={settings.openai_configured ? `sk-••••••••••${settings.openai_hint}` : "sk-…"}
            class="flex-1 font-mono" />
          {@render testBtn("openai")}
        </div>
        {@render testLine("openai")}
      </div>
      <div>
        <div class="mb-1 flex items-center justify-between">
          <label for="otk" class="text-m font-medium">{t("settings.owntracks")}</label>
          {@render badge(settings.owntracks_configured)}
        </div>
        <div class="flex gap-2">
          <input id="otk" type="password" bind:value={owntracksKey} autocomplete="new-password"
            placeholder={settings.owntracks_configured ? "••••••••" : t("settings.owntracksPlaceholder")}
            class="flex-1 font-mono" />
          {@render testBtn("owntracks")}
        </div>
        {@render testLine("owntracks")}
        <p class="mt-1 text-s text-dida-text-faint">{t("settings.owntracksHint")}</p>
      </div>
    </div>
    <div class="mt-4 flex items-center gap-3">
      <SaveButton size="small" dirty={!!(anthropicKey.trim() || openaiKey.trim() || owntracksKey.trim())} {saving} onclick={saveSettings} />
      {#if saveMsg}<span class="text-s text-dida-text-muted">{saveMsg}</span>{/if}
    </div>
  </div>
{/if}
