<script module lang="ts">
  import { page } from "$app/state";
  import { version } from "$lib/version.svelte";

  // The kit's reload would land on bare /panel, where the cast token is already
  // stripped from the URL; the panel reloads itself through the cast URL instead.
  version.holdWhile(() => page.url.pathname === "/panel");
</script>

<script lang="ts">
  // Living-room wall panel (Nest Hub via DashCast). Rendered chrome-free (see the
  // root layout); authenticates itself from the token in the cast URL (?k=…),
  // exchanges it for a session cookie, then strips the token from the URL.
  //
  // Three modes:
  //  • active — live read-only floor plan + a rotating camera pane.
  //  • saver  — after idle: OPUS photo slideshow + clock + Ecowitt weather.
  //  • bell   — a doorbell press interrupts everything with the gate camera live
  //             (MJPEG), then falls back to active.
  // A tap anywhere wakes/returns to active. Every transition (and every tap) is
  // logged server-side via /panel/event, so the Hub's behaviour — including
  // whether DashCast delivers touch at all — is verifiable from the api logs.
  import { onMount, onDestroy } from "svelte";
  import { clock, longDate, longDay } from "$lib/dt";
  import { t } from "$lib/i18n";
  import { i18n } from "$lib/kit";
  import { replaceState } from "$app/navigation";
  import { auth } from "$lib/auth.svelte";
  import { api, Unauthorized, type Floor, type PanelConfig, type SlideshowPhoto } from "$lib/api";
  import { devices, type CapState } from "$lib/store.svelte";
  import { capMeta, formatValue } from "$lib/capabilities";
  import Brand from "$lib/Brand.svelte";
  import CameraSnap from "$lib/CameraSnap.svelte";
  import LiveVideo from "$lib/LiveVideo.svelte";
  import FloorplanView from "$lib/FloorplanView.svelte";

  const CAM_ROTATE_S = 8;    // rotating pane: next camera
  const SNAP_S = 2;          // rotating pane: snapshot poll
  const BELL_S = 60;         // bell view: fall back to active after
  const PHOTO_BATCH = 30;    // ids fetched per /photos/random call

  // Admin-configured display behaviour (Settings → Panel); safe defaults until it
  // loads so the panel is fully functional even if the config fetch is slow.
  let cfg = $state<PanelConfig>({
    idle_s: 120, photo_interval_s: 20, transition: "kenburns", photo_fit: "blur", presence_entity: "",
  });
  const IDLE_S = $derived(cfg.idle_s);
  const PHOTO_S = $derived(cfg.photo_interval_s);

  const EMPTY_S = 180;       // presence gating: sleep the display after this long empty
  let status = $state<"authing" | "ready" | "denied">("authing");
  let mode = $state<"active" | "saver" | "bell" | "asleep">("active");
  let now = $state(new Date());
  let bootKey: string | null = null; // the cast URL's panel token, for self-reload

  async function authenticate() {
    const k = page.url.searchParams.get("k");
    bootKey = k;
    try {
      if (k) {
        await api.panelAuth(k);
        // Drop the token from the visible URL — cosmetic only, and on a slow
        // device (the Hub's aged Chromium) the auth roundtrip can finish BEFORE
        // SvelteKit's router is initialized, making replaceState throw. That
        // must never take the panel down: proven on the Hub, where the page
        // authenticated and then sat on "denied" while a desktop browser
        // running the same build booted fine.
        try {
          replaceState("/panel", {});
        } catch {
          /* router not ready yet — the token stays in a URL bar nobody sees */
        }
      }
      await auth.load(); // populate auth.user (from the fresh cookie, or an existing one)
      status = auth.user ? "ready" : "denied";
    } catch (e) {
      status = e instanceof Unauthorized ? "denied" : "denied";
    }
    if (status === "ready") {
      void api.panelEvent("boot");
      floors = await api.listFloors();
      try {
        camOrder = (await api.cameraLayout()).order;
      } catch {
        camOrder = []; // wall order is cosmetic — alphabetical fallback
      }
      try {
        cfg = await api.panelConfig();
      } catch {
        /* keep the safe defaults — the panel stays fully functional */
      }
    }
  }

  // ── floors: the panel shows the primary (first) floor ──
  let floors = $state<Floor[]>([]);
  const curFloor = $derived([...floors].sort((a, b) => a.sort_order - b.sort_order)[0] ?? null);
  const markerScale = $derived(floors.find((f) => f.marker_scale != null)?.marker_scale ?? 1);

  // ── cameras: same set + order as the Kamere wall (camera_order), rotating ──
  let camOrder = $state<string[]>([]);
  const cameras = $derived.by(() => {
    const cams = devices.list
      .filter((d) => typeof d.caps["camera"]?.value === "string")
      .sort((a, b) => a.name.localeCompare(b.name, "hr"));
    if (!camOrder.length) return cams;
    const idx = new Map(camOrder.map((id, i) => [id, i]));
    return [...cams].sort((a, b) => (idx.get(a.entityId) ?? 999) - (idx.get(b.entityId) ?? 999));
  });
  let camIdx = $state(0);
  let snapTick = $state(0);
  const cam = $derived(cameras.length ? cameras[camIdx % cameras.length] : null);
  $effect(() => {
    if (mode !== "active" || status !== "ready") return;
    const rot = setInterval(() => (camIdx += 1), CAM_ROTATE_S * 1000);
    const poll = setInterval(() => (snapTick += 1), SNAP_S * 1000);
    return () => { clearInterval(rot); clearInterval(poll); };
  });

  // ── weather: Ecowitt entities, merged across stations (freshest value per cap) ──
  function mergedEco(suffix: string): Record<string, CapState> {
    const out: Record<string, CapState> = {};
    for (const d of devices.list) {
      if (!d.entityId.startsWith("ecowitt:") || !d.entityId.endsWith(`:${suffix}`)) continue;
      for (const [c, cs] of Object.entries(d.caps)) {
        if (typeof cs.value !== "number") continue;
        if (!out[c] || cs.updatedAt > out[c].updatedAt) out[c] = cs;
      }
    }
    return out;
  }
  const outdoor = $derived(mergedEco("outdoor"));
  const indoor = $derived(mergedEco("indoor"));
  const unitOf = (cap: string, cs: CapState): string =>
    cap === "temperature" ? "°" : (cs.unit ?? capMeta(cap).unit ?? "");
  // The secondary weather rows, in display order; absent caps simply don't render.
  const SAVER_ROWS = ["humidity", "wind_speed", "rain_rate", "rain_daily", "uv_index", "pressure"];
  const saverRows = $derived(
    SAVER_ROWS.filter((c) => outdoor[c] != null)
      // a dry day's 0.0 mm/h rain rate is noise — show the rate only while raining
      .filter((c) => c !== "rain_rate" || (outdoor[c].value as number) > 0),
  );

  // ── screensaver photos (OPUS through the DIDA proxy) ──
  // Randomisation: we pull a batch of random photo ids from OPUS, show them
  // one at a time, then pull another random batch — so over time the whole
  // (album) library cycles in a fresh random order each round. Only IDs are
  // held; each preview is fetched on demand (and browser-cached for an hour).
  //
  // A photo is swapped onto the screen ONLY after it has fully loaded (the
  // preload pipeline below), and the NEXT one is fetched while the current is
  // shown — so a slow or failed fetch never leaves the panel black. Bad assets
  // (a video with no thumbnail, a transient proxy error) are skipped at once,
  // and a transient empty/failed batch is retried, never latched.
  const PHOTO_ERR_RETRY_S = 4; // after a failure, retry sooner than the full interval
  let photoQueue: SlideshowPhoto[] = [];
  let photoPos = 0;
  let photosConfigured = $state(true); // false ONLY on photos_not_configured (a real "no photos")
  let photosErr = $state(false);   // transient fetch trouble → note it, keep retrying
  let photoA = $state<string | null>(null);
  let photoB = $state<string | null>(null);
  let photoShowB = $state(false);
  let shownMeta = $state<SlideshowPhoto | null>(null); // the visible photo's when/where
  const noPhoto = $derived(!photoA && !photoB);

  // A loaded photo, ready to swap in: its preview URL plus its when/where caption.
  interface Loaded { url: string; meta: SlideshowPhoto }

  // Refill the queue with a fresh random batch. Returns false on any trouble
  // (caller retries) — an empty or failed batch must never latch the slideshow
  // off; only a "not configured" answer does (there's genuinely nothing to show).
  async function refill(): Promise<boolean> {
    try {
      const r = await api.photosRandom(PHOTO_BATCH);
      if (!r.photos.length) { photosErr = true; return false; } // transient empty → retry
      photoQueue = r.photos; photoPos = 0; photosErr = false; return true;
    } catch (e) {
      if (e instanceof Error && e.message.includes("photos_not_configured")) photosConfigured = false;
      else photosErr = true;
      return false;
    }
  }
  // Load the next photo fully (decoded and ready to paint), skipping assets that
  // fail. Resolves with a ready photo, or null when there's nothing to show right
  // now (not configured, or the batch/asset fetches are failing — retry later).
  async function loadNext(attempts = 0): Promise<Loaded | null> {
    if (!photosConfigured || attempts > 8) return null;
    if (photoPos >= photoQueue.length && !(await refill())) return null;
    const meta = photoQueue[photoPos++];
    const url = api.photoPreviewUrl(meta.id);
    const ok = await new Promise<boolean>((resolve) => {
      const img = new Image();
      img.onload = () => resolve(true);
      img.onerror = () => resolve(false); // real skip — the comment used to lie
      img.src = url;
    });
    return ok ? { url, meta } : loadNext(attempts + 1); // a bad asset is skipped immediately
  }
  function swapIn(p: Loaded) {
    // Cross-fade: set the hidden layer to the new (already-loaded) photo, flip.
    if (photoShowB) { photoA = p.url; photoShowB = false; }
    else { photoB = p.url; photoShowB = true; }
    shownMeta = p.meta;
  }
  $effect(() => {
    if (mode !== "saver") return;
    // Re-probe config on every entry: OPUS can be connected at runtime, and the
    // page lives on the Hub for weeks. (The previous photo stays on screen across
    // a re-entry, so returning to the saver never flashes black.)
    photosConfigured = true;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let ahead: Promise<Loaded | null> = loadNext(); // prefetch one ahead
    const tick = async () => {
      const p = await ahead;
      ahead = loadNext(); // start loading the following photo while this one shows
      if (!alive) return;
      if (p) swapIn(p);
      // On success pace at the configured interval; on trouble retry soon so a
      // blip clears in seconds, not a whole photo interval.
      timer = setTimeout(tick, (p ? PHOTO_S : PHOTO_ERR_RETRY_S) * 1000);
    };
    void tick();
    return () => { alive = false; clearTimeout(timer); };
  });
  // The visible photo's caption: "Place · when". Date only (the wall clock above
  // carries today's time); year shown so an old photo reads as old.
  const photoWhen = $derived.by(() => {
    if (!shownMeta?.taken_at) return "";
    return longDate(shownMeta.taken_at);
  });
  const photoPlace = $derived.by(() => {
    const country = shownMeta?.country ? new Intl.DisplayNames([i18n.locale], { type: "region" }).of(shownMeta.country) : null;
    return [shownMeta?.place, country].filter(Boolean).join(", ");
  });
  const photoCaption = $derived([photoPlace, photoWhen].filter(Boolean).join(" · "));

  // Photo transition: crossfade duration + optional Ken Burns slow zoom (see the
  // keyframes below). "none" is a hard cut. Bound to the two <img> layers.
  const fadeMs = $derived(cfg.transition === "none" ? 0 : 1000);
  const kenburns = $derived(cfg.transition === "kenburns");
  // How a photo fills the screen: object-fit for the main image; "blur" also
  // paints a blurred, dimmed copy behind it so a portrait's letterbox reads as a
  // soft fill instead of hard black bars. "cover" fills and crops.
  const objectFit = $derived(cfg.photo_fit === "cover" ? "object-cover" : "object-contain");
  const blurFill = $derived(cfg.photo_fit === "blur");

  // ── bell interrupt: any `…:bell` button press → that camera, live, fullscreen ──
  let bellCam = $state<string | null>(null);
  let bellSeen: Record<string, number> = {};
  let bellTimer: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    for (const d of devices.list) {
      if (!d.entityId.endsWith(":bell")) continue;
      const cs = d.caps["button"];
      if (!cs) continue;
      const prev = bellSeen[d.entityId];
      bellSeen[d.entityId] = cs.updatedAt;
      // prev === undefined is the snapshot seed — an OLD press must not fire on boot.
      if (prev !== undefined && cs.updatedAt > prev) onBell(d.entityId);
    }
  });
  function onBell(bellId: string) {
    const camId = bellId.slice(0, -":bell".length);
    if (!devices.byId[camId]?.caps["camera"]) return; // a bell with no camera sibling
    bellCam = camId;
    mode = "bell";
    void api.panelEvent("bell");
    if (bellTimer) clearTimeout(bellTimer);
    bellTimer = setTimeout(() => {
      if (mode === "bell") { mode = "active"; lastActivity = Date.now(); }
    }, BELL_S * 1000);
  }

  // ── liveness beat: the only signal that says this page is still alive. ──
  // Everything else about a wall panel looks healthy from the outside even when
  // it is dead — DashCast holds the receiver whatever the page shows. Beat while
  // ready, in every mode (the screensaver and the black asleep screen are still
  // us); the cast adapter re-casts when the beat stops. See adapters/cast.
  const ALIVE_S = 60;
  $effect(() => {
    if (status !== "ready") return;
    void api.panelAlive();
    const t = setInterval(() => void api.panelAlive(), ALIVE_S * 1000);
    return () => clearInterval(t);
  });

  // ── self-update: a kiosk has no user to tap the layout's "reload" banner.
  //    When a deploy makes this tab stale, reload — the page lives on the Hub
  //    for weeks and there is no other way to pick up new code (a DashCast
  //    load_url can't reach a page that was loaded top-level). Re-enter through
  //    the full cast URL so the reload re-auths even if the session cookie has
  //    meanwhile expired. ──
  let reloading = false;
  $effect(() => {
    if (!version.available || reloading) return;
    reloading = true;
    void reloadWhenServed();
  });

  // A new build means a deploy is in flight, and a deploy restarts the very
  // container that serves this page — navigate into that window and the Hub
  // lands on the proxy's error page, where no code of ours is left to retry.
  // (That is exactly how an error page sat on the wall for twelve hours on
  // 2026-07-22.) So confirm the origin is answering, immediately before leaving,
  // and wait as long as it takes: the watchdog is the backstop, not the plan.
  const RELOAD_RETRY_S = 10;
  async function reloadWhenServed() {
    for (;;) {
      try {
        await api.version();
        break;
      } catch {
        await new Promise((r) => setTimeout(r, RELOAD_RETRY_S * 1000));
      }
    }
    location.assign(bootKey ? `/panel?k=${encodeURIComponent(bootKey)}` : "/panel");
  }

  // ── presence gating: sleep the display when the room is empty ──
  // The Hub's own presence sensor isn't reachable from a web page, but DIDA
  // already knows the room from a chosen occupancy/motion/person entity. When
  // it's been empty for EMPTY_S the panel goes black (asleep); any presence — or
  // a touch — wakes it. Empty entity id, or an unknown/missing entity, means
  // "always on" (never lock the screen off on a bad config).
  const roomPresent = $derived.by(() => {
    if (!cfg.presence_entity) return true;
    const d = devices.byId[cfg.presence_entity];
    if (!d) return true;
    return d.caps["occupancy"]?.value === true
      || d.caps["motion"]?.value === true
      || (typeof d.caps["person_count"]?.value === "number" && (d.caps["person_count"].value as number) > 0);
  });
  let lastPresence = Date.now();
  $effect(() => {
    if (roomPresent) {
      lastPresence = Date.now();
      // Walked into an empty room → wake straight to the live view.
      if (mode === "asleep") { mode = "active"; lastActivity = Date.now(); void api.panelEvent("wake"); }
    }
  });

  // ── idle / wake ──
  let lastActivity = Date.now();
  function onPointer() {
    lastActivity = Date.now();
    if (mode === "saver" || mode === "asleep") { mode = "active"; void api.panelEvent("wake"); }
    else if (mode === "bell") { mode = "active"; void api.panelEvent("tap"); }
    else void api.panelEvent("tap");
  }
  let tick: ReturnType<typeof setInterval>;
  let idler: ReturnType<typeof setInterval>;
  onMount(() => {
    void authenticate();
    tick = setInterval(() => (now = new Date()), 1000);
    idler = setInterval(() => {
      // Empty room for long enough → sleep (from any awake mode). Presence check
      // first so a room that stays occupied never sleeps just from no touching.
      if (mode !== "asleep" && mode !== "bell" && cfg.presence_entity
          && !roomPresent && Date.now() - lastPresence > EMPTY_S * 1000) {
        mode = "asleep";
        void api.panelEvent("sleep");
      } else if (mode === "active" && Date.now() - lastActivity > IDLE_S * 1000) {
        mode = "saver";
        void api.panelEvent("saver");
      }
    }, 5000);
    // Capture phase: markers/labels below may stopPropagation, the wake must not care.
    window.addEventListener("pointerdown", onPointer, true);
  });
  onDestroy(() => {
    clearInterval(tick);
    clearInterval(idler);
    if (bellTimer) clearTimeout(bellTimer);
    window.removeEventListener("pointerdown", onPointer, true);
  });

  const hhmm = $derived(clock(now));
  const dateStr = $derived(longDay(now));
  const snapUrl = (id: string): string =>
    `/api/camera/${encodeURIComponent(id)}/snapshot?w=640&t=p${snapTick}`;
</script>

<svelte:head><title>{t("panel.view.title")}</title></svelte:head>

<div class="fixed inset-0 flex flex-col overflow-hidden bg-dida-bg text-dida-text select-none">
  {#if status === "denied"}
    <div class="flex flex-1 flex-col items-center justify-center gap-3 text-center">
      <Brand size="md" />
      <p class="text-dida-text-muted">{t("panel.view.denied")}</p>
      <p class="max-w-sm text-m text-dida-text-faint">{t("panel.view.deniedHint")}</p>
    </div>
  {:else}
    <!-- ── ACTIVE: plan + rotating camera (always mounted; overlays cover it) ── -->
    <header class="flex items-baseline justify-between px-6 pt-3">
      <div class="flex items-baseline gap-3">
        <span class="text-4xl font-semibold leading-none tabular-nums tracking-tight">{hhmm}</span>
        <span class="text-m capitalize text-dida-text-muted">{dateStr}</span>
      </div>
      {#if outdoor.temperature}
        <span class="text-2xl font-semibold tabular-nums">
          {formatValue("temperature", outdoor.temperature.value as number)}°
          {#if indoor.temperature}
            <span class="ml-2 text-m font-normal text-dida-text-muted">{t("panel.view.inside")} {formatValue("temperature", indoor.temperature.value as number)}°</span>
          {/if}
        </span>
      {/if}
    </header>

    <main class="flex min-h-0 flex-1 gap-4 p-4">
      {#if curFloor}
        <FloorplanView floor={curFloor} {markerScale}
          class="h-full aspect-square shrink-0 rounded-xl border border-dida-border bg-dida-panel" />
      {/if}
      <section class="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-dida-border bg-dida-panel">
        {#if cam}
          <div class="relative min-h-0 flex-1 bg-black">
            <CameraSnap src={snapUrl(cam.entityId)} alt={cam.name} class="h-full w-full object-contain" />
            <div class="absolute inset-x-0 bottom-0 flex items-center justify-between bg-gradient-to-t from-black/70 to-transparent px-3 pb-2 pt-6">
              <span class="text-m font-medium text-white">{cam.name}</span>
              {#if cameras.length > 1}
                <span class="flex gap-1">
                  {#each cameras as c, i (c.entityId)}
                    <span class="h-1.5 w-1.5 rounded-full {i === camIdx % cameras.length ? 'bg-white' : 'bg-white/30'}"></span>
                  {/each}
                </span>
              {/if}
            </div>
          </div>
        {:else}
          <div class="flex flex-1 items-center justify-center text-m text-dida-text-faint">{t("panel.view.noCameras")}</div>
        {/if}
      </section>
    </main>

    <!-- ── SCREENSAVER overlay ── -->
    {#if mode === "saver"}
      {#snippet photoLayer(src: string)}
        {#key src}
          {#if blurFill}
            <!-- Blurred, dimmed copy fills the letterbox so a portrait reads as a
                 soft fill, not hard black bars. Same src → decoded once. -->
            <img src={src} alt="" aria-hidden="true" draggable="false"
              class="absolute inset-0 h-full w-full scale-110 object-cover blur-2xl brightness-[0.45]" />
          {/if}
          <!-- The whole photo (object-contain), unless "cover" (fill + crop). -->
          <img src={src} alt="" draggable="false"
            class="absolute inset-0 h-full w-full {objectFit} {kenburns ? 'kenburns' : ''}" />
        {/key}
      {/snippet}

      <div class="absolute inset-0 z-40 overflow-hidden bg-black">
        <!-- Two persistent layers crossfade via opacity; the inner imgs are keyed
             on src so the Ken Burns zoom restarts per photo without breaking the
             outer layer's fade. -->
        <div class="absolute inset-0" style="opacity:{photoShowB ? 0 : 1}; transition:opacity {fadeMs}ms ease-in-out">
          {#if photoA}{@render photoLayer(photoA)}{/if}
        </div>
        <div class="absolute inset-0" style="opacity:{photoShowB ? 1 : 0}; transition:opacity {fadeMs}ms ease-in-out">
          {#if photoB}{@render photoLayer(photoB)}{/if}
        </div>

        <!-- Soft corner scrims: a gentle darkening at the two text corners that
             fades into the photo, so the white text stays legible over ANY photo
             (bright, busy, pale) without a hard box. Pure CSS gradient — no blur
             filter, safe on the Cast device. -->
        <div class="pointer-events-none absolute inset-0" style="background:
          radial-gradient(68% 70% at 0% 0%, rgba(0,0,0,0.66), transparent 74%),
          radial-gradient(62% 52% at 100% 100%, rgba(0,0,0,0.64), transparent 72%)"></div>

        <!-- Clock + weather: fixed TOP-LEFT (the Hub is LCD — no burn-in — so it
             stays put rather than drifting). A text-shadow keeps it legible over
             any photo without darkening the frame. -->
        <div class="pointer-events-none absolute left-10 top-8 flex flex-col gap-5"
          style="text-shadow:0 2px 12px rgba(0,0,0,0.75)">
          <div>
            <div class="text-8xl font-semibold leading-none tabular-nums tracking-tight text-white">{hhmm}</div>
            <div class="mt-2 text-2xl capitalize text-white/85">{dateStr}</div>
          </div>
          <div class="text-white">
            {#if outdoor.temperature}
              <div class="text-6xl font-semibold leading-none tabular-nums">{formatValue("temperature", outdoor.temperature.value as number)}°</div>
            {/if}
            <div class="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-2xl tabular-nums text-white/90">
              {#each saverRows as c (c)}
                <span>
                  {#if c === "uv_index"}<span class="text-white/65">UV </span>{/if}
                  {formatValue(c, outdoor[c].value as number)}<span class="text-white/65"> {unitOf(c, outdoor[c])}</span>
                </span>
              {/each}
            </div>
            {#if indoor.temperature}
              <div class="mt-2 text-xl text-white/75">{t("panel.view.inside")} {formatValue("temperature", indoor.temperature.value as number)}°{#if indoor.humidity}&nbsp;· {formatValue("humidity", indoor.humidity.value as number)}%{/if}</div>
            {/if}
          </div>
        </div>

        <!-- The current photo's when/where (EXIF) — BOTTOM-RIGHT, big and clear. -->
        {#if photoCaption}
          <div class="pointer-events-none absolute bottom-8 right-10 flex flex-col items-end text-right text-white"
            style="text-shadow:0 2px 16px rgba(0,0,0,0.9)">
            {#if photoPlace}
              <div class="flex items-center gap-2.5 text-4xl font-semibold leading-tight">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" class="size-7 shrink-0 opacity-80" aria-hidden="true"><path d="M12 21s-6-5.3-6-10a6 6 0 0 1 12 0c0 4.7-6 10-6 10Z"/><circle cx="12" cy="11" r="2"/></svg>
                <span>{photoPlace}</span>
              </div>
            {/if}
            {#if photoWhen}
              <div class="mt-2 text-2xl text-white/85 tabular-nums">{photoWhen}</div>
            {/if}
          </div>
        {/if}

        {#if noPhoto && (photosErr || !photosConfigured)}
          <p class="absolute left-1/2 top-6 -translate-x-1/2 text-s text-white/40">{t("panel.view.photosOff")}</p>
        {/if}
      </div>
    {/if}

    <!-- ── BELL overlay: the gate camera, live ── -->
    {#if mode === "bell" && bellCam}
      <div class="absolute inset-0 z-50 bg-black">
        <LiveVideo entityId={bellCam} alt={devices.byId[bellCam]?.name ?? ""} class="h-full w-full object-contain" />
        <div class="absolute left-6 top-5 flex items-center gap-3 rounded-full bg-black/60 px-4 py-2 backdrop-blur">
          <span class="relative flex h-3 w-3">
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-dida-warn opacity-75"></span>
            <span class="relative inline-flex h-3 w-3 rounded-full bg-dida-warn"></span>
          </span>
          <span class="text-xl font-semibold text-white">{t("panel.view.bell", { name: devices.byId[bellCam]?.name ?? "" })}</span>
        </div>
      </div>
    {/if}

    <!-- ── ASLEEP: room empty → black screen. Presence (or a touch, or the
         doorbell) wakes it. The Hub is LCD, so this + its own ambient dimming
         reads as "off" until someone's there. ── -->
    {#if mode === "asleep"}
      <div class="absolute inset-0 z-[55] bg-black"></div>
    {/if}
  {/if}
</div>

<style>
  /* Ken Burns: a slow, continuous zoom-and-pan on the active photo. Keyed per
     photo (see the {#key} above), so it restarts each time; alternate makes it
     ease back rather than snap. object-cover + the overlay's overflow-hidden
     keep the scaled image filling the frame with no letterbox. */
  :global(img.kenburns) {
    animation: dida-kenburns 24s ease-out both;
    transform-origin: center;
    will-change: transform;
  }
  @keyframes dida-kenburns {
    from { transform: scale(1.02) translate(0, 0); }
    to { transform: scale(1.14) translate(-1.5%, -1.5%); }
  }
</style>
