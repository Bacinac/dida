<script lang="ts">
  import { onMount } from "svelte";
  import { Button, SaveButton, dialog, formatRuntime } from "$lib/kit";
  import { api, type Settings, type SettingCheck } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";

  // The OwnTracks payload key. Its Test reads the freshest received location —
  // the only honest end-to-end signal a symmetric key has. AI keys live on
  // Settings → AI.
  let settings = $state<Settings | null>(null);
  let owntracksKey = $state("");
  let saving = $state(false);
  let saveMsg = $state<string | null>(null);
  let testing = $state(false);
  let testResult = $state<SettingCheck | null>(null);
  const said = (r: SettingCheck) =>
    r.age_min == null
      ? r.detail
      : r.age_min
        ? t("settings.owntracksLast", { age: formatRuntime(r.age_min) })
        : t("settings.owntracksNow");

  async function loadSettings() {
    try {
      settings = await api.getSettings();
    } catch { settings = null; }
  }

  async function testOwntracks() {
    testing = true;
    testResult = null;
    try {
      testResult = await api.testSettings("owntracks");
    } catch (e) {
      testResult = { ok: false, detail: errMsg(e) };
    } finally {
      testing = false;
    }
  }

  async function removeOwntracks() {
    const sure = await dialog.confirm({
      title: t("settings.removeKey"),
      message: t("settings.removeOwntracks"),
      confirmLabel: t("settings.removeKey"),
      danger: true,
    });
    if (!sure) return;
    saveMsg = null;
    try {
      await api.updateSettings({ owntracks_secret: "" });
      testResult = null;
      await loadSettings();
    } catch (e) {
      saveMsg = errMsg(e);
    }
  }

  async function saveSettings() {
    saving = true; saveMsg = null;
    try {
      await api.updateSettings({ owntracks_secret: owntracksKey.trim() });
      owntracksKey = "";
      await loadSettings();
      saveMsg = t("settings.saved");
    } catch (e) {
      saveMsg = errMsg(e);
    } finally {
      saving = false;
    }
  }
  onMount(loadSettings);
</script>

<svelte:head><title>{t("settings.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else if settings}
  <div class="rounded-lg border border-dida-border bg-dida-panel p-4">
    <p class="mb-3 text-s text-dida-text-faint">{t("settings.intro")}</p>
    <div>
      <div class="mb-1 flex items-center justify-between">
        <label for="otk" class="text-m font-medium">{t("settings.owntracks")}</label>
        <span class="text-s {settings.owntracks_configured ? 'text-dida-ok' : 'text-dida-text-faint'}">
          {settings.owntracks_configured ? t("settings.configured") : t("settings.notSet")}
        </span>
      </div>
      <div class="flex gap-2">
        <input id="otk" type="password" bind:value={owntracksKey} autocomplete="new-password"
          placeholder={settings.owntracks_configured ? "••••••••" : t("settings.owntracksPlaceholder")}
          class="flex-1 font-mono" />
        <Button onclick={testOwntracks} disabled={testing}>{testing ? "…" : t("common.test")}</Button>
      </div>
      {#if testResult}
        <p class="mt-1 text-s {testResult.ok ? 'text-dida-ok' : 'text-dida-danger'}">
          {testResult.ok ? said(testResult) : t("settings.keyFails", { detail: said(testResult) })}
        </p>
      {/if}
      <p class="mt-1 text-s text-dida-text-faint">{t("settings.owntracksHint")}</p>
    </div>
    <div class="mt-4 flex items-center gap-3">
      <SaveButton size="small" dirty={!!owntracksKey.trim()} {saving} onclick={saveSettings} />
      {#if settings.owntracks_configured}
        <span class="ml-auto"><Button size="small" tone="danger" onclick={removeOwntracks}>{t("settings.removeKey")}</Button></span>
      {/if}
      {#if saveMsg}<span class="text-s text-dida-text-muted">{saveMsg}</span>{/if}
    </div>
  </div>
{/if}
