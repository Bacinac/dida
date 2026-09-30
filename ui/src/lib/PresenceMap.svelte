<script lang="ts">
  import { onMount } from "svelte";
  import { Dialog } from "$lib/kit";
  import { api, type Zone } from "$lib/api";
  import { t } from "$lib/i18n";
  import { presenceStatus, presenceToneClass, type Person, type LastZone } from "$lib/presence";
  import ZoneMap from "$lib/ZoneMap.svelte";

  // A focused, read-only map for ONE person: their point + the zone circles for
  // context. Live GPS coords win; without them we fall back to the last-known
  // zone's centre so the map still shows WHERE (e.g. Island House) even when the
  // phone stopped reporting a precise point. Only when neither exists do we say so.
  let { person, last, now, onClose }: { person: Person; last?: LastZone; now: number; onClose: () => void } = $props();

  let zones = $state<Zone[]>([]);
  onMount(async () => {
    try { zones = await api.listZones(); } catch { zones = []; }
  });

  const st = $derived(presenceStatus(person, last, now));
  const hasCoords = $derived(person.lat != null && person.lon != null);
  const lastZoneCenter = $derived(last ? zones.find((z) => z.name === last.zone) : undefined);
  // The point to show: live coords (fresh or stale as-is) else the last zone's
  // centre (always styled stale — it's a zone, not a precise fix).
  const point = $derived(
    hasCoords
      ? { lat: person.lat as number, lon: person.lon as number, stale: person.stale }
      : lastZoneCenter
        ? { lat: lastZoneCenter.latitude, lon: lastZoneCenter.longitude, stale: true }
        : null,
  );
  const people = $derived(
    point ? [{ name: person.name, lat: point.lat, lon: point.lon, location: st.text, stale: point.stale }] : [],
  );
  // A fresh object → ZoneMap flies to it on open.
  const focus = $derived(point ? { lat: point.lat, lon: point.lon } : null);
</script>

<Dialog size="narrow" label={person.name} onclose={onClose}>
  <div>
    <div class="mb-3 flex items-start gap-3">
      <div class="min-w-0 flex-1 leading-tight">
        <p class="truncate text-m font-semibold">{person.name}</p>
        <p class="truncate text-s {presenceToneClass[st.tone]}" style={st.colorStyle}>{st.text}</p>
      </div>
    </div>

    {#if point}
      <ZoneMap {zones} {people} {focus} focusZoom={17} />
    {:else}
      <p class="rounded-lg border border-dashed border-dida-border p-4 text-center text-m text-dida-text-muted">
        {t("presence.noCoords")}
      </p>
    {/if}
  </div>
</Dialog>
