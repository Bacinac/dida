<script lang="ts">
  // The COLLAPSED room label on the plan: one value — the room's primary reading
  // (temperature by default), the mean across its sensors, comfort-coloured. A tap
  // (handled by the parent) expands it to every reading of the room.
  import { capMeta, formatValue, stateColor } from "#lib/capabilities.js";
  import DeviceIcon from "#lib/DeviceIcon.svelte";
  import { primaryCap, type RoomCap } from "#lib/roomSensors.js";

  let { caps, heating = false }: { caps: RoomCap[]; heating?: boolean } = $props();
  const prim = $derived(primaryCap(caps));
  const unit = (cap: string): string => (cap === "temperature" ? "°" : (capMeta(cap).unit ?? ""));
</script>

{#if prim && prim.mean != null}
  <span class="inline-flex items-center gap-0.5 rounded-md bg-black/30 px-1.5 py-0.5 text-s font-semibold tabular-nums shadow-md backdrop-blur-sm {stateColor(prim.cap, prim.mean)}"
    >{#if heating}<DeviceIcon type="flame" class="size-3.5 text-orange-400" />{/if}{formatValue(prim.cap, prim.mean)}<span class="opacity-60">{unit(prim.cap)}</span></span>
{/if}
