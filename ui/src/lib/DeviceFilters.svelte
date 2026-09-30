<script lang="ts">
  import { Button, Picks, formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { typeLabel, adapterLabel } from "$lib/capabilities";
  import { devices } from "$lib/store.svelte";

  let {
    q = $bindable(),
    room = $bindable(),
    type = $bindable(),
    adapter = $bindable(),
    onlyOn = $bindable(),
    groupBy = $bindable(),
    types,
    adapters,
    visible,
    total,
  }: {
    q: string;
    room: string;
    type: string;
    adapter: string;
    onlyOn: boolean;
    groupBy: string;
    types: string[];
    adapters: string[];
    visible: number;
    total: number;
  } = $props();

  const rooms = $derived(devices.sortedAreas);
  const active = $derived(
    !!q || room !== "all" || type !== "all" || adapter !== "all" || onlyOn,
  );

  const GROUPS: { key: string; labelKey: "filter.room" | "filter.type" | "filter.source" }[] = [
    { key: "room", labelKey: "filter.room" },
    { key: "type", labelKey: "filter.type" },
    { key: "adapter", labelKey: "filter.source" },
  ];

  function clear() {
    q = "";
    room = "all";
    type = "all";
    adapter = "all";
    onlyOn = false;
  }

</script>

<div class="mb-4 flex flex-col gap-2 rounded-lg border border-dida-border bg-dida-panel p-3">
  <div class="flex flex-wrap items-center gap-2">
    <input
      bind:value={q}
      placeholder={t("filter.search")}
      class="min-w-[10rem] flex-1"
    />

    <select bind:value={room} aria-label={t("filter.room")}>
      <option value="all">{t("filter.room")}: {t("filter.all")}</option>
      {#each rooms as a (a.id)}<option value={String(a.id)}>{devices.roomLabel(a)}</option>{/each}
      <option value="none">{t("room.unassigned")}</option>
    </select>

    <select bind:value={type} aria-label={t("filter.type")}>
      <option value="all">{t("filter.type")}: {t("filter.all")}</option>
      {#each types as ty (ty)}<option value={ty}>{typeLabel(ty)}</option>{/each}
    </select>

    {#if adapters.length > 1}
      <select bind:value={adapter} aria-label={t("filter.source")}>
        <option value="all">{t("filter.source")}: {t("filter.all")}</option>
        {#each adapters as ad (ad)}<option value={ad}>{adapterLabel(ad)}</option>{/each}
      </select>
    {/if}

    <Button selected={onlyOn} onclick={() => (onlyOn = !onlyOn)}>⚡ {t("filter.onlyOn")}</Button>
  </div>

  <div class="flex flex-wrap items-center gap-2 text-m">
    <span class="text-dida-text-faint">{t("filter.groupBy")}:</span>
    <div class="flex gap-1">
      <Picks picks={GROUPS.map((g) => ({ key: g.key, label: t(g.labelKey) }))} chosen={[groupBy]} onpick={(k) => (groupBy = k)} />
    </div>

    <span class="ml-auto text-dida-text-faint">
      {formatNumber(visible, { maximumFractionDigits: 1 })} / {formatNumber(total, { maximumFractionDigits: 1 })}
    </span>
    {#if active}
      <Button size="small" onclick={clear}>{t("filter.clear")}</Button>
    {/if}
  </div>
</div>
