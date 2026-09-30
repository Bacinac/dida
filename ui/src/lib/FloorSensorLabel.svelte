<script lang="ts">
  // Read-only sensors on the plan render as a transparent value label
  // (e.g. "21.4° · 55%") instead of an icon marker — the reading IS the marker.
  // Short: value + unit only (no cap labels), every value shaded by its own state
  // band. Motion / occupancy sensors show a person icon ONLY while occupied
  // (invisible when empty). Each value is tappable → its history inline.
  // (Outdoor vs indoor weather sensors are split into separate markers upstream,
  // so one label is always one location.)
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import { capMeta, capRank, formatValue, hasPresenceCap, presenceOccupied, PLAN_HIDDEN_READINGS, stateColor } from "$lib/capabilities";
  import type { CapState, Device } from "$lib/store.svelte";

  // `show` scopes what this label renders: "readings" = the value label only (no
  // presence), "motion" = the bare presence dot only (no readings). A sensor that
  // carries both is drawn by TWO labels — one of each — placed independently, so the
  // scope is how each half ignores the other even when they share ONE entity.
  let { members, interactive = true, show = "both", onhistory }: {
    members: Device[];
    interactive?: boolean;
    show?: "readings" | "motion" | "both";
    onhistory?: (entityId: string, cap: string) => void;
  } = $props();

  interface Reading { entityId: string; cap: string; cs: CapState; }
  // Numeric read-out values (temperature, humidity, lux, …), most important
  // first. Battery / signal diagnostics stay off the plan (PLAN_HIDDEN_READINGS).
  const readings = $derived.by<Reading[]>(() => {
    if (show === "motion") return [];
    const out: Reading[] = [];
    for (const m of members) {
      for (const cap of Object.keys(m.caps)) {
        if (m.hiddenCaps.includes(cap) || PLAN_HIDDEN_READINGS.has(cap)) continue;
        if (capMeta(cap).control !== "sensor") continue;
        if (typeof m.caps[cap].value !== "number") continue;
        out.push({ entityId: m.entityId, cap, cs: m.caps[cap] });
      }
    }
    return out.sort((a, b) => capRank(a.cap) - capRank(b.cap));
  });

  // The motion/occupancy indicator (scoped out when this label is the value half).
  // Reads the CONSIDERED presence verdict (person_count ▸ occupancy ▸ motion) across
  // each member: a camera goes green only on a real PERSON (not any tracked object),
  // a mmWave board follows its debounced occupancy (not a stuck raw binary). hiddenCaps
  // stay excluded per member.
  const presenceCaps = $derived(members.map((m) =>
    Object.fromEntries(Object.entries(m.caps).filter(([c]) => !m.hiddenCaps.includes(c)))));
  const hasPresence = $derived(show !== "readings" && hasPresenceCap(presenceCaps));
  const occupied = $derived(presenceOccupied(presenceCaps));
  // The bare dot must stay grabbable in edit mode, so it also shows when not
  // interactive; the pip on a shared "both" label is purely the live signal (occupied).
  const showPerson = $derived(hasPresence && (occupied || !interactive));

  // Readings only (no person cell) — the person is the pip / bare dot, drawn apart.
  const rows = $derived.by<Reading[][]>(() => {
    if (readings.length <= 3) return [readings];
    const out: Reading[][] = [];
    for (let i = 0; i < readings.length; i += 2) out.push(readings.slice(i, i + 2));
    return out;
  });

  // Compact unit: temperature shows a bare degree sign, the rest their own unit.
  const unitOf = (r: Reading): string =>
    r.cap === "temperature" ? "°" : (r.cs.unit ?? capMeta(r.cap).unit ?? "");

</script>

{#snippet value(r: Reading)}
  <button
    type="button"
    disabled={!interactive}
    onpointerdown={(e) => interactive && e.stopPropagation()}
    onclick={(e) => { if (interactive) { e.stopPropagation(); onhistory?.(r.entityId, r.cap); } }}
    class="rounded transition-colors {stateColor(r.cap, r.cs.value as number)} {interactive ? 'hover:text-dida-accent' : 'pointer-events-none cursor-default'}"
  >{#if r.cap === "uv_index"}<span class="opacity-70">UV </span>{/if}{formatValue(r.cap, r.cs.value as number)}<span class="opacity-60">{unitOf(r)}</span></button>
{/snippet}

{#if readings.length}
  <!-- Value label in a frosted pill (readings need a backdrop to stay legible over the
       plan). Motion rides as a corner PIP that appears only while occupied. -->
  <div class="relative flex flex-col gap-y-0.5 rounded-md bg-black/30 px-1.5 py-0.5 text-s font-semibold tabular-nums text-white shadow-md backdrop-blur-sm">
    {#each rows as row, ri (ri)}
      <div class="flex items-center gap-x-1.5">
        {#each row as cell, i (cell.entityId + cell.cap)}
          {#if i > 0}<span class="text-white/40">·</span>{/if}
          {@render value(cell)}
        {/each}
      </div>
    {/each}
    {#if hasPresence && occupied}
      <span class="absolute -right-1.5 -top-1.5 grid size-4 place-items-center rounded-full bg-dida-ok text-white shadow shadow-dida-ok/60 ring-1 ring-dida-ok/60">
        <DeviceIcon type="presence" class="size-3" />
      </span>
    {/if}
  </div>
{:else if showPerson}
  <!-- Pure presence sensor: the SAME frosted pill as every icon marker (identical
       footprint, so a "reset to same size" really is uniform), tinted emerald while
       occupied. In view mode it only renders when occupied → invisible until motion. -->
  <span class="grid shrink-0 place-items-center rounded-full border px-1.5 py-1 shadow-md backdrop-blur {occupied ? 'border-dida-ok bg-dida-ok text-white shadow-dida-ok/40' : 'border-white/15 bg-black/30 text-white/70'}">
    <DeviceIcon type="presence" class="size-4 shrink-0" />
  </span>
{/if}
