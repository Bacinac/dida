<script lang="ts">
  import { onMount } from "svelte";
  import { api, type RetentionClass, type RetentionOverride } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { adapterLabel, capLabel, CAP_GROUPS, capGroupIndex, classColor } from "$lib/capabilities";
  import { devices } from "$lib/store.svelte";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";
  import { Button, Card, PageActions, SaveButton, formatNumber, toasts } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { SECTION_TITLE_CLASS } from "$lib/ui";

  let classes = $state<RetentionClass[]>([]);
  let caps = $state<Record<string, string>>({});
  let overrides = $state<RetentionOverride[]>([]);
  let known = $state<string[]>([]);
  let snapshot = $state("");
  let saving = $state(false);
  let err = $state<string | null>(null);

  // new-override draft
  let ovEntity = $state("");
  let ovCap = $state("");
  let ovClass = $state("");

  const snap = () => JSON.stringify({ classes, caps, overrides });
  const dirty = $derived(snapshot !== "" && snap() !== snapshot);
  const classNames = $derived(classes.map((c) => c.name));
  const capList = $derived([...new Set([...known, ...Object.keys(caps)])].sort());
  const classLabel = (name: string) => classes.find((c) => c.name === name)?.label || name;

  // An override names a real entity and one of ITS capabilities — picked, not
  // typed: a hand-typed id that names nothing would sit there doing nothing.
  const ovItems = $derived(Object.values(devices.byId).map(pickerItem));
  const ovCapsOf = (id: string): string[] => Object.keys(devices.byId[id]?.caps ?? {});
  const ovName = (id: string): string => {
    const d = devices.byId[id];
    if (!d) return id;
    const twin = Object.values(devices.byId).some(
      (o) => o.entityId !== id && o.name === d.name && o.areaId === d.areaId);
    return `${d.name}${twin ? ` (${adapterLabel(d.adapter)})` : ""} · ${devices.areaName(d.areaId)}`;
  };

  // Capability→class grid, grouped by domain so ~50 dropdowns read as ~8 sections;
  // any cap the backend reports that falls outside every group collects in "other".
  const capGroups = $derived.by(() => {
    const buckets: string[][] = CAP_GROUPS.map(() => []);
    const other: string[] = [];
    for (const c of capList) {
      const i = capGroupIndex(c);
      (i < CAP_GROUPS.length ? buckets[i] : other).push(c);
    }
    const out = CAP_GROUPS.map((g, i) => ({ key: g.key, caps: buckets[i] })).filter((g) => g.caps.length > 0);
    if (other.length) out.push({ key: "capgroup.other" as MessageKey, caps: other });
    return out;
  });

  // Assign a whole domain to one class in a single move (most caps in a group share
  // a class — turns 50 dropdowns into ~8 group decisions).
  function bulkAssign(groupCaps: string[], cls: string) {
    if (!cls) return;
    const next = { ...caps };
    for (const c of groupCaps) next[c] = cls;
    caps = next;
  }

  // Accordion (mirrors Settings → Adapters): at most one group open, all collapsed
  // by default so the page reads as ~8 headers until you drill into one.
  let openGroup = $state<string | null>(null);
  // Distinct classes assigned within a group → summary dots on its collapsed header.
  const groupClasses = (groupCaps: string[]) =>
    [...new Set(groupCaps.map((c) => caps[c] ?? "default"))]
      .sort((a, b) => classNames.indexOf(a) - classNames.indexOf(b));

  async function load() {
    err = null;
    try {
      const p = await api.getRetention();
      classes = p.classes;
      caps = p.capabilities;
      overrides = p.overrides;
      known = p.known_capabilities ?? [];
      ovClass = classes.find((c) => c.name !== "default")?.name ?? "default";
      snapshot = snap();
    } catch (e) {
      err = errMsg(e);
    }
  }

  async function save() {
    saving = true;
    try {
      await api.saveRetention({ classes, capabilities: caps, overrides });
      snapshot = snap();
      toasts.success(t("settings.saved"));
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      saving = false;
    }
  }

  function addOverride() {
    if (!ovEntity.trim() || !ovCap.trim()) return;
    overrides = [
      ...overrides,
      { entity_id: ovEntity.trim(), capability: ovCap.trim(), class_name: ovClass },
    ];
    ovEntity = "";
    ovCap = "";
  }
  const removeOverride = (i: number) => (overrides = overrides.filter((_, j) => j !== i));

  // Big day-counts read as years so the matrix is legible.
  function yrs(d: number): string {
    if (d === 0) return t("retention.drop");
    if (d >= 365) return t("retention.years", { n: formatNumber(d / 365, { maximumFractionDigits: 1 }) });
    return "";
  }

  onMount(load);
</script>

<svelte:head><title>{t("nav.retention")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else if err}
  <p class="text-m text-dida-danger">{err}</p>
{:else}
  <PageActions>
    <SaveButton {dirty} {saving} onclick={save} />
  </PageActions>

  <!-- Classes × tiers matrix -->
  <h2 class="mb-2 {SECTION_TITLE_CLASS}">{t("retention.classes")}</h2>
  <Card>
    <div class="overflow-x-auto">
      <table class="w-full min-w-[36rem] text-m">
        <thead>
          <tr class="text-left text-s text-dida-text-muted">
            <th class="py-1 pr-3 font-medium">{t("retention.class")}</th>
            <th class="px-2 py-1 font-medium">{t("retention.tierRaw")}</th>
            <th class="px-2 py-1 font-medium">{t("retention.tier1h")}</th>
            <th class="px-2 py-1 font-medium">{t("retention.tier1d")}</th>
          </tr>
        </thead>
        <tbody>
          {#each classes as cls (cls.name)}
            <tr class="border-t border-dida-border/50">
              <td class="py-1.5 pr-3">
                <div class="flex items-center gap-2">
                  <span class="size-2.5 shrink-0 rounded-full" style="background:{classColor(cls.name)}"></span>
                  <input class="w-40" bind:value={cls.label} />
                  <span class="font-mono text-xs text-dida-text-faint">{cls.name}</span>
                </div>
              </td>
              <td class="px-2 py-1.5 align-top">
                <input type="number" min="0" class="w-24" bind:value={cls.days_raw} />
                <div class="mt-0.5 text-xs text-dida-text-faint">{yrs(cls.days_raw)}</div>
              </td>
              <td class="px-2 py-1.5 align-top">
                <input type="number" min="0" class="w-24" bind:value={cls.days_1h} />
                <div class="mt-0.5 text-xs text-dida-text-faint">{yrs(cls.days_1h)}</div>
              </td>
              <td class="px-2 py-1.5 align-top">
                <input type="number" min="0" class="w-24" bind:value={cls.days_1d} />
                <div class="mt-0.5 text-xs text-dida-text-faint">{yrs(cls.days_1d)}</div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="mt-2 text-xs text-dida-text-faint">{t("retention.daysNote")}</p>
    <p class="mt-1 text-xs text-dida-text-faint">{t("retention.textNote")}</p>
  </Card>

  <!-- Capability → class -->
  <h2 class="mb-2 mt-6 {SECTION_TITLE_CLASS}">{t("retention.capsTitle")}</h2>
  <Card>
    {#if capList.length === 0}
      <p class="text-s text-dida-text-faint">—</p>
    {:else}
      <div class="flex flex-col gap-2">
        {#each capGroups as grp (grp.key)}
          {@const open = openGroup === grp.key}
          <div class="overflow-hidden rounded-lg border border-dida-border bg-dida-panel">
            <div class="flex w-full items-center justify-between gap-2 p-3">
              <button
                type="button"
                onclick={() => (openGroup = open ? null : grp.key)}
                class="flex min-w-0 flex-1 items-center gap-2 text-left hover:text-dida-accent"
              >
                <span class="truncate text-m font-semibold">{t(grp.key)}</span>
                <span class="shrink-0 rounded bg-dida-panel-2 px-1.5 text-xs text-dida-text-faint">{grp.caps.length}</span>
                <span class="flex shrink-0 items-center gap-1">
                  {#each groupClasses(grp.caps) as cn (cn)}
                    <span class="size-2 rounded-full" style="background:{classColor(cn)}" title={classLabel(cn)}></span>
                  {/each}
                </span>
              </button>
              <button
                type="button"
                onclick={() => (openGroup = open ? null : grp.key)}
                aria-label={t(grp.key)}
                class="shrink-0 text-s text-dida-text-faint hover:text-dida-accent"
              >{open ? "▾" : "▸"}</button>
            </div>
            {#if open}
              <div class="px-3 pb-3">
                <div class="mb-3 flex justify-end">
                  <select
                    class="w-40 text-dida-text-muted"
                    value=""
                    title={t("retention.setAll")}
                    onchange={(e) => { bulkAssign(grp.caps, e.currentTarget.value); e.currentTarget.value = ""; }}
                  >
                    <option value="" disabled>{t("retention.setAll")}</option>
                    {#each classNames as n (n)}<option value={n}>{classLabel(n)}</option>{/each}
                  </select>
                </div>
                <div class="grid gap-2" style="grid-template-columns:repeat(auto-fill,minmax(13rem,1fr))">
                  {#each grp.caps as cap (cap)}
                    <label
                      class="flex flex-col gap-1 rounded border border-l-2 border-dida-border/60 bg-dida-panel-2 p-2"
                      style="border-left-color:{classColor(caps[cap] ?? 'default')}"
                    >
                      <span class="truncate text-m" title={cap}>
                        {capLabel(cap)}<span class="ml-1.5 font-mono text-xs text-dida-text-faint">{cap}</span>
                      </span>
                      <select
                        class="w-full"
                        value={caps[cap] ?? "default"}
                        onchange={(e) => (caps = { ...caps, [cap]: e.currentTarget.value })}
                      >
                        {#each classNames as n (n)}<option value={n}>{classLabel(n)}</option>{/each}
                      </select>
                    </label>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </Card>

  <!-- Per-device overrides -->
  <h2 class="mb-2 mt-6 {SECTION_TITLE_CLASS}">{t("retention.overridesTitle")}</h2>
  <Card>
    {#if overrides.length}
      <div class="mb-3 flex flex-col gap-1.5">
        {#each overrides as o, i (o.entity_id + o.capability)}
          <div class="flex items-center gap-2 rounded border border-dida-border/60 px-2 py-1.5 text-m"
            title={`${o.entity_id} / ${o.capability}`}>
            <span>{ovName(o.entity_id)}</span>
            <span class="text-dida-text-faint">·</span>
            <span>{capLabel(o.capability)}</span>
            <span class="text-dida-text-faint">→</span>
            <span class="text-dida-accent">{classLabel(o.class_name)}</span>
            <span class="ml-auto"><Button size="small" label={t("common.remove")} title={t("common.remove")} onclick={() => removeOverride(i)}>✕</Button></span>
          </div>
        {/each}
      </div>
    {:else}
      <p class="mb-3 text-s text-dida-text-faint">{t("retention.noOverrides")}</p>
    {/if}
    <div class="flex flex-wrap items-end gap-2">
      <div class="w-72 max-w-full">
        <EntityPicker bind:value={ovEntity} items={ovItems}
          onchange={(id) => (ovCap = ovCapsOf(id)[0] ?? "")} />
      </div>
      <select class="w-44" bind:value={ovCap}>
        {#each ovCapsOf(ovEntity) as cp (cp)}<option value={cp}>{capLabel(cp)}</option>{/each}
      </select>
      <select class="w-full" bind:value={ovClass}>
        {#each classNames as n (n)}<option value={n}>{classLabel(n)}</option>{/each}
      </select>
      <Button disabled={!ovEntity.trim() || !ovCap.trim()} onclick={addOverride}>{t("retention.add")}</Button>
    </div>
  </Card>
{/if}
