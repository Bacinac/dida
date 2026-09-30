<script lang="ts">
  // Family-onboarding landing page. Reached by scanning a user's setup QR, which
  // is `…/api/auth/link?k=<token>&next=/onboard`: the API sets the session cookie
  // and redirects here, so we arrive already signed in. Rendered chrome-free (see
  // the root layout).
  //
  // Three flavours of the same walk-through:
  //  * inside the Android companion app (DIDA-App UA / window.DidaApp bridge):
  //    the native location setup — permissions walkthrough via the bridge, state
  //    re-read on every `dida-native-status` event the app dispatches;
  //  * Android browser: download/open the companion app (APK sideload; the
  //    "open" handoff mints a fresh one-time login link because the QR's token
  //    was consumed by THIS browser session), PWA + OwnTracks as fallbacks;
  //  * iPhone: PWA + OwnTracks one-tap import (no companion app on iOS).
  import { onMount } from "svelte";
  import { Button } from "$lib/kit";
  import { goto } from "$app/navigation";
  import { auth } from "$lib/auth.svelte";
  import { api } from "$lib/api";
  import { t } from "$lib/i18n";
  import Brand from "$lib/Brand.svelte";

  let ready = $state(false);
  let importUrl = $state<string | null>(null); // owntracks:///config?inline=…
  let locReachable = $state(false);
  let apkVersion = $state<string | null>(null); // served-APK version, null = no artifact
  let openBusy = $state(false);

  // OwnTracks App Store link — iOS background location (no native app there).
  const APPSTORE = "https://apps.apple.com/app/owntracks/id692424691";

  const isAndroid = typeof navigator !== "undefined" && /android/i.test(navigator.userAgent);

  // Native bridge the companion app injects; present ⇒ we run inside the app.
  // scanQr is optional: older installed builds predate the in-app scanner.
  type Bridge = {
    status(): string;
    startLocationSetup(): void;
    appVersion(): string;
    scanQr?: () => void;
  };
  const bridge: Bridge | null =
    typeof window !== "undefined" ? ((window as any).DidaApp ?? null) : null;

  type NativeStatus = {
    version: string;
    provisioned: boolean;
    fineLocation: boolean;
    backgroundLocation: boolean;
    batteryExempt: boolean;
    zones: number;
  };
  let native = $state<NativeStatus | null>(null);

  function readNative() {
    if (!bridge) return;
    try {
      native = JSON.parse(bridge.status());
    } catch {
      native = null;
    }
  }

  onMount(() => {
    const setup = async () => {
      await auth.load(); // the redirect just set the cookie; populate auth.user
      if (auth.user) {
        try {
          const o = await api.myOwntracks();
          importUrl = o.inline_url || null;
          locReachable = o.reachable;
        } catch {
          /* location sharing is optional — the page still works without it */
        }
        if (isAndroid && !bridge) {
          try {
            apkVersion = (await api.apkMeta()).versionName;
          } catch {
            apkVersion = null; // no artifact served — hide the section, loudly logged server-side
          }
        }
      }
      readNative();
      ready = true;
    };
    setup();
    window.addEventListener("dida-native-status", readNative);
    return () => window.removeEventListener("dida-native-status", readNative);
  });

  /** THE one setup button — deterministic by construction: an intent:// forces
   * Android into the app when it is installed (regardless of App-Link
   * verification state), and its browser fallback is the APK download when it
   * is not. Installed → opens signed in (fresh login link) straight onto the
   * permission walkthrough; not installed → the APK downloads, the person
   * installs it, comes back and taps the SAME button again. */
  async function setupApp() {
    openBusy = true;
    try {
      const { url } = await api.myAppLink();
      const u = new URL(url);
      const fallback = apkVersion
        ? `${window.location.origin}/api/app/dida.apk?v=${encodeURIComponent(apkVersion)}`
        : url;
      window.location.href =
        `intent://${u.host}${u.pathname}${u.search}` +
        `#Intent;scheme=https;package=biz.boskovic.dida;` +
        `S.browser_fallback_url=${encodeURIComponent(fallback)};end`;
    } catch {
      /* stay on the page; the person can retry */
    } finally {
      openBusy = false;
    }
  }
</script>

<svelte:head><title>{t("onboard.title")}</title></svelte:head>

<div class="min-h-dvh bg-dida-bg text-dida-text">
  <div class="mx-auto flex max-w-md flex-col gap-4 px-5 py-8">
    <div class="flex justify-center"><Brand size="md" /></div>

    {#if !ready}
      <p class="text-center text-m text-dida-text-faint">…</p>
    {:else if !auth.user}
      <div class="rounded-xl border border-dida-border bg-dida-panel p-5 text-center">
        <p class="mb-3 text-dida-text-muted">{t("onboard.needQr")}</p>
        {#if bridge?.scanQr}
          <!-- In the app: however it was opened (installer's "Open", the icon),
               ONE button recovers the flow — scan the same setup QR right here. -->
          <div class="grid"><Button tone="primary" onclick={() => bridge?.scanQr?.()}>{t("onboard.scanQr")}</Button></div>
          <div class="h-2"></div>
        {/if}
        <!-- Manual escape hatch: inside the companion app there is no address bar,
             so a signed-out /onboard must not be a dead end — login loops back here. -->
        <div class="grid"><Button onclick={() => goto("/login?next=/onboard")}>
          {t("login.title")}
        </Button></div>
      </div>
    {:else}
      <h1 class="text-center text-xl font-semibold">{t("onboard.hello", { name: auth.user.username })}</h1>
      <p class="text-center text-m text-dida-text-muted">{t("onboard.signedIn")}</p>

      {#if bridge}
        <!-- Inside the companion app: the native location walkthrough. -->
        <section class="rounded-xl border border-dida-border bg-dida-panel p-4">
          <p class="mb-1 font-medium">{t("onboard.nativeTitle")}</p>
          {#if native?.provisioned && native.backgroundLocation}
            <p class="mb-2 text-m text-dida-ok">{t("onboard.nativeActive")}</p>
          {:else}
            <p class="mb-3 text-m text-dida-text-muted">{t("onboard.nativeHint")}</p>
          {/if}
          {#if native}
            <ul class="mb-3 space-y-1 text-m text-dida-text-muted">
              <li>{native.backgroundLocation ? "✓" : "○"} {t("onboard.stepBackground")}</li>
              <li>{native.batteryExempt ? "✓" : "○"} {t("onboard.stepBattery")}</li>
              {#if native.provisioned}
                <li>✓ {t("onboard.stepZones", { n: String(native.zones) })}</li>
              {/if}
            </ul>
          {/if}
          {#if !(native?.provisioned && native.backgroundLocation)}
            <div class="grid"><Button tone="primary" onclick={() => bridge.startLocationSetup()}>{t("onboard.nativeStart")}</Button></div>
          {/if}
          <!-- Which build is actually installed — kills the "is this the old
               APK?" question over the phone in one glance. -->
          {#if native}
            <p class="mt-2 text-right text-s text-dida-text-faint">DIDA {native.version}</p>
          {/if}
        </section>

        <section class="rounded-xl border border-dida-border bg-dida-panel p-4">
          <p class="mb-1 font-medium">{t("onboard.openTitle")}</p>
          <div class="grid"><Button tone="primary" onclick={() => goto("/")}>{t("onboard.enter")}</Button></div>
        </section>
      {:else if isAndroid}
        <!-- Android browser: ONE guided button, nothing else. Not installed →
             downloads the APK; installed → opens the app signed in, straight
             onto the permission walkthrough. -->
        <section class="rounded-xl border border-dida-border bg-dida-panel p-4">
          <p class="mb-3 text-m text-dida-text-muted">{t("onboard.stepIntro")}</p>
          <div class="grid"><Button tone="primary" disabled={openBusy} onclick={setupApp}>{t("onboard.stepBtn")}</Button></div>
          <ol class="mt-3 list-decimal space-y-1 pl-5 text-s text-dida-text-muted">
            <li>{t("onboard.step1")}</li>
            <li>{t("onboard.step2")}</li>
            <li>{t("onboard.step3")}</li>
          </ol>
          {#if !apkVersion}
            <p class="mt-2 text-s text-dida-danger">{t("onboard.noApk")}</p>
          {/if}
        </section>
      {:else}
        <!-- iPhone / desktop: the web IS the app there; OwnTracks covers
             background location on iOS. -->
        <section class="rounded-xl border border-dida-border bg-dida-panel p-4">
          <p class="mb-1 font-medium">{t("onboard.openTitle")}</p>
          <p class="mb-3 text-m text-dida-text-muted">{t("onboard.pwaHint")}</p>
          <div class="grid"><Button tone="primary" onclick={() => goto("/")}>{t("onboard.enter")}</Button></div>
        </section>

        <section class="rounded-xl border border-dida-border bg-dida-panel p-4">
          <p class="mb-1 font-medium">{t("onboard.locTitle")}</p>
          <p class="mb-3 text-m text-dida-text-muted">{t("onboard.locHint")}</p>
          {#if importUrl && locReachable}
            <a href={APPSTORE} target="_blank" rel="noopener"
              class="mb-2 block rounded-lg border border-dida-border px-3 py-2 text-center text-m text-dida-text-muted hover:bg-dida-panel-2">OwnTracks (App Store)</a>
            <div class="grid"><Button onclick={() => { if (importUrl) window.location.href = importUrl; }}>
              {t("onboard.locImport")}
            </Button></div>
            <p class="mt-2 text-s text-dida-text-faint">{t("onboard.locImportHint")}</p>
          {:else}
            <p class="text-m text-dida-text-faint">{t("onboard.locUnavailable")}</p>
          {/if}
        </section>
      {/if}
    {/if}
  </div>
</div>
