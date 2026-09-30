<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import { devices } from "$lib/store.svelte";
  import { api } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { Button, toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { persons, presenceStatus, presenceToneClass, type Person, type LastZone } from "$lib/presence";
  import PresenceMap from "$lib/PresenceMap.svelte";

  // Outer spacing/width is the caller's call — the Devices page keeps its bottom
  // margin (default), the floorplan aligns the track under the plan (max-w-2xl).
  let { class: cls = "mb-4" }: { class?: string } = $props();

  // A tapped chip opens a focused map at that person's last-known location.
  let mapPerson = $state<Person | null>(null);

  // Last real zone per person (from history) — the live store only holds the
  // latest value, which the network adapters overwrite with "away".
  let lastZones = $state<Record<string, LastZone>>({});
  async function loadLastZones() {
    try { lastZones = await api.presenceLastLocations(); } catch { /* keep prior */ }
  }

  // Who the household curated OUT of the panel (a GLOBAL display choice; only an
  // admin edits it). Fetched once — it changes rarely and only from this UI.
  let hidden = $state<Set<string>>(new Set());
  let editing = $state(false);
  async function loadHidden() {
    try { hidden = new Set((await api.presenceHidden()).hidden); } catch { /* keep prior */ }
  }
  async function toggleHidden(entityId: string) {
    const next = new Set(hidden);
    if (next.has(entityId)) next.delete(entityId); else next.add(entityId);
    const prev = hidden;
    hidden = next; // optimistic — persist immediately, revert on failure
    try {
      await api.setPresenceHidden([...next]);
    } catch {
      hidden = prev;
      toasts.error(t("presence.saveError"));
    }
  }

  // Wall-clock tick so staleness re-evaluates as time passes (not just on events).
  let now = $state(Date.now());
  let timer: ReturnType<typeof setInterval>;
  let zoneTimer: ReturnType<typeof setInterval>;
  onMount(() => {
    loadLastZones();
    loadHidden();
    timer = setInterval(() => (now = Date.now()), 30_000);
    zoneTimer = setInterval(loadLastZones, 60_000); // a new zone arriving rarely — refresh gently
  });
  onDestroy(() => { clearInterval(timer); clearInterval(zoneTimer); });

  const allPeople = $derived(persons(devices.list, now));
  const people = $derived(allPeople.filter((p) => !hidden.has(p.entityId)));
  // Edit mode shows EVERYONE (so an admin can unhide); otherwise the curated set.
  const shown = $derived(editing ? allPeople : people);
</script>

{#if allPeople.length}
  <section class={cls}>
    <div class="mb-2 flex items-center justify-between gap-2">
      <h2 class="{SUBSECTION_TITLE_CLASS}">{t("presence.title")}</h2>
      {#if auth.isAdmin}
        <Button size="small" selected={editing} onclick={() => (editing = !editing)}>{editing ? t("presence.editDone") : t("presence.edit")}</Button>
      {/if}
    </div>
    <!-- All people in ONE row (grid-flow-col + auto-cols-fr → equal columns that
         never overflow) at a FIXED height: two lines, name over place, each a
         single truncating line — nothing here may wrap, or a narrow phone column
         grows the whole track taller. The clock time of a last-known fix is the
         first thing to go: it only appears from `sm` up, where there's room; on a
         phone the tooltip and the map carry it. No initials avatar — initials
         collide (Bea/Ben → "BE"). The place is the last real one (never a
         placeless "away"): green when they're there now, amber when that's only
         the last-known, muted "Vani" when no zone was ever seen — so the colour
         still reads even when the name of the place is clipped. In edit mode a
         chip toggles the person's visibility instead of opening the map. -->
    {#if shown.length}
      <div class="grid grid-flow-col auto-cols-fr gap-1.5">
        {#each shown as p (p.entityId)}
          {@const st = presenceStatus(p, lastZones[p.entityId], now)}
          {@const isHidden = hidden.has(p.entityId)}
          <button type="button"
            onclick={() => (editing ? toggleHidden(p.entityId) : (mapPerson = p))}
            class="relative flex min-w-0 flex-col gap-0.5 rounded-lg border bg-dida-panel px-1.5 py-1 leading-tight
                   {editing ? 'border-dashed border-dida-border' : 'border-dida-border hover:border-dida-accent'}
                   {editing && isHidden ? 'opacity-40' : ''}"
            title={editing ? t(isHidden ? "presence.hiddenTag" : "presence.shown") : `${p.name} · ${st.text}`}
          >
            {#if editing}
              <span class="absolute right-0.5 top-0.5 rounded px-1 text-2xs font-semibold uppercase tracking-wide
                           {isHidden ? 'bg-dida-border/40 text-dida-text-faint' : 'bg-dida-accent/15 text-dida-accent'}"
              >{t(isHidden ? "presence.hiddenTag" : "presence.shown")}</span>
            {/if}
            <span class="w-full truncate rounded bg-dida-accent/15 px-1 text-center text-s font-semibold text-dida-accent sm:text-m">
              {p.name}
            </span>
            <span class="w-full truncate text-center text-xs sm:text-s {presenceToneClass[st.tone]}" style={st.colorStyle}>
              {st.place}{#if st.at}<span class="hidden text-dida-text-faint sm:inline"> {st.at}</span>{/if}
            </span>
          </button>
        {/each}
      </div>
    {:else}
      <p class="text-s text-dida-text-faint">{t("presence.allHidden")}</p>
    {/if}
  </section>
{/if}

{#if mapPerson}
  <PresenceMap person={mapPerson} last={lastZones[mapPerson.entityId]} {now} onClose={() => (mapPerson = null)} />
{/if}
