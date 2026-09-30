<script lang="ts">
  // Automations chrome: the rules and their calendar/beat schedules, as two tabs.
  import { page } from "$app/state";
  import { t } from "$lib/i18n";

  let { children } = $props();

  const TABS = [
    { href: "/automations", key: "nav.automations" as const, exact: true },
    { href: "/automations/schedules", key: "nav.schedules" as const, exact: false },
  ];
  const path = $derived(page.url.pathname);
  const isActive = (tab: (typeof TABS)[number]) =>
    tab.exact ? path === tab.href : path === tab.href || path.startsWith(tab.href + "/");
</script>

<nav class="mb-5 flex flex-wrap gap-x-1 border-b border-dida-border">
  {#each TABS as tab (tab.href)}
    <a
      href={tab.href}
      class="-mb-px border-b-2 px-3 py-2 text-m transition-colors {isActive(tab)
        ? 'border-dida-accent font-medium text-dida-accent'
        : 'border-transparent text-dida-text-muted hover:text-dida-text'}"
    >{t(tab.key)}</a>
  {/each}
</nav>

{@render children()}
