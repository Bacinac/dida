<script lang="ts">
  // Settings chrome: a horizontal tab bar for the current group (settingsNav.ts).
  // The sidebar picks the group; these tabs pick the page within it. Tabs the user
  // can't see (admin-only / not in allowed_pages) are filtered out; a group with a
  // single visible tab shows no bar.
  import { page } from "$app/state";
  import { auth } from "$lib/auth.svelte";
  import { t } from "$lib/i18n";
  import { SETTINGS_GROUPS, type SettingsTab } from "$lib/settingsNav";

  let { children } = $props();

  const canShow = (tab: SettingsTab) =>
    tab.admin ? auth.isAdmin : tab.page ? auth.canSee(tab.page) : true;

  const path = $derived(page.url.pathname);
  const group = $derived(
    SETTINGS_GROUPS.find((g) =>
      g.tabs.some((tab) => path === tab.href || path.startsWith(tab.href + "/")),
    ),
  );
  const tabs = $derived((group?.tabs ?? []).filter(canShow));
  const isActive = (href: string) => path === href || path.startsWith(href + "/");
</script>

{#if tabs.length > 1}
  <nav class="mb-5 flex flex-wrap gap-x-1 border-b border-dida-border">
    {#each tabs as tab (tab.href)}
      <a
        href={tab.href}
        class="-mb-px border-b-2 px-3 py-2 text-m transition-colors {isActive(tab.href)
          ? 'border-dida-accent font-medium text-dida-accent'
          : 'border-transparent text-dida-text-muted hover:text-dida-text'}"
      >{t(tab.key)}</a>
    {/each}
  </nav>
{/if}

{@render children()}
