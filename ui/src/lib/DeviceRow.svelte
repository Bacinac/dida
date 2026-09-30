<script lang="ts">
  // A device folded to one line on the Devices page: what it is, its one glanceable
  // value (the same one its floor-plan marker shows) and, for a single switch, the
  // switch itself. The rest is one tap away, in the full card.
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import CapabilityControl from "$lib/CapabilityControl.svelte";
  import { glance, hasPresenceCap, presenceOccupied, type DeviceType } from "$lib/capabilities";
  import { Tag } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { clock } from "$lib/dt";
  import type { CapState, Device } from "$lib/store.svelte";

  let { members, name, type, onopen }: {
    members: Device[]; name: string; type: DeviceType; onopen: () => void;
  } = $props();

  const shown = $derived(members.map((m) =>
    Object.fromEntries(Object.entries(m.caps).filter(([c]) => !m.hiddenCaps.includes(c))) as Record<string, CapState>));
  const caps = $derived(Object.assign({}, ...shown) as Record<string, CapState>);
  const switches = $derived(members.filter((m, i) => "on_off" in shown[i]));
  const lit = $derived(switches.filter((m) => m.caps["on_off"]?.value === true).length);
  const value = $derived(glance(caps));
  const hasPresence = $derived(hasPresenceCap(shown));
  const occupied = $derived(presenceOccupied(shown));
  const unreachable = $derived(members.some((m) => !m.reachable));
  const downSince = $derived(Math.max(0, ...members.map((m) => (m.reachable ? 0 : m.reachableSince))));
  const heldSince = $derived(Math.max(0, ...members.map((m) => m.manualSince)));
</script>

<div class="flex min-h-12 items-center gap-2 rounded-lg border bg-dida-panel px-3 py-1
            {unreachable ? 'border-dida-warn/40' : 'border-dida-border hover:border-dida-accent'}">
  <button type="button" aria-expanded="false" onclick={onopen} class="flex min-w-0 flex-1 items-center gap-2 text-left">
    <span class="text-xl leading-none text-dida-text-muted">›</span>
    <DeviceIcon {type} class="size-4 shrink-0 {lit ? 'text-dida-accent' : 'text-dida-text-muted'}" />
    <span class="line-clamp-2 min-w-0 flex-1 break-words font-semibold leading-tight">{name}</span>
    {#if heldSince}
      <Tag tone="busy" title={t("card.manualSince", { t: clock(heldSince) })}>{t("card.manual")}</Tag>
    {/if}
    {#if unreachable}
      <Tag tone="warn" title={downSince ? t("card.unreachableSince", { t: clock(downSince) }) : t("card.unreachable")}>
        {t("card.unreachable")}
      </Tag>
    {/if}
    {#if hasPresence}
      <span class="size-2.5 shrink-0 rounded-full {occupied ? 'bg-dida-ok' : 'bg-dida-text-faint'}"
        title={t(occupied ? "cap.occupancy.on" : "cap.occupancy.off")}></span>
    {/if}
    {#if value}<span class="shrink-0 text-m tabular-nums text-dida-text-muted">{value}</span>{/if}
    {#if switches.length > 1}<span class="shrink-0 text-s tabular-nums text-dida-text-faint">{lit}/{switches.length}</span>{/if}
  </button>
  {#if switches.length === 1}
    <div class="shrink-0">
      <CapabilityControl entityId={switches[0].entityId} capability="on_off" cs={switches[0].caps["on_off"]} compact />
    </div>
  {/if}
</div>
