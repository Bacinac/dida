<script lang="ts">
  import "../app.css";
  import { onMount } from "svelte";
  import { page } from "$app/state";
  import { goto } from "$app/navigation";
  import { Button, Dialogs, Frame, NewVersion, Toasts, i18n, theme, type Section } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { devices } from "$lib/store.svelte";
  import { replay } from "$lib/replay.svelte";
  import { translations } from "$lib/translations.svelte";
  import { geo } from "$lib/geolocation.svelte";
  import { push } from "$lib/push.svelte";
  import { ui } from "$lib/shell.svelte";
  import { CONSUMER_PAGES, PHONE_TAB_ORDER } from "$lib/pages";
  import { SETTINGS_GROUPS } from "$lib/settingsNav";
  import Brand from "$lib/Brand.svelte";
  import TopBar from "$lib/TopBar.svelte";
  import AssistantPanel from "$lib/AssistantPanel.svelte";
  import { version } from "$lib/version.svelte";
  import { help } from "$lib/help";
  import { nativeBridge } from "$lib/native";
  import { errMsg } from "$lib/errors";
  import { toasts } from "$lib/kit";

  let { children } = $props();

  onMount(() => {
    theme.sync();
    i18n.init();
    auth.load();
    version.watch();
  });

  // Inside the native app, make its OWN strings (toasts, update dialog, permission
  // prompts) follow the DIDA UI language, not the device locale — runs on load and
  // on every language switch.
  $effect(() => {
    const tag = i18n.locale;
    void nativeBridge()?.setLocale(tag).catch((e) => toasts.error(errMsg(e)));
  });

  // Route guard, driven by auth state.
  $effect(() => {
    if (!auth.checked) return;
    const path = page.url.pathname;
    // /panel (wall display) and /onboard (phone setup) authenticate themselves
    // from a token in the URL, so never bounce them to /login.
    if (!auth.user && path !== "/login" && path !== "/panel" && path !== "/onboard") { goto("/login"); return; }
    if (auth.user && path === "/login") { goto("/"); return; }
    if (!auth.user || auth.isAdmin) return;
    // Keep regular users out of admin-only routes, and out of pages an admin has
    // hidden from them (allowed_pages). Bounce to the first page they can see.
    if (ADMIN_ROUTES.some((r) => path === r || path.startsWith(r + "/"))) { goto(firstAllowedHref()); return; }
    const pg = pageForPath(path);
    if (pg && !auth.canSee(pg)) goto(firstAllowedHref());
  });

  // Load display-name translations for the active language, and re-apply them to
  // stored entity names on a language switch (device headers retranslate
  // reactively via tr(); the stored entity `name` needs a re-load). Kept apart
  // from the WS lifecycle below so switching language never reconnects the socket.
  $effect(() => {
    if (!auth.user) return;
    void i18n.locale; // re-run on a language switch
    void (async () => {
      await translations.load();
      await devices.load();
    })();
  });

  $effect(() => {
    if (auth.user) void nativeBridge()?.syncIdentity(String(auth.user.id), window.location.origin).catch((e) => toasts.error(errMsg(e)));
  });

  // Run the live store only while authenticated; tear it down on logout.
  $effect(() => {
    if (auth.user) {
      devices.load();
      devices.start();
      geo.start(); // reports this device's location iff the user opted in
      void push.init(); // register the SW + reflect an existing push opt-in
      return () => {
        devices.stop();
        void replay.close();
        geo.stop();
      };
    }
  });

  // Bound so the drawer's header can clear the conversation — the panel owns the
  // transcript, the chrome owns the buttons that act on it.
  let assistantPanel = $state<AssistantPanel | null>(null);
  // A question queued before the drawer existed (explain-this-page) is sent once
  // the panel has mounted.
  $effect(() => {
    const q = ui.pendingQuestion;
    if (q && assistantPanel && auth.user && auth.canSee("assistant")) {
      ui.pendingQuestion = null;
      assistantPanel.ask(q);
    }
  });
  // Sidebar navigation, grouped (mirrors BABA's monitoring / archive / settings).
  // `admin` items are config-only and hidden from regular users. `page` items
  // are further gated per-user by `allowed_pages` (auth.canSee) — admin-managed
  // page visibility. An item with neither flag is always visible to anyone
  // logged in (e.g. the Settings container, media sub-pages).
  type NavItem = { href: string; key: MessageKey; admin?: boolean; page?: string; children?: NavItem[]; active?: boolean };
  // Consumer pages come from the single source (CONSUMER_PAGES); the sidebar only
  // decides their GROUPING and interleaves the admin-only items. `navPage(key)`
  // pulls one consumer page's href + label from that source so nothing is re-typed.
  const byKey = new Map(CONSUMER_PAGES.map((p) => [p.key, p]));
  const navPage = (key: string): NavItem => {
    const p = byKey.get(key);
    if (!p) throw new Error(`unknown consumer page: ${key}`);
    if (!p.href) throw new Error(`consumer page ${key} is an overlay — it has no route to link to`);
    return { href: p.href, key: p.nav, page: p.key };
  };
  // Settings sidebar entries = the groups (settingsNav.ts); a group's PAGES are
  // tabs on its page, not sidebar links. A group shows when any of its tabs is
  // visible, and lands on its first visible tab.
  const settingsChildren = $derived<NavItem[]>(
    SETTINGS_GROUPS.flatMap((g) => {
      const vis = g.tabs.filter((tab) =>
        tab.admin ? auth.isAdmin : tab.page ? auth.canSee(tab.page) : true,
      );
      if (!vis.length) return [];
      // The group's sidebar link is active on ANY of its tabs, not just the landing.
      const p = page.url.pathname;
      const active = g.tabs.some((tab) => p === tab.href || p.startsWith(tab.href + "/"));
      return [{ href: vis[0].href, key: g.key, active }];
    }),
  );
  const navGroups = $derived<NavItem[][]>([
    // Devices moved to Settings; Floor plan is home. MediaHub folds
    // Players/Library/Tidal/Radio into one surface — no sub-nav.
    // The assistant is absent by design: it is a drawer opened from the header on
    // every page (TopBar), so it needs no nav slot on either breakpoint.
    [navPage("entry"), navPage("floorplan"), navPage("cameras"), navPage("media"), navPage("heating")],
    [
      { href: "/automations", key: "nav.automations", admin: true },
      navPage("history"),
    ],
    [{ href: "/settings", key: "nav.settings", children: settingsChildren }],
  ]);
  // Admin-only routes: /automations plus every Settings tab flagged `admin` in
  // settingsNav (the single source). Consumer settings tabs (devices/adapters) are
  // gated per-user by allowed_pages via pageForPath, not here. Deriving this closes
  // the gap where an admin-only settings page (backup, scenes, users…) was reachable
  // by direct URL just because it wasn't in a hand-maintained list.
  const ADMIN_ROUTES = [
    "/automations",
    ...SETTINGS_GROUPS.flatMap((g) => g.tabs.filter((tab) => tab.admin).map((tab) => tab.href)),
  ];

  // Route-guard helpers, both derived from the single source in canonical order.
  // Map a pathname to the page key gating it, or null if it isn't page-gated
  // (admin-only routes, /account, /about… are governed elsewhere).
  function pageForPath(path: string): string | null {
    for (const p of CONSUMER_PAGES) {
      // An overlay page has no route, so no path can map to it.
      if (!p.href) continue;
      if (p.href !== "/" && (path === p.href || path.startsWith(p.href + "/"))) return p.key;
    }
    return null;
  }
  // First page a restricted user may see, in canonical priority order — the
  // bounce target when they hit a hidden or admin route. Overlay pages are skipped:
  // there is nowhere to send someone whose only permitted surface is a drawer.
  function firstAllowedHref(): string {
    return CONSUMER_PAGES.find((p) => p.href && auth.canSee(p.key))?.href ?? "/account";
  }

  // A nav item is visible when it clears both gates it declares.
  const canShow = (i: NavItem) => (!i.admin || auth.isAdmin) && (!i.page || auth.canSee(i.page));
  const visibleGroups = $derived(
    navGroups
      .map((g) =>
        g
          .filter(canShow)
          .map((i) => (i.children ? { ...i, children: i.children.filter(canShow) } : i))
          // Drop a parent whose every child was filtered away (e.g. Settings
          // when a non-admin can't see Adapters either).
          .filter((i) => !i.children || i.children.length > 0),
      )
      .filter((g) => g.length > 0),
  );

  const pathname = $derived(page.url.pathname);

  // What the frame draws: the visible groups with their words, and each consumer
  // page's icon for the phone's bottom bar. The bar takes the highest-priority
  // home pages (PHONE_TAB_ORDER) that survived into the visible groups; the
  // frame sends the rest to its sheet.
  const iconOf = (href: string) => CONSUMER_PAGES.find((p) => p.href === href)?.icon;
  const toSection = (i: NavItem): Section => ({
    href: i.href,
    label: t(i.key),
    icon: iconOf(i.href),
    active: i.active,
    children: i.children?.map(toSection),
  });
  const sections = $derived(visibleGroups.map((g) => g.map(toSection)));
  const tabs = PHONE_TAB_ORDER.map((k) => byKey.get(k)?.href).filter((h): h is string => !!h);

  async function logout() {
    await auth.logout();
    goto("/login", { replaceState: true });
  }

  // The wall panel (/panel), phone onboarding (/onboard) and /login render bare —
  // no sidebar/header chrome.
  const showChrome = $derived(
    auth.checked && !!auth.user &&
      pathname !== "/login" && pathname !== "/panel" && pathname !== "/onboard",
  );
</script>

{#if !auth.checked}
  <div class="grid h-full place-items-center text-dida-text-faint">…</div>
{:else if !showChrome}
  {@render children()}
{:else}
  <Frame
    {pathname}
    {sections}
    {tabs}
    account={{
      name: auth.user?.username ?? "",
      role: auth.user?.role ? t(`role.${auth.user.role}` as MessageKey) : undefined,
      href: "/account",
      onlogout: logout,
    }}
    {help}
    version={version.label}
    focus={ui.focusMode}
  >
    {#snippet brand()}<Brand />{/snippet}
    {#snippet bar()}<TopBar />{/snippet}
    {@render children()}
  </Frame>

  <!-- Assistant drawer: over the current page, never instead of it. Gated by the
       same `assistant` page key the nav entry used to carry. -->
  {#if ui.assistantOpen && auth.canSee("assistant")}
    <button
      type="button"
      onclick={() => (ui.assistantOpen = false)}
      aria-label={t("common.back")}
      class="fixed inset-0 z-40 bg-black/40"
    ></button>
    <aside
      class="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-dida-border bg-dida-bg p-4 shadow-2xl"
    >
      <div class="mb-2 flex items-center justify-between gap-2">
        <h2 class="font-semibold">{t("nav.assistant")}</h2>
        <div class="flex items-center gap-1">
          <Button size="small"
            onclick={() => assistantPanel?.clearChat()}
            title={t("assistant.clear")}
          >{t("assistant.clear")}</Button>
          <Button size="small"
            onclick={() => (ui.assistantOpen = false)}
            label={t("common.back")}
          >✕</Button>
        </div>
      </div>
      <div class="min-h-0 flex-1">
        <AssistantPanel bind:this={assistantPanel} />
      </div>
    </aside>
  {/if}
{/if}

<NewVersion watch={version} />
<Toasts />
<Dialogs />
