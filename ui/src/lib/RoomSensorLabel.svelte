<script lang="ts">
  // The COLLAPSED room label on the plan: one value — the room's primary reading
  // (temperature by default), the mean across its sensors, comfort-coloured. A tap
  // (handled by the parent) expands it to every reading of the room.
  import { capMeta, formatValue, stateColor } from "$lib/capabilities";
  import { primaryCap, type RoomCap } from "$lib/roomSensors";

  let { caps }: { caps: RoomCap[] } = $props();
  const prim = $derived(primaryCap(caps));
  const unit = (cap: string): string => (cap === "temperature" ? "°" : (capMeta(cap).unit ?? ""));
</script>

{#if prim && prim.mean != null}
  <span class="rounded-md bg-black/30 px-1.5 py-0.5 text-s font-semibold tabular-nums shadow-md backdrop-blur-sm {stateColor(prim.cap, prim.mean)}"
    >{formatValue(prim.cap, prim.mean)}<span class="opacity-60">{unit(prim.cap)}</span></span>
{/if}
