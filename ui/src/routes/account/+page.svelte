<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { geo } from "$lib/geolocation.svelte";
  import { push } from "$lib/push.svelte";
  import { Button, Notice, i18n, theme, toasts, type Locale, type Theme } from "$lib/kit";
  import { errMsg } from "$lib/errors";
  import { nativeBridge } from "$lib/native";
  import { t, type MessageKey } from "$lib/i18n";
  import { clock } from "$lib/dt";

  import { SECTION_TITLE_CLASS } from "$lib/ui";
  import { version } from "$lib/version.svelte";

  // The native app's own version (which APK is installed), read from the bridge —
  // distinct from the server version below. Only present inside the app.
  const bridge = nativeBridge();
  let appVersion = $state<string | null>(null);
  // Inside the native app, location is shared natively (OwnTracks background +
  // geofences), not the browser geolocation toggle — so that toggle is hidden
  // here to avoid reading as "off" while the app is actually reporting.
  const inApp = bridge !== null;

  // --- web push (this device) ---
  let testState = $state<"idle" | "sending" | "sent" | "err">("idle");
  async function sendTest() {
    testState = "sending";
    try {
      await push.test(t("push.testMessage"));
      testState = "sent";
    } catch {
      testState = "err";
    }
    setTimeout(() => (testState = "idle"), 4000);
  }

  let oldPw = $state("");
  let newPw = $state("");
  let newPw2 = $state("");
  let busy = $state(false);
  let msg = $state<{ kind: "ok" | "err"; text: string } | null>(null);

  onMount(async () => {
    if (bridge) {
      try {
        appVersion = await bridge.appVersion();
      } catch (e) {
        toasts.error(errMsg(e));
      }
    }
    await loadCarApps();
  });

  // --- the car app (DIDA Auto sideload) ---
  type CarMeta = { versionCode: number; versionName: string };
  let carAuto = $state<CarMeta | null>(null); // null = no artifact served → row hidden
  async function loadCarApps() {
    try {
      carAuto = await api.carAppMeta("auto");
    } catch {
      carAuto = null;
    }
  }

  async function submit(e: Event) {
    e.preventDefault();
    msg = null;
    if (newPw.length < 8) {
      msg = { kind: "err", text: t("account.pwTooShort") };
      return;
    }
    if (newPw !== newPw2) {
      msg = { kind: "err", text: t("account.pwMismatch") };
      return;
    }
    busy = true;
    try {
      await api.changePassword(oldPw, newPw);
      msg = { kind: "ok", text: t("account.changed") };
      oldPw = newPw = newPw2 = "";
    } catch (e) {
      msg = { kind: "err", text: e instanceof Error ? e.message : t("account.failed") };
    } finally {
      busy = false;
    }
  }

  // --- appearance (theme + language), persisted to the user's profile so the
  //     choice follows them across devices ---
  const THEME_OPTS: Theme[] = ["light", "dark", "system"]; // conventional order
  const LOCALES: Locale[] = ["hr", "en"];
  // Languages listed alphabetically, locale-aware (the house dropdown convention).
  const localeOpts = $derived(
    LOCALES.map((code) => ({ code, label: t(`language.${code}` as MessageKey) })).sort((a, b) =>
      a.label.localeCompare(b.label, i18n.locale),
    ),
  );
  function pickTheme(v: Theme) {
    theme.setTheme(v);
    auth.saveTheme(v);
  }
  function pickLocale(v: Locale) {
    i18n.set(v);
    auth.saveLocale(v);
  }

</script>

<svelte:head><title>{t("account.title")}</title></svelte:head>

<p class="mb-5 text-m text-dida-text-muted">{t("account.loggedAs")} <strong>{auth.user?.username}</strong> ({auth.user?.role ? t(`role.${auth.user.role}` as MessageKey) : ""}).</p>

<!-- Cards flow into columns (masonry) so the page fills the width instead of one
     tall single-column stack; break-inside-avoid keeps each card whole. -->
<div class="columns-1 gap-x-5 md:columns-2 2xl:columns-3">
<form onsubmit={submit} class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
  <h2 class="mb-3 {SECTION_TITLE_CLASS}">{t("account.changePw")}</h2>

  <label class="block text-s text-dida-text-muted" for="o">{t("account.currentPw")}</label>
  <input id="o" type="password" bind:value={oldPw} autocomplete="current-password" required class="mt-1 w-full" />

  <label class="mt-3 block text-s text-dida-text-muted" for="n">{t("account.newPw")}</label>
  <input id="n" type="password" bind:value={newPw} autocomplete="new-password" required class="mt-1 w-full" />

  <label class="mt-3 block text-s text-dida-text-muted" for="n2">{t("account.repeatPw")}</label>
  <input id="n2" type="password" bind:value={newPw2} autocomplete="new-password" required class="mt-1 w-full" />

  {#if msg}
    <Notice tone={msg.kind === "ok" ? "info" : "err"}>{msg.text}</Notice>
  {/if}

  <div class="mt-4 grid"><Button tone="primary" type="submit" disabled={busy || !oldPw || newPw.length < 8 || newPw !== newPw2}>
    {busy ? t("common.saving") : t("account.submit")}
  </Button></div>
</form>

<div class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
  <h2 class="mb-3 {SECTION_TITLE_CLASS}">{t("account.appearance")}</h2>

  <label class="block text-s text-dida-text-muted" for="pref-theme">{t("theme.label")}</label>
  <select id="pref-theme" class="mt-1 w-full" value={theme.theme} onchange={(e) => pickTheme(e.currentTarget.value as Theme)}>
    {#each THEME_OPTS as opt (opt)}
      <option value={opt}>{t(`theme.${opt}` as MessageKey)}</option>
    {/each}
  </select>

  <label class="mt-3 block text-s text-dida-text-muted" for="pref-lang">{t("language.label")}</label>
  <select id="pref-lang" class="mt-1 w-full" value={i18n.locale} onchange={(e) => pickLocale(e.currentTarget.value as Locale)}>
    {#each localeOpts as opt (opt.code)}
      <option value={opt.code}>{opt.label}</option>
    {/each}
  </select>
</div>

<div class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
  <h2 class="mb-1 {SECTION_TITLE_CLASS}">{t("presence.share.title")}</h2>
  {#if inApp}
    <p class="text-m text-dida-ok">{t("presence.share.byApp")}</p>
  {:else}
    <label class="flex items-center gap-2 text-m">
      <input
        type="checkbox"
        checked={geo.enabled}
        onchange={(e) => geo.setEnabled(e.currentTarget.checked)}
        class="accent-dida-accent"
      />
      {t("presence.share.toggle")}
    </label>
    <p class="mt-2 text-s text-dida-text-faint">
      {t("presence.share.statusLabel")}:
      <span class="{geo.status === 'active' ? 'text-dida-ok' : geo.status === 'denied' || geo.status === 'error' || geo.status === 'unsupported' ? 'text-dida-danger' : 'text-dida-text-muted'}">
        {t(`presence.share.${geo.status}` as MessageKey)}
      </span>
      {#if geo.zone}
        · {geo.zone === "away" ? t("presence.away") : geo.zone}
      {/if}
      {#if geo.lastAt}
        · {clock(geo.lastAt, true)}
      {/if}
    </p>
  {/if}
</div>

<div class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
  <h2 class="mb-1 {SECTION_TITLE_CLASS}">{t("push.title")}</h2>
  {#if inApp}
    <!-- Native app: notifications arrive over FCM, not the browser Push API (which
         the WebView doesn't support). Show that, not a dead "unsupported" toggle. -->
    <p class="text-m text-dida-ok">{t("push.byApp")}</p>
    <div class="mt-3"><Button size="small" disabled={testState === "sending"} onclick={sendTest}>
      {testState === "sending" ? t("common.saving") : t("push.test")}
    </Button></div>
    {#if testState === "sent"}
      <p class="mt-2 text-s text-dida-ok">{t("push.testSent")}</p>
    {:else if testState === "err"}
      <p class="mt-2 text-s text-dida-danger">{t("push.testErr")}</p>
    {/if}
  {:else}
    <label class="flex items-center gap-2 text-m">
      <input
        type="checkbox"
        checked={push.status === "active" || push.status === "pending"}
        disabled={push.busy || push.status === "insecure" || push.status === "unsupported"}
        onchange={(e) => (e.currentTarget.checked ? push.enable() : push.disable())}
        class="accent-dida-accent"
      />
      {t("push.toggle")}
    </label>
    <p class="mt-2 text-s text-dida-text-faint">
      {t("presence.share.statusLabel")}:
      <span class="{push.status === 'active' ? 'text-dida-ok' : push.status === 'foreign' ? 'text-dida-warn' : push.status === 'denied' || push.status === 'error' || push.status === 'insecure' || push.status === 'unsupported' ? 'text-dida-danger' : 'text-dida-text-muted'}">
        {t(`push.status.${push.status}` as MessageKey)}
      </span>
    </p>
    {#if push.status === "active"}
      <div class="mt-3"><Button size="small" disabled={testState === "sending"} onclick={sendTest}>
        {testState === "sending" ? t("common.saving") : t("push.test")}
      </Button></div>
      {#if testState === "sent"}
        <p class="mt-2 text-s text-dida-ok">{t("push.testSent")}</p>
      {:else if testState === "err"}
        <p class="mt-2 text-s text-dida-danger">{t("push.testErr")}</p>
      {/if}
    {/if}
  {/if}
</div>

{#if carAuto}
  <div class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
    <h2 class="mb-1 {SECTION_TITLE_CLASS}">{t("car.title")}</h2>
    <p class="mb-3 text-s text-dida-text-muted">{t("car.intro")}</p>
    {#if carAuto}
      <div>
        <p class="text-m font-medium">
          DIDA Auto <span class="text-s text-dida-text-faint">{carAuto.versionName}</span>
        </p>
        <p class="mb-2 text-s text-dida-text-muted">{t("car.autoHint")}</p>
        <a
          href={`/api/app/dida-auto.apk?v=${encodeURIComponent(carAuto.versionName)}`}
          class="inline-block rounded-lg border border-dida-border px-3 py-1.5 text-m hover:border-dida-accent"
          >{t("car.download")}</a
        >
      </div>
    {/if}
    <p class="mt-3 text-s text-dida-text-faint">{t("car.unknownSources")}</p>
  </div>
{/if}

<div class="mb-5 break-inside-avoid rounded-lg border border-dida-border bg-dida-panel p-4">
  <h2 class="mb-2 {SECTION_TITLE_CLASS}">{t("account.version")}</h2>
  <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-m">
    {#if appVersion}
      <dt class="text-dida-text-faint">{t("account.versionApp")}</dt>
      <dd class="font-mono">{appVersion}</dd>
    {/if}
    <dt class="text-dida-text-faint">{t("account.versionServer")}</dt>
    <dd class="font-mono">v{version.label || "…"}</dd>
  </dl>
  {#if inApp}
    <div class="mt-3"><Button tone="primary" onclick={() => bridge?.checkUpdate().catch((e) => toasts.error(errMsg(e)))}>
      {t("account.updateNow")}
    </Button></div>
  {/if}
</div>
</div>
