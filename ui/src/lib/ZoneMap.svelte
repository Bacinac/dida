<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import "leaflet/dist/leaflet.css";
  import { t } from "$lib/i18n";
  import type { Zone } from "$lib/api";

  type Person = { name: string; lat: number; lon: number; location: string; stale?: boolean };

  let {
    zones,
    people = [],
    focus = null,
    focusZoom = 15,
    onCreate,
    onMove,
  }: {
    zones: Zone[];
    people?: Person[];
    focus?: { lat: number; lon: number } | null; // pass a NEW object to fly the map there
    focusZoom?: number; // zoom level used when flying to `focus` (presence map wants tighter)
    // Both handlers are optional: omit them for a READ-ONLY viewer (no "add on
    // map" button, zone pins not draggable) — used by the presence map.
    onCreate?: (lat: number, lon: number) => void;
    onMove?: (id: number, lat: number, lon: number) => void;
  } = $props();

  let el: HTMLDivElement;
  const micro = (deg: number): number => Math.round(deg * 1e6) / 1e6;
  let addMode = $state(false);
  // Leaflet is loaded client-side only (it needs `window`); keep refs untyped.
  let L: typeof import("leaflet") | null = null;
  let map: import("leaflet").Map | null = null;
  let zoneLayer: import("leaflet").LayerGroup | null = null;
  let peopleLayer: import("leaflet").LayerGroup | null = null;
  let didFit = false;

  const homeIcon = () => L!.divIcon({ className: "", html: zonePin(true), iconSize: [16, 16], iconAnchor: [8, 8] });
  const zoneIcon = () => L!.divIcon({ className: "", html: zonePin(false), iconSize: [14, 14], iconAnchor: [7, 7] });
  const personIcon = (label: string, stale: boolean) =>
    L!.divIcon({ className: "", html: personPin(label, stale), iconSize: [26, 26], iconAnchor: [13, 13] });

  function zonePin(home: boolean): string {
    const c = home ? "#f59e0b" : "#38bdf8";
    return `<span style="display:block;width:${home ? 16 : 14}px;height:${home ? 16 : 14}px;border-radius:9999px;background:${c};border:2px solid #0b1020;box-shadow:0 0 0 1px ${c}"></span>`;
  }
  function personPin(label: string, stale: boolean): string {
    const bg = stale ? "#64748b" : "#10b981";
    const op = stale ? "opacity:.55;" : "";
    return `<span style="${op}display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:9999px;background:${bg};color:#06281d;border:2px solid #0b1020;font:600 11px system-ui;box-shadow:0 1px 4px rgba(0,0,0,.5)">${label}</span>`;
  }

  function drawZones() {
    if (!L || !map || !zoneLayer) return;
    zoneLayer.clearLayers();
    for (const z of zones) {
      const color = z.is_home ? "#f59e0b" : "#38bdf8";
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m, color, weight: 1, fillColor: color, fillOpacity: 0.12,
      }).addTo(zoneLayer);
      const m = L.marker([z.latitude, z.longitude], {
        icon: z.is_home ? homeIcon() : zoneIcon(), draggable: !!onMove, title: z.name,
      }).addTo(zoneLayer);
      m.bindTooltip(z.name, { direction: "top", offset: [0, -8] });
      if (onMove)
        m.on("dragend", () => {
          const ll = m.getLatLng();
          onMove(z.id, micro(ll.lat), micro(ll.lng));
        });
    }
    // Auto-fit to all zones only when there's no explicit focus — a focused map
    // (the presence popover) must stay on its point, not zoom out to every zone.
    if (!didFit && !focus && zones.length) {
      const b = L.latLngBounds(zones.map((z) => [z.latitude, z.longitude] as [number, number]));
      map.fitBounds(b.pad(0.3), { maxZoom: 13 });
      didFit = true;
    }
  }

  function drawPeople() {
    if (!L || !map || !peopleLayer) return;
    peopleLayer.clearLayers();
    for (const p of people) {
      if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon)) continue;
      const initials = p.name.slice(0, 2).toUpperCase();
      const m = L.marker([p.lat, p.lon], { icon: personIcon(initials, !!p.stale), title: p.name }).addTo(peopleLayer);
      m.bindTooltip(`${p.name} — ${p.location}${p.stale ? " ·" : ""}`, { direction: "top", offset: [0, -10] });
    }
  }

  let tilesBroken = $state(false);

  onMount(async () => {
    L = (await import("leaflet")).default;
    // Open ON the focus point (presence popover) when given — the reactive
    // flyTo below only handles LATER focus changes, and `map` isn't reactive so
    // a focus set before this async mount would otherwise never be applied.
    map = L.map(el, { zoomControl: true }).setView(
      focus ? [focus.lat, focus.lon] : [45.74, 16.02],
      focus ? focusZoom : 8,
    );
    if (focus) didFit = true; // an explicit focus wins over the fit-all-zones default
    const tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap",
    }).addTo(map);
    // Tiles come from openstreetmap.org, so the BROWSER fetches them — a blocker
    // extension or filtering resolver leaves an empty dark box that reads as a
    // broken page. Say what happened instead: the coordinate fields below work
    // regardless of whether a single tile ever arrives.
    tiles.on("tileerror", () => (tilesBroken = true));
    tiles.on("tileload", () => (tilesBroken = false));
    zoneLayer = L.layerGroup().addTo(map);
    peopleLayer = L.layerGroup().addTo(map);
    map.on("click", (e: import("leaflet").LeafletMouseEvent) => {
      if (!addMode || !onCreate) return;
      addMode = false;
      onCreate(micro(e.latlng.lat), micro(e.latlng.lng));
    });
    drawZones();
    drawPeople();
  });

  onDestroy(() => {
    map?.remove();
    map = null;
  });

  // Redraw when the data changes (table edits, live presence updates).
  $effect(() => {
    void zones;
    drawZones();
  });
  // Fly to a zone when the list selects one (focus is a fresh object each click).
  $effect(() => {
    if (focus && map) map.flyTo([focus.lat, focus.lon], focusZoom, { duration: 0.6 });
  });
  $effect(() => {
    void people;
    drawPeople();
  });
</script>

<div class="relative">
  <div bind:this={el} class="h-80 w-full overflow-hidden rounded-lg border border-dida-border {addMode ? 'cursor-crosshair' : ''}"></div>
  {#if onCreate}
    <button
      onclick={() => (addMode = !addMode)}
      class="absolute right-2 top-2 z-[400] rounded border px-2 py-1 text-s font-medium shadow {addMode
        ? 'border-dida-accent bg-dida-accent-strong text-dida-on-accent'
        : 'border-dida-border bg-dida-panel text-dida-text hover:border-dida-accent'}"
    >
      {addMode ? t("zones.clickToPlace") : t("zones.addOnMap")}
    </button>
  {/if}
  {#if tilesBroken}
    <p class="pointer-events-none absolute left-1/2 top-1/2 z-[401] max-w-[80%] -translate-x-1/2 -translate-y-1/2 rounded border border-dida-warn/40 bg-dida-panel/95 px-3 py-2 text-center text-s text-dida-warn">
      {t("zones.tilesBlocked")}
    </p>
  {/if}
</div>
