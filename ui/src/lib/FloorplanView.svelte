<script lang="ts">
  // Read-only floor plan — the same markers, room labels and light glows as
  // /floorplan (shared derivation in $lib/floorplanItems), with zero
  // interactivity: no popovers, no editing, nothing tappable. Built for the
  // wall panel, usable anywhere a passive live plan is wanted.
  import { devices } from "$lib/store.svelte";
  import type { Floor } from "$lib/api";
  import { buildGlows, buildPlanItems, buildRoomLabels, isPlug, isSensorLabel, itemPosOn, planCandidates } from "$lib/floorplanItems";
  import FloorGlow from "$lib/FloorGlow.svelte";
  import FloorMarker from "$lib/FloorMarker.svelte";
  import FloorSensorLabel from "$lib/FloorSensorLabel.svelte";
  import RoomSensorLabel from "$lib/RoomSensorLabel.svelte";

  let { floor, markerScale = 1, class: klass = "" }: {
    floor: Floor;
    markerScale?: number;
    class?: string;
  } = $props();

  const fkey = $derived(floor.key);
  const floorImg = $derived(floor.img_path ? `/api/floorplan/${floor.key}?v=${encodeURIComponent(floor.img_path)}` : "");
  // Night dims the plan (helper:daynight, Ecowitt-solar driven) — same wash as /floorplan.
  const isNight = $derived(devices.byId["helper:daynight"]?.caps?.text?.value === "night");
  const areas = $derived(devices.areas);
  const areaById = $derived(new Map(areas.map((a) => [a.id, a])));
  const floorAreas = $derived(areas.filter((a) => a.fp_poly && a.fp_floor === fkey));
  const items = $derived(buildPlanItems(planCandidates()));
  const placedItems = $derived(items.map((it) => ({ it, p: itemPosOn(it, fkey) })).filter((x) => x.p));
  const roomLabels = $derived(buildRoomLabels(fkey).filter((r) => !r.off));
  const glows = $derived(buildGlows(placedItems, floorAreas, areaById, fkey));
</script>

<!-- isolate: keep the plan's marker/glow ladder private, so a z-30 marker can't paint
     over whatever chrome the embedding page puts above it (see /floorplan). -->
<div class="relative isolate overflow-hidden {klass}">
  {#if floorImg}
    <img src={floorImg} alt={floor.name} class="pointer-events-none absolute inset-0 h-full w-full object-contain" draggable="false" />
  {/if}
  {#if isNight}
    <div class="pointer-events-none absolute inset-0 z-[3] bg-[#0a1526]/55 transition-opacity duration-[1500ms]"></div>
  {/if}
  <FloorGlow {glows} />

  {#each placedItems as { it, p } (it.key)}
    {#if p}
      <!-- pointer-events-none: the labels' history buttons must be inert here -->
      <div style="left:{p.x}%; top:{p.y}%; transform: translate(-50%,-50%) scale(calc({it.scale * markerScale} * var(--fp-mark, 1)))"
        class="pointer-events-none absolute z-30">
        {#if (it.slot === "motion" || isSensorLabel(it)) && !isPlug(it)}
          <FloorSensorLabel members={it.members} show={it.labelShow ?? "both"} />
        {:else}
          <FloorMarker members={it.members} icon={it.icon} name={it.name} plug={isPlug(it)} tint={it.tint} />
        {/if}
      </div>
    {/if}
  {/each}

  {#each roomLabels as rl (rl.area.id)}
    <div style="left:{rl.x}%; top:{rl.y}%; transform: translate(-50%,-50%) scale(calc({markerScale} * var(--fp-mark, 1)))"
      class="pointer-events-none absolute z-[28]">
      <RoomSensorLabel caps={rl.caps} />
    </div>
  {/each}
</div>
