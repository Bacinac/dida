<script lang="ts">
  // What DIDA says in the frame's top bar on every page: the live system signals
  // (API health, bus connection) and the assistant. Help and who is signed in are
  // the frame's own.
  //
  // Theme and language are NOT here. They are set-once preferences that Account
  // already owns and persists to the profile, and the theme's "system" mode covers
  // the one case for flipping it often — so a permanent copy in the chrome was
  // duplication that had to be kept in sync, on every screen, for a yearly action.
  import { onMount } from "svelte";
  import { devices } from "$lib/store.svelte";
  import { api } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { ui } from "$lib/shell.svelte";
  import { t } from "$lib/i18n";
  import { Tag, type TagTone } from "$lib/kit";

  const conn = $derived(devices.conn);
  const connLabel = $derived(
    conn === "live" ? t("conn.live") : conn === "connecting" ? t("conn.connecting") : t("conn.offline"),
  );

  // API health — polled here so the signal is visible app-wide (this was the old
  // Status page's job; that page is gone, its signals live in the header now).
  let apiOk = $state<boolean | null>(null);
  async function pingApi() {
    try { apiOk = (await api.health()).ok === true; } catch { apiOk = false; }
  }
  onMount(() => {
    pingApi();
    const timer = setInterval(pingApi, 5000);
    return () => clearInterval(timer);
  });

  // Signals are colour-coded badges — the colour alone tells the status; the exact
  // word (healthy / live / connecting…) lives in the tooltip.
  const apiTone: TagTone = $derived(apiOk === null ? "quiet" : apiOk ? "ok" : "err");
  const apiLabel = $derived(apiOk === null ? "…" : apiOk ? t("system.healthy") : t("system.unavailable"));
  const busTone: TagTone = $derived(conn === "live" ? "ok" : conn === "connecting" ? "warn" : "err");
</script>

<!-- Connection/health signals are ops info — admin only; a regular user (family)
     doesn't need them (a real outage shows as a stale UI anyway). -->
{#if auth.isAdmin}
  <Tag tone={apiTone} title="{t('system.api')}: {apiLabel}">{t("system.api")}</Tag>
  <Tag tone={busTone} title="{t('system.bus')}: {connLabel}">{t("system.busShort")}</Tag>
{/if}
{#if auth.canSee("assistant")}
  <!-- The assistant opens OVER the page rather than replacing it, so it lives
       here on every surface instead of costing a nav slot. On the LEFT: a primary,
       always-available action (identity/UserMenu stays on the right). -->
  <button
    type="button"
    onclick={() => ui.toggleAssistant()}
    aria-label={t("nav.assistant")}
    title={t("nav.assistant")}
    aria-pressed={ui.assistantOpen}
    class="rounded p-1.5 text-dida-text-muted hover:bg-dida-panel-2 hover:text-dida-text"
    class:text-dida-accent={ui.assistantOpen}
  >
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" class="size-5" aria-hidden="true">
      <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.6-.8L3 21l1.8-5.9A8.5 8.5 0 1 1 21 11.5z"/>
    </svg>
  </button>
{/if}
