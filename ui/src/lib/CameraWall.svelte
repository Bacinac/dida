<script lang="ts">
  // Live situational-awareness wall — one cumulative grid of every camera across
  // every location (BABA at home + each Frigate site), each tile badged with its
  // `site`. NOT a second NVR: DIDA shows "live + now" (glance & act); BABA/Frigate
  // own the archive (recordings, identities), reached via the per-tile links.
  //
  // Video is pulled from DIDA's OWN proxy (/api/camera/<id>/…), never straight from
  // the source — so it rides DIDA's tunnel, is gated by DIDA login, and needs no
  // camera-subnet exposure. Grid tiles poll a cheap snapshot; the solo view opens
  // one live stream.
  //
  // Layout: a responsive grid of 16:9 cells. Every camera occupies a whole number
  // of cells (colSpan × rowSpan), so the frame shows COMPLETE (object-contain — no
  // cropping) and tiles still pack into tidy rows (grid-auto-flow: dense). An admin
  // resizes any tile by width/height in edit mode; the arrangement (order + spans)
  // is shared and persisted.
  import { onMount, onDestroy } from "svelte";
  import { devices } from "$lib/store.svelte";
  import { tr } from "$lib/translations.svelte";
  import { auth } from "$lib/auth.svelte";
  import { api, type CameraEvent } from "$lib/api";
  import { Button, Tag, toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { sceneLit } from "$lib/capabilities";
  import { errMsg } from "$lib/errors";
  import CameraSnap from "$lib/CameraSnap.svelte";
  import LiveVideo from "$lib/LiveVideo.svelte";

  // The sister NVR's address is deployment-specific — hardcoding one owner's
  // hostname shipped it to every install, and put a live link to a private
  // system on the public demo. Empty (the default) simply hides the link.
  const BABA_URL = (import.meta.env.VITE_BABA_URL as string | undefined) ?? "";

  interface Zone { name: string; on: boolean }
  interface Scene { label: string; state: string }
  interface Person { name: string; face: boolean }  // face = recognised by face (else body)
  interface NamedObject { name: string; kind: string }  // kind = "pet" | "vehicle"
  interface Cam {
    id: string; name: string; stream: string; site: string;
    motion: boolean; count: number; klass: string;
    zones: Zone[]; scenes: Scene[]; people: Person[]; objects: NamedObject[]; hasEvents: boolean;
  }

  // One shared poll tick — cache-busts snapshots AND (referenced below) expires
  // stale detections so a days-old "vehicle"/motion doesn't linger on a tile.
  let tick = $state(0);
  const FRESH_MS = 180_000;
  const cameras = $derived.by<Cam[]>(() => {
    void tick; // re-run periodically so freshness re-evaluates on its own
    const now = Date.now();
    const fresh = (at: number | undefined) => at != null && now - at < FRESH_MS;
    const list = devices.list;
    return list
      .filter((d) => typeof d.caps["camera"]?.value === "string")
      .map((d) => {
        let stream = "", site = "", hasEvents = false;
        try {
          const desc = JSON.parse(d.caps["camera"]!.value as string);
          stream = desc?.stream ?? ""; site = desc?.site ?? "";
          hasEvents = typeof desc?.events === "string" && desc.events.length > 0;
        } catch { /* bad descriptor */ }
        const zones: Zone[] = list
          .filter((z) => z.entityId.startsWith(`${d.entityId}:zone:`))
          .map((z) => ({ name: z.name, on: z.caps["occupancy"]?.value === true }));
        const scenes: Scene[] = list
          .filter((s) => s.entityId.startsWith(`${d.entityId}:scene:`))
          .map((s) => ({
            // Just the name — a scene is named after its region alone, exactly like a zone
            // (the camera is the device they group under). Nothing to parse, nothing to
            // fall back to: the adapter never invents a name, so there is no bad one to fix
            // up here. Deriving it from the entity_id is what printed a raw UUID.
            label: s.name,
            state: String(s.caps["scene_state"]?.value ?? ""),
          }))
          .filter((s) => s.state);
        // Recognised people present now: BABA names only the present (a rare map), and
        // the adapter mirrors a departure as "absent" — so present = value ∈ face/body.
        const people: Person[] = list
          .filter((p) => p.entityId.startsWith(`${d.entityId}:person:`))
          .map((p) => ({ name: p.name, src: String(p.caps["identity_presence"]?.value ?? "absent") }))
          .filter((p) => p.src === "face" || p.src === "body")
          .map((p) => ({ name: p.name, face: p.src === "face" }));
        // Recognised pets/vehicles present now — the non-person twin of `people`
        // (BABA's separate object_identities map). Present = value ∈ pet/vehicle.
        const objects: NamedObject[] = list
          .filter((p) => p.entityId.startsWith(`${d.entityId}:object:`))
          .map((p) => ({ name: p.name, kind: String(p.caps["object_presence"]?.value ?? "absent") }))
          // Pets only. A named vehicle belongs to the PLACE it stands in — the tile
          // says the place is taken, the floor plan's marker says whose car it is.
          .filter((o) => o.kind === "pet");
        const mo = d.caps["motion"], pc = d.caps["person_count"], oc = d.caps["object_class"];
        return {
          id: d.entityId, name: d.name, stream, site,
          motion: mo?.value === true && fresh(mo.updatedAt),
          count: typeof pc?.value === "number" ? (pc.value as number) : 0,
          klass: (typeof oc?.value === "string" && oc.value !== "none" && fresh(oc.updatedAt)) ? (oc.value as string) : "",
          zones, scenes, people, objects, hasEvents,
        };
      })
      .filter((c) => c.stream)
      .sort((a, b) => a.name.localeCompare(b.name, "hr"));
  });

  // Shared, admin-set arrangement: tile order (drag-drop) + per-tile grid span.
  type Span = { c: number; r: number };
  const DEFAULT_SPAN: Span = { c: 1, r: 1 };
  let order = $state<string[]>([]);
  let spans = $state<Record<string, Span>>({});
  const spanOf = (id: string): Span => spans[id] ?? DEFAULT_SPAN;
  async function loadLayout() {
    try {
      const l = await api.cameraLayout();
      order = l.order;
      spans = l.spans ?? {};
    } catch { /* keep defaults */ }
  }
  async function persist(prevOrder: string[], prevSpans: Record<string, Span>) {
    try {
      await api.setCameraLayout(order, spans);
    } catch (e) {
      order = prevOrder; spans = prevSpans; // roll back so the tiles don't lie
      toasts.error(errMsg(e));
    }
  }
  const ordered = $derived.by<Cam[]>(() => {
    const idx = new Map(order.map((id, i) => [id, i]));
    return [...cameras].sort((a, b) =>
      ((idx.get(a.id) ?? Infinity) - (idx.get(b.id) ?? Infinity)) || a.name.localeCompare(b.name, "hr"));
  });

  // Layout edit mode: tiles become draggable AND show width/height steppers.
  let editMode = $state(false);
  let dragId = $state<string | null>(null);
  async function onDrop(targetId: string) {
    if (!dragId || dragId === targetId) return;
    const ids = ordered.map((c) => c.id);
    const from = ids.indexOf(dragId), to = ids.indexOf(targetId);
    if (from < 0 || to < 0) { dragId = null; return; }
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    const prevO = order, prevS = spans;
    order = ids;                     // optimistic
    dragId = null;
    await persist(prevO, prevS);
  }
  // Resize a tile within the grid (clamped so it always tiles cleanly). Width is
  // capped to the current column count; height to a few rows.
  function resize(id: string, dc: number, dr: number) {
    const cur = spanOf(id);
    const c = Math.max(1, Math.min(cols, cur.c + dc));
    const r = Math.max(1, Math.min(4, cur.r + dr));
    if (c === cur.c && r === cur.r) return;
    const prevO = order, prevS = spans;
    spans = { ...spans, [id]: { c, r } };
    void persist(prevO, prevS);
  }

  // A camera is "active" (attention-worthy) when there's motion or a body in view, or a
  // boundary zone/scene is live. Drives a highlighted border. Deliberately NOT sceneLit:
  // an open gate wants attention, a parking spot that is merely occupied does not.
  function isActive(c: Cam): boolean {
    return c.motion || c.count > 0 || c.zones.some((z) => z.on) || c.scenes.some((s) => /open|otvor/i.test(s.state));
  }
  const trClass = (c: string) => {
    const key = `objclass.${c.trim()}`;
    const tr = t(key as Parameters<typeof t>[0]);
    return tr !== key ? tr : c.trim();
  };

  // Snapshot poll (tick declared above): the interval drives the cache-bust.
  let timer: ReturnType<typeof setInterval> | null = null;

  // ── Responsive 16:9 grid. `cols` cells across (by width); a 1×1 tile is one
  //    16:9 cell, so a 16:9 camera fits with no bars and no crop; odd-aspect
  //    cameras (a tall doorbell) letterbox until an admin resizes them. ──
  let gridW = $state(0);
  const GAP = 8;
  const cols = $derived(gridW >= 1280 ? 4 : gridW >= 768 ? 3 : gridW >= 480 ? 2 : 1);
  const cellW = $derived(cols > 0 ? (gridW - GAP * (cols - 1)) / cols : 0);
  const rowH = $derived(Math.max(80, Math.round(cellW * 9 / 16)));

  // Wall / kiosk mode: fullscreen, chrome-less, fit-every-tile-to-viewport grid —
  // for a big screen or the Nest Hub panel (ignores spans; packs squarely).
  let wall = $state(false);
  let wallEl = $state<HTMLElement | null>(null);
  const kioskCols = $derived(Math.max(1, Math.ceil(Math.sqrt(Math.max(1, cameras.length)))));
  function toggleWall() {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void wallEl?.requestFullscreen?.();
  }
  function onFsChange() { wall = !!document.fullscreenElement; }

  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowRight" && evCam) { evIdx = Math.min(evIdx + 1, evList.length - 1); return; }
    if (e.key === "ArrowLeft" && evCam) { evIdx = Math.max(evIdx - 1, 0); return; }
    if (e.key !== "Escape") return;
    if (evCam) closeEvents();
    else if (solo) solo = null;
  }
  onMount(() => {
    timer = setInterval(() => { if (!document.hidden && !solo && !evCam) tick += 1; }, 2500);
    document.addEventListener("fullscreenchange", onFsChange);
    window.addEventListener("keydown", onKey);
    void loadLayout();
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
    document.removeEventListener("fullscreenchange", onFsChange);
    window.removeEventListener("keydown", onKey);
  });

  // Key the proxy by entity_id (identity is the entity_id, never the display name).
  // Tiles ask for a tile-sized frame: at full resolution a remote site's uplink
  // goes entirely on snapshots the grid paints a quarter size, leaving nothing
  // for the recorded clip the viewer actually opened.
  const TILE_W = 640;
  const snap = (id: string) => `/api/camera/${encodeURIComponent(id)}/snapshot?w=${TILE_W}&t=${tick}`;

  let solo = $state<Cam | null>(null);

  // ── recorded-event viewer: tap a detection badge → the archive's evidence frames.
  let evCam = $state<Cam | null>(null);
  let evList = $state<CameraEvent[]>([]);
  let evIdx = $state(0);
  let evLoading = $state(false);
  const evCur = $derived<CameraEvent | null>(evList[evIdx] ?? null);
  let evPlay = $state(false); // current event shown as its recorded clip (video), not the still
  let evErr = $state<"" | "failed" | "empty">("");  // clip failed / archive has nothing — never a dead 0:00 player
  // BABA events carry `thumb` (possibly "" when the track has no crop — many zone
  // events don't); Frigate events don't have the key at all. An empty BABA thumb
  // must NOT fall through to the Frigate snapshot route (its id shape 400s) — it
  // gets a transparent pixel and the ▶ clip button carries the evidence.
  const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const evImg = (cam: Cam, e: CameraEvent, thumb = false): string =>
    e.thumb != null
      ? (e.thumb ? `/api/camera/${encodeURIComponent(cam.id)}/thumb/${encodeURIComponent(e.thumb)}` : BLANK)
      : `/api/camera/${encodeURIComponent(cam.id)}/event/${encodeURIComponent(e.id)}/snapshot${thumb ? "?thumb=1" : ""}`;
  const evClip = (cam: Cam, e: CameraEvent): string =>
    `/api/camera/${encodeURIComponent(cam.id)}/clip?start=${e.clip_start}&end=${e.clip_end}`;
  const evHasClip = (e: CameraEvent | null): boolean =>
    !!e && !!e.has_clip && typeof e.clip_start === "number" && typeof e.clip_end === "number";
  function evTime(sec: number): string {
    return dateTime(sec * 1000, true);
  }
  // Selecting an event (filmstrip tap, arrows, initial load) IS the play gesture:
  // a visit with a clip starts playing immediately — the still is only for visits
  // whose recording didn't survive (retention).
  $effect(() => { void evIdx; evPlay = evHasClip(evList[evIdx] ?? null); evErr = ""; });
  async function openEvents(c: Cam) {
    evCam = c; evList = []; evIdx = 0; evPlay = false; evLoading = true;
    const label = c.count > 0 ? "person" : "";
    try {
      // Coalesce: a 200 whose body isn't the expected shape would leave evList
      // undefined, and the very next line of the template reads .length — the
      // render throws and the dialog is stuck on its spinner with no error.
      evList = (await api.cameraEvents(c.id, label)).events ?? [];
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      evLoading = false;
    }
  }
  const closeEvents = () => { evCam = null; evList = []; evIdx = 0; evPlay = false; };
</script>

<!-- Identity badges only. The raw COUNTERS (person_count 👤 + object_class) were
     deliberately dropped: a bare "2 people" / "vehicle" tally is noise next to the
     signals that matter — WHO/WHAT is recognised (below) and the scene states
     (parking occupancy, gate) rendered on the tile. `count`/`klass` stay computed
     (they drive the events filter), just not shown. -->
{#snippet detBadges(c: Cam)}
  <!-- Named people, coloured by recognition confidence to match BABA's own badge:
       green = confirmed by face, amber = re-ID by body (a provisional match). -->
  {#each c.people as p (p.name)}
    <Tag onpicture tone={p.face ? "ok" : "warn"}>✓ {p.name}</Tag>
  {/each}
  <!-- Named pets/vehicles (body vs enrolled reference). Uncoloured so they never
       read as people; icon by kind. -->
  {#each c.objects as o (o.name)}
    <Tag onpicture>{o.kind === 'pet' ? '🐾' : '🚗'} {o.name}</Tag>
  {/each}
{/snippet}

<div bind:this={wallEl} class={wall ? "flex h-dvh flex-col bg-black p-2" : ""}>
  <div class="mb-2 flex items-center justify-end gap-2">
    {#if auth.isAdmin && !wall}
      <Button size="small" selected={editMode} onclick={() => (editMode = !editMode)}>{editMode ? t("cameras.layoutDone") : t("cameras.editLayout")}</Button>
    {/if}
    <Button size="small" onclick={toggleWall}>{wall ? t("cameras.exitWall") : t("cameras.wall")} ⛶</Button>
  </div>

  {#if cameras.length === 0}
    <div class="rounded-xl border border-dashed border-dida-border p-8 text-center text-dida-text-muted">
      <p>{t("cameras.empty")}</p>
      <p class="mt-1 text-m text-dida-text-faint">{t("cameras.emptyHint")}</p>
    </div>
  {:else}
    <div
      bind:clientWidth={gridW}
      class="grid gap-2 {wall ? 'min-h-0 flex-1' : ''}"
      style={wall
        ? `grid-template-columns: repeat(${kioskCols}, minmax(0,1fr)); grid-auto-rows: minmax(0,1fr);`
        : cols === 1
          ? "grid-template-columns: 1fr; grid-auto-rows: auto;"
          : `grid-template-columns: repeat(${cols}, minmax(0,1fr)); grid-auto-rows: ${rowH}px; grid-auto-flow: row dense;`}
    >
      {#each ordered as c (c.id)}
        {@const active = isActive(c)}
        {@const sp = spanOf(c.id)}
        <!-- svelte-ignore a11y_no_static_element_interactions -- drag-to-reorder in edit mode; there is no keyboard gesture for "put this tile there", and the wall is read-only outside edit -->
        <div
          draggable={editMode}
          ondragstart={() => (dragId = c.id)}
          ondragover={(e) => { if (editMode) e.preventDefault(); }}
          ondrop={() => onDrop(c.id)}
          style={wall || cols === 1 ? "" : `grid-column: span ${Math.min(sp.c, cols)}; grid-row: span ${sp.r};`}
          class="group relative block h-full overflow-hidden rounded-xl border bg-black text-left transition
                 {active ? 'border-dida-warn/60' : 'border-dida-border'}
                 {editMode ? 'cursor-move ring-2 ring-dida-accent/50' : ''} {dragId === c.id ? 'opacity-40' : ''}"
        >
          <!-- One column (phone): the tile takes the FRAME's own aspect — a panorama
               is a short wide strip, a portrait doorbell a tall one; no letterboxing.
               Multi-column keeps the 16:9 cell packing (admin-arranged wall). -->
          <CameraSnap src={snap(c.id)} alt={c.name} class={cols === 1 && !wall ? "block h-auto w-full" : "size-full object-contain"} />

          {#if !editMode}
            <button type="button" onclick={() => (solo = c)} aria-label={c.name}
              class="absolute inset-0 z-[5] cursor-pointer"></button>
          {/if}

          <!-- edit mode: width/height steppers (snap to the grid, so tiles stay tidy) -->
          {#if editMode}
            <div class="absolute left-1 top-1 z-[8] flex items-center gap-1 rounded-lg bg-black/80 px-1.5 py-1 text-white"
                 onpointerdown={(e) => e.stopPropagation()} role="group" aria-label={t("cameras.resize")}>
              <span class="pr-0.5 text-2xs text-white/60">W</span>
              <button type="button" onclick={(e) => { e.stopPropagation(); resize(c.id, -1, 0); }}
                class="flex size-5 items-center justify-center rounded bg-white/15 text-m leading-none hover:bg-white/30" aria-label="width -">−</button>
              <button type="button" onclick={(e) => { e.stopPropagation(); resize(c.id, 1, 0); }}
                class="flex size-5 items-center justify-center rounded bg-white/15 text-m leading-none hover:bg-white/30" aria-label="width +">+</button>
              <span class="pl-1 pr-0.5 text-2xs text-white/60">H</span>
              <button type="button" onclick={(e) => { e.stopPropagation(); resize(c.id, 0, -1); }}
                class="flex size-5 items-center justify-center rounded bg-white/15 text-m leading-none hover:bg-white/30" aria-label="height -">−</button>
              <button type="button" onclick={(e) => { e.stopPropagation(); resize(c.id, 0, 1); }}
                class="flex size-5 items-center justify-center rounded bg-white/15 text-m leading-none hover:bg-white/30" aria-label="height +">+</button>
            </div>
          {/if}

          <!-- top-right: recognised identities (people + pets/vehicles). Shown only
               when something is NAMED — anonymous detection is deliberately silent.
               With an events feed the badge is a button. -->
          {#if c.people.length > 0 || c.objects.length > 0}
            {#if c.hasEvents && !editMode}
              <button type="button" title={t("cameras.viewEvents")}
                onclick={(e) => { e.stopPropagation(); openEvents(c); }}
                class="absolute right-1.5 top-1.5 z-[7] flex flex-wrap justify-end gap-1 rounded-full ring-dida-accent/0 transition hover:brightness-125">
                {@render detBadges(c)}
              </button>
            {:else}
              <div class="pointer-events-none absolute right-1.5 top-1.5 z-[6] flex flex-wrap justify-end gap-1">
                {@render detBadges(c)}
              </div>
            {/if}
          {/if}

          <!-- top-left (view mode): one badge per scene region — the NAME, lit when the
               region reads active. The state itself is the colour, so it isn't spelled out. -->
          {#if c.scenes.length && !editMode}
            <div class="pointer-events-none absolute left-1.5 top-1.5 z-[6] flex flex-wrap gap-1">
              {#each c.scenes as s (s.label)}
                <Tag onpicture tone="quiet" kind={sceneLit(s.state) ? "lit" : undefined}>{tr(s.label)}</Tag>
              {/each}
            </div>
          {/if}

          <!-- bottom bar: name + site + activity -->
          <div class="pointer-events-none absolute inset-x-0 bottom-0 z-[6] flex items-center gap-2 bg-gradient-to-t from-black/80 to-transparent px-2.5 pb-1.5 pt-6">
            {#if c.motion}
              <span class="size-2 shrink-0 animate-pulse rounded-full bg-dida-warn"></span>
            {:else}
              <span class="size-2 shrink-0 rounded-full bg-dida-ok/80"></span>
            {/if}
            <span class="truncate text-m font-medium text-white">{c.name}</span>
            {#if c.site}
              <Tag onpicture>📍 {c.site}</Tag>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Solo / focused view: one live stream, closed when dismissed. -->
  {#if solo}
    {@const c = solo}
    <div class="fixed inset-0 z-50 flex flex-col bg-black/90 p-3 sm:p-6" role="dialog" aria-modal="true">
      <div class="mb-2 flex items-center gap-3">
        <span class="text-l font-semibold text-white">{c.name}</span>
        {#if c.site}
          <Tag onpicture>📍 {c.site}</Tag>
        {/if}
        {#each c.scenes as s (s.label)}
          <Tag onpicture tone="quiet" kind={sceneLit(s.state) ? "lit" : undefined}>{tr(s.label)}</Tag>
        {/each}
        <div class="ml-auto flex items-center gap-2">
          {#if c.hasEvents}
            <button type="button" onclick={() => openEvents(c)}
              class="rounded border border-white/25 px-2.5 py-1 text-s text-white hover:bg-white/10">{t("cameras.events")}</button>
          {/if}
          {#if c.id.startsWith("baba:") && auth.isAdmin}
            {#if BABA_URL}
              <a href={BABA_URL} target="_blank" rel="noopener"
                 class="rounded border border-white/25 px-2.5 py-1 text-s text-white hover:bg-white/10">{t("cameras.openInBaba")} ↗</a>
            {/if}
          {/if}
          <button type="button" onclick={() => (solo = null)} aria-label={t("common.close")}
            class="rounded border border-white/25 px-2.5 py-1 text-s text-white hover:bg-white/10">✕</button>
        </div>
      </div>
      <div class="flex min-h-0 flex-1 items-center justify-center">
        <!-- Closed while the event viewer covers it: a stream behind it is bandwidth
             taken from the clip playing in front of it. -->
        {#if !evCam}
          <LiveVideo entityId={c.id} alt={c.name} class="max-h-full max-w-full rounded-lg object-contain" />
        {/if}
      </div>
    </div>
  {/if}

  <!-- Recorded-event viewer: the evidence behind a live detection. -->
  {#if evCam}
    {@const c = evCam}
    <div class="fixed inset-0 z-[60] flex flex-col bg-black/95 p-3 sm:p-6" role="dialog" aria-modal="true">
      <div class="mb-2 flex items-center gap-3">
        <span class="text-l font-semibold text-white">{c.name}</span>
        {#if c.site}
          <Tag onpicture>📍 {c.site}</Tag>
        {/if}
        <span class="text-s text-white/60">{t("cameras.recordedEvents")}</span>
        <button type="button" onclick={closeEvents} aria-label={t("common.close")}
          class="ml-auto rounded border border-white/25 px-2.5 py-1 text-s text-white hover:bg-white/10">✕</button>
      </div>

      {#if evLoading}
        <div class="flex flex-1 items-center justify-center text-white/70">{t("common.loading")}</div>
      {:else if !evList.length}
        <div class="flex flex-1 items-center justify-center text-center text-white/70">{t("cameras.noEvents")}</div>
      {:else if evCur}
        <div class="relative flex min-h-0 flex-1 items-center justify-center">
          <img src={evImg(c, evCur, true)} alt="" aria-hidden="true"
            class="absolute inset-0 size-full rounded-lg object-contain blur-lg" />
          {#if evPlay && evHasClip(evCur)}
            {#if evErr}
              <p class="absolute inset-x-0 top-1/2 z-10 -translate-y-1/2 text-center text-m text-dida-warn">
                {evErr === "empty" ? t("cameras.clipEmpty") : t("cameras.clipFailed")}</p>
            {/if}
            <!-- svelte-ignore a11y_media_has_caption -- a camera clip has no caption track to offer; there is nothing to add here, only something to invent -->
            <!-- An archive with no segments for the window answers 200 with a VALID
                 empty MP4 (zero-length, no tracks) — it loads fine and sits at 0:00,
                 so the honest signal is metadata duration 0, not onerror. -->
            <video src={evClip(c, evCur)} controls autoplay playsinline
              onerror={() => (evErr = "failed")}
              onloadedmetadata={(e) => { const d = e.currentTarget.duration; if (!d || isNaN(d)) evErr = "empty"; }}
              class="relative max-h-full max-w-full rounded-lg bg-black"></video>
          {:else}
            <img src={evImg(c, evCur)} alt={trClass(evCur.label)} class="relative max-h-full max-w-full rounded-lg object-contain" />
          {/if}
          {#if evIdx > 0}
            <button type="button" onclick={() => (evIdx -= 1)} aria-label={t("cameras.prevEvent")}
              class="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black/80">‹</button>
          {/if}
          {#if evIdx < evList.length - 1}
            <button type="button" onclick={() => (evIdx += 1)} aria-label={t("cameras.nextEvent")}
              class="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/60 px-3 py-2 text-xl text-white hover:bg-black/80">›</button>
          {/if}
          <div class="pointer-events-none absolute inset-x-0 bottom-0 flex items-center gap-2 bg-gradient-to-t from-black/80 to-transparent px-3 pb-2 pt-8 text-m text-white">
            <span class="rounded-full bg-black/70 px-2 py-0.5 font-semibold">{trClass(evCur.label)}</span>
            <span class="tabular-nums">{evTime(evCur.start_time)}</span>
            {#if typeof evCur.top_score === "number"}
              <span class="text-white/70">{Math.round(evCur.top_score * 100)}%</span>
            {/if}
            <span class="ml-auto text-s text-white/50">{evIdx + 1}/{evList.length}</span>
          </div>
        </div>

        <div class="mt-2 flex shrink-0 gap-1.5 overflow-x-auto pb-1">
          {#each evList as e, i (e.id)}
            <button type="button" onclick={() => (evIdx = i)} aria-label={evTime(e.start_time)}
              class="relative h-14 w-20 shrink-0 overflow-hidden rounded border-2 {i === evIdx ? 'border-dida-accent' : 'border-transparent opacity-60 hover:opacity-100'}">
              <img src={evImg(c, e, true)} alt={trClass(e.label)} class="size-full bg-white/5 object-cover" loading="lazy" />
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>
