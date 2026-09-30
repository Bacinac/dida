<script lang="ts">
  import { api } from "$lib/api";
  import { Picks, Tag } from "$lib/kit";
  import { auth } from "$lib/auth.svelte";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import SignalPath from "$lib/SignalPath.svelte";
  import type { Device } from "$lib/store.svelte";
  import {
    fmtTime, gradient, isPlaying, mediaAlbum, mediaArt, mediaArtist, mediaCmd, mediaDuration,
    mediaMuted, mediaQuality, mediaQueue, mediaSource, mediaTitle, mediaVolume, nextTrack, positionBase,
    prevTrack, transport, transportLabel,
  } from "$lib/media";

  let { device, compact = false, volumeDevice = null }: {
    device: Device;
    compact?: boolean;
    // Route volume/mute to a DIFFERENT device than the one showing now-playing —
    // e.g. a streaming player whose analog gain lives on an AVR zone (iFi → Zone 2),
    // so the player stays bit-perfect and the slider drives the real loudness knob.
    volumeDevice?: Device | null;
  } = $props();
  const readOnly = $derived(!auth.canControl); // view-only user: no transport control
  const volDev = $derived(volumeDevice ?? device);

  // NB: not named `state` — that collides with the `$state` rune and breaks
  // svelte-check (it reads `$state(…)` as store-subscribing this variable).
  const xport = $derived(transport(device));
  const playing = $derived(isPlaying(device));
  const title = $derived(mediaTitle(device));
  const artist = $derived(mediaArtist(device));
  const art = $derived(mediaArt(device));
  const duration = $derived(mediaDuration(device));
  const vol = $derived(mediaVolume(volDev) ?? 0);
  const muted = $derived(mediaMuted(volDev));
  const album = $derived(mediaAlbum(device));
  const quality = $derived(mediaQuality(device));
  // Hi-res = above CD (24-bit OR >48 kHz, e.g. "FLAC 96kHz/24bit") → gold badge.
  const hires = $derived.by(() => {
    if (!quality) return false;
    const bits = quality.match(/(\d{1,3})bit/);
    const khz = quality.match(/(\d{1,3}(?:\.\d)?)kHz/);
    return (bits ? +bits[1] > 16 : false) || (khz ? +khz[1] > 48 : false);
  });
  const heading = $derived(title ?? device.name);
  // A live stream has no seekable duration → drives the progress-bar-vs-indicator
  // layout. "UŽIVO" (`live`) is stricter: the stream must actually be playing, so
  // a stopped radio reads "Zaustavljeno", not a lying red LIVE badge.
  const stream = $derived(duration <= 0);
  const live = $derived(stream && playing);
  // Richer now-playing: "Artist • Album" when known, else the live/transport state.
  const sub = $derived(
    [artist, album].filter(Boolean).join(" • ") || (live && title ? t("media.live") : transportLabel(xport)),
  );
  // Some players own no volume (e.g. a HEOS player on a Denon/Marantz — volume
  // is the AVR's job). Hide the slider rather than show a dead control. When a
  // volumeDevice is given, its caps decide (the AVR zone owns the loudness).
  const hasVolume = $derived("volume" in volDev.caps);

  // Ticking clock so the playhead advances between sparse position updates.
  // (The interval itself is set up after the lyrics logic, so it can speed up
  // for smooth karaoke scrolling — see below.)
  let now = $state(Date.now());
  const position = $derived.by(() => {
    const base = positionBase(device);
    if (!base) return 0;
    let p = base.pos;
    if (playing) p += (now - base.at) / 1000;
    if (duration > 0) p = Math.min(p, duration);
    return Math.max(0, p);
  });
  const pct = $derived(duration > 0 ? Math.min(100, (position / duration) * 100) : 0);

  // Volume slider follows the real value unless the user is dragging it.
  let dragging = $state(false);
  let localVol = $state(0);
  $effect(() => {
    if (!dragging) localVol = vol;
  });

  let artFailed = $state(false);
  $effect(() => {
    void art; // reset the fallback when the art URL changes
    artFailed = false;
  });

  // Now-playing views — big art / full queue / lyrics / signal path. The card
  // never grows; the alternate views scroll within it.
  type View = "normal" | "queue" | "lyrics" | "pipeline";
  let view = $state<View>("normal");
  const VIEW_ICON: Record<View, string> = {
    normal: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 6.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z",
    queue: "M4 6h11M4 12h11M4 18h7M17 13v8l5-4z",
    lyrics: "M4 6h10M4 10h16M4 14h10M4 18h16",
    pipeline: "M3 12h4m10 0h4M7 8h10v8H7z",
  };
  const lyricsOpen = $derived(view === "lyrics");
  const source = $derived(mediaSource(device));

  // The play queue + lyrics come straight from device state / a uniform endpoint
  // — no per-source branching here. The queue is whatever the adapter is driving
  // (any source); lyrics are dispatched server-side by the entity's source.
  const queueData = $derived(mediaQueue(device));
  const hasLyrics = $derived(source === "library"); // sources that can supply lyrics
  const viewTabs = $derived([
    { key: "normal", label: t("media.cover") },
    ...(queueData.items.length ? [{ key: "queue", label: t("media.queue") }] : []),
    ...(hasLyrics ? [{ key: "lyrics", label: t("media.lyrics") }] : []),
    { key: "pipeline", label: t("pipeline.title") },
  ].map((tab) => ({ ...tab, title: tab.label })));
  function playFrom(i: number): void {
    run(mediaCmd.playIndex(device.entityId, i));
  }

  // Lyrics — cached per track, cleared on track change, (re)loaded when shown.
  let lyrics = $state<string | null>(null);
  let lyricsSynced = $state<string | null>(null);
  let lyricsLoading = $state(false);
  let lyricsErr = $state(false);
  let lyricsBox = $state<HTMLElement | null>(null);
  $effect(() => {
    void title; // a new track invalidates the cached lyrics
    lyrics = null;
    lyricsSynced = null;
    lyricsErr = false;
  });
  $effect(() => {
    // lyricsErr gates the retry: without it, a failed fetch (lyrics stays null)
    // re-triggers this effect via lyricsLoading→false and refetches forever. The
    // track-change effect above clears lyricsErr, so a new track retries cleanly.
    if (view !== "lyrics" || lyrics !== null || lyricsLoading || lyricsErr) return;
    lyricsLoading = true;
    lyricsErr = false;
    api.mediaLyrics(device.entityId)
      .then((r) => { lyrics = r.text ?? ""; lyricsSynced = r.subtitles ?? null; })
      .catch(() => (lyricsErr = true))
      .finally(() => (lyricsLoading = false));
  });
  // Parse LRC subtitles ("[mm:ss.xx] line") into timestamped lines.
  const syncedLines = $derived.by(() => {
    const out: { t: number; text: string }[] = [];
    for (const raw of (lyricsSynced ?? "").split("\n")) {
      const m = raw.match(/^\[(\d+):(\d+(?:\.\d+)?)\]\s?(.*)$/);
      if (m) out.push({ t: +m[1] * 60 + parseFloat(m[2]), text: m[3] });
    }
    return out;
  });
  // The line the playhead has reached (-1 = none yet). `position` is the live playhead.
  const activeLine = $derived.by(() => {
    let idx = -1;
    for (let i = 0; i < syncedLines.length; i++) {
      if (syncedLines[i].t <= position + 0.25) idx = i;
      else break;
    }
    return idx;
  });
  // Keep the active line centred as it advances — scroll inside the box only.
  $effect(() => {
    const i = activeLine;
    const box = lyricsBox;
    if (!lyricsOpen || i < 0 || !box) return;
    const el = box.querySelector<HTMLElement>(`[data-li="${i}"]`);
    if (el) box.scrollTop = el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2;
  });

  // Advance the playhead clock; tick fast while synced lyrics are open so the
  // karaoke highlight tracks closely, otherwise once a second is plenty.
  $effect(() => {
    if (!playing) return;
    const ms = lyricsOpen && syncedLines.length ? 250 : 1000;
    const id = setInterval(() => (now = Date.now()), ms);
    return () => clearInterval(id);
  });

  let err = $state<string | null>(null);
  async function run(p: Promise<unknown>): Promise<void> {
    err = null;
    try {
      await p;
    } catch (e) {
      err = errMsg(e);
    }
  }
  const commitVol = () => {
    dragging = false;
    run(mediaCmd.setVolume(volDev.entityId, Math.round(localVol)));
  };
</script>

{#snippet artTile(cover = false)}
  {#if art && !artFailed}
    <!-- White base so a transparent brand wordmark (radio-station logos) stays
         readable; object-contain shows the WHOLE logo — never cropped or zoomed.
         Opaque square art (album covers, favicons) fills edge-to-edge, so the
         white never shows. The big cover adds a blurred fill of the same art. -->
    <div class="relative h-full w-full overflow-hidden bg-white">
      {#if cover}
        <div class="absolute inset-0 scale-110 bg-cover bg-center blur-2xl brightness-90"
             style="background-image:url({art})" aria-hidden="true"></div>
      {/if}
      <img src={art} alt="" onerror={() => (artFailed = true)}
           class="relative h-full w-full object-contain {cover ? 'p-2' : ''}" />
    </div>
  {:else}
    <div class="grid h-full w-full place-items-center" style="background:{gradient(heading)}">
      <svg viewBox="0 0 24 24" fill="currentColor" class="h-1/3 w-1/3 text-white/85">
        <path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z" />
      </svg>
    </div>
  {/if}
{/snippet}

{#snippet qualityBadge()}
  {#if quality}
    <Tag tone="quiet" kind={hires ? "hires" : undefined} title={t("media.quality")}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
           class="size-3 {hires ? '' : 'text-dida-accent'}">
        <path d="M5 14v-4M9.5 18V6M14 16V8M18.5 13v-2" />
      </svg>
      <span class="font-mono">{quality}</span>
    </Tag>
  {/if}
{/snippet}

{#snippet iconBtn(label: string, onclick: () => void, path: string, big = false)}
  <button
    type="button" {onclick} aria-label={label} title={label} disabled={readOnly}
    class="grid place-items-center rounded-full transition disabled:opacity-50 disabled:hover:bg-transparent
           {big
            ? 'size-14 bg-dida-accent-strong text-dida-on-accent shadow-lg hover:brightness-110'
            : 'size-10 text-dida-text-muted hover:bg-dida-panel-2 hover:text-dida-text'}"
  >
    <svg viewBox="0 0 24 24" fill="currentColor" class={big ? 'size-7' : 'size-5'}><path d={path} /></svg>
  </button>
{/snippet}

{#snippet transportRow(big: boolean)}
  {@const PLAY = "M8 5v14l11-7z"}
  {@const PAUSE = "M6 5h4v14H6zM14 5h4v14h-4z"}
  {@render iconBtn(t("media.previous"), () => run(prevTrack(device)), "M18 6l-8.5 6 8.5 6V6zM8 6H6v12h2V6z")}
  {@render iconBtn(playing ? t("media.pause") : t("media.play"), () => run(mediaCmd.playPause(device)), playing ? PAUSE : PLAY, big)}
  {@render iconBtn(t("media.stop"), () => run(mediaCmd.stop(device.entityId)), "M6 6h12v12H6z")}
  {@render iconBtn(t("media.next"), () => run(nextTrack(device)), "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z")}
{/snippet}

{#snippet volumeRow()}
  {@const SPK = muted
    ? "M3 9v6h4l5 5V4L7 9H3zm13.5 3l2.7-2.7-1.4-1.4L15 10.6V12zm0 0l2.7 2.7-1.4 1.4L15 13.4z"
    : "M3 9v6h4l5 5V4L7 9H3zm11 .8a3.5 3.5 0 0 1 0 4.4M16.5 7a7 7 0 0 1 0 10"}
  <button
    type="button" onclick={() => run(mediaCmd.toggleMute(volDev.entityId))} disabled={readOnly}
    aria-label={muted ? t("media.unmute") : t("media.mute")} title={muted ? t("media.unmute") : t("media.mute")}
    class="shrink-0 text-dida-text-muted hover:text-dida-text disabled:opacity-50 {muted ? 'opacity-60' : ''}"
  >
    <svg viewBox="0 0 24 24" fill={muted ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round" class="size-5"><path d={SPK} /></svg>
  </button>
  <input
    type="range" min="0" max="100" step="1" bind:value={localVol}
    oninput={() => (dragging = true)} onchange={commitVol} disabled={readOnly}
    aria-label={t("media.volume")} class="w-full accent-dida-accent disabled:opacity-50"
  />
  <span class="w-9 shrink-0 text-right font-mono text-s tabular-nums text-dida-text-faint">{Math.round(localVol)}%</span>
{/snippet}

{#snippet barTransport()}
  {@const PLAY = "M8 5v14l11-7z"}
  {@const PAUSE = "M6 5h4v14H6zM14 5h4v14h-4z"}
  <button type="button" onclick={() => run(prevTrack(device))} disabled={readOnly} aria-label={t("media.previous")} title={t("media.previous")}
          class="grid size-8 place-items-center rounded-full text-dida-text-muted transition hover:bg-dida-panel-2 hover:text-dida-text disabled:opacity-50 disabled:hover:bg-transparent">
    <svg viewBox="0 0 24 24" fill="currentColor" class="size-5"><path d="M18 6l-8.5 6 8.5 6V6zM8 6H6v12h2V6z" /></svg>
  </button>
  <button type="button" onclick={() => run(mediaCmd.playPause(device))} disabled={readOnly} aria-label={playing ? t("media.pause") : t("media.play")} title={playing ? t("media.pause") : t("media.play")}
          class="grid size-10 place-items-center rounded-full bg-dida-accent-strong text-dida-on-accent shadow transition hover:brightness-110 disabled:opacity-50">
    <svg viewBox="0 0 24 24" fill="currentColor" class="size-5"><path d={playing ? PAUSE : PLAY} /></svg>
  </button>
  <button type="button" onclick={() => run(nextTrack(device))} disabled={readOnly} aria-label={t("media.next")} title={t("media.next")}
          class="grid size-8 place-items-center rounded-full text-dida-text-muted transition hover:bg-dida-panel-2 hover:text-dida-text disabled:opacity-50 disabled:hover:bg-transparent">
    <svg viewBox="0 0 24 24" fill="currentColor" class="size-5"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></svg>
  </button>
{/snippet}

{#snippet viewTab(tab: { key: string; label: string })}
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-4"><path d={VIEW_ICON[tab.key as View]} /></svg>
  <span class="hidden sm:inline">{tab.label}</span>
{/snippet}

{#if compact}
  <!-- Inline control for the dashboard DeviceCard. -->
  <div class="flex flex-col gap-2 py-1">
    <div class="flex items-center gap-3">
      <div class="size-12 shrink-0 overflow-hidden rounded-md">{@render artTile()}</div>
      <div class="min-w-0 flex-1">
        <p class="truncate text-m font-medium">{heading}</p>
        <p class="truncate text-s text-dida-text-faint">{sub}</p>
        {#if quality}<div class="mt-1">{@render qualityBadge()}</div>{/if}
      </div>
    </div>
    <div class="flex items-center justify-between">
      <div class="flex items-center -ml-2">{@render transportRow(false)}</div>
      {#if live}<Tag tone="err">{t("media.live")}</Tag>{/if}
    </div>
    {#if hasVolume}<div class="flex items-center gap-2">{@render volumeRow()}</div>{/if}
  </div>
{:else}
  <!-- Now-playing card: fills the height its parent leaves (capped at the former
       fixed size) so a short screen shrinks the cover instead of scrolling the
       page. The PLAY CONTROLS + progress are a PINNED
       header (always at the top — never scroll for them); the view content
       (cover art / queue / lyrics / signal-path) scrolls below within the card. -->
  <div class="relative flex h-full min-h-[14rem] max-h-[34rem] flex-col overflow-hidden rounded-2xl border border-dida-border bg-dida-panel sm:max-h-[38rem]">
    <!-- Blurred art/gradient backdrop. -->
    <div class="pointer-events-none absolute inset-0 opacity-30 blur-2xl"
         style={art && !artFailed ? `background-image:url(${art});background-size:cover;background-position:center` : `background:${gradient(heading)}`}></div>
    <div class="pointer-events-none absolute inset-0 bg-dida-panel/70"></div>

    <!-- PINNED CONTROL HEADER (transport + progress + view tabs). -->
    <div class="relative flex shrink-0 flex-col gap-2 border-b border-dida-border/60 p-3">
      <div class="flex items-center gap-3">
        <div class="size-12 shrink-0 overflow-hidden rounded-md shadow">{@render artTile()}</div>
        <div class="min-w-0 flex-1">
          <p class="truncate text-m font-semibold">{heading}</p>
          <p class="truncate text-s text-dida-text-faint">{sub}</p>
        </div>
        <div class="flex shrink-0 items-center gap-1">{@render barTransport()}</div>
      </div>
      <!-- progress -->
      {#if stream}
        <div class="flex items-center justify-center gap-2 text-s font-semibold tracking-wider {playing ? 'text-dida-danger' : 'text-dida-text-faint'}">
          {#if playing}<span class="inline-block size-2 animate-pulse rounded-full bg-dida-danger"></span>{t("media.live")}{:else}{transportLabel(xport)}{/if}
        </div>
      {:else}
        <div class="flex items-center gap-2">
          <span class="w-9 shrink-0 text-right font-mono text-xs tabular-nums text-dida-text-faint">{fmtTime(position)}</span>
          <div class="h-1 flex-1 overflow-hidden rounded-full bg-dida-border"><div class="h-full rounded-full bg-dida-accent transition-[width] duration-1000 ease-linear" style="width:{pct}%"></div></div>
          <span class="w-9 shrink-0 font-mono text-xs tabular-nums text-dida-text-faint">{fmtTime(duration)}</span>
        </div>
      {/if}
      <!-- view tabs (appear by availability) -->
      <div class="self-center"><Picks picks={viewTabs} chosen={[view]} onpick={(k) => (view = k as View)} face={viewTab} /></div>
    </div>

    <!-- CONTENT (scrolls within the fixed height). -->
    <div bind:this={lyricsBox} class="relative min-h-0 flex-1 overflow-y-auto scroll-smooth">
      {#if view === "normal"}
        <!-- Cover fills the content area (height-bound square), so it scales up
             with the card instead of a fixed small max-width. -->
        <div class="flex h-full items-center justify-center p-3">
          <div class="aspect-square h-full max-h-full max-w-full overflow-hidden rounded-xl shadow-2xl ring-1 ring-black/10">{@render artTile(true)}</div>
        </div>
      {:else if view === "lyrics"}
        <div class="px-4 py-6 text-center leading-relaxed">
          {#if lyricsLoading}<span class="text-m text-dida-text-muted">{t("common.loading")}</span>
          {:else if syncedLines.length}
            {#each syncedLines as line, i (i)}
              <p data-li={i} class="py-1 text-xl transition-all duration-300 {i === activeLine ? 'font-semibold text-dida-accent' : i < activeLine ? 'text-dida-text-faint/60' : 'text-dida-text-muted'}">{line.text || "♪"}</p>
            {/each}
          {:else if lyricsErr || !lyrics}<span class="text-m text-dida-text-muted">{t("media.noLyrics")}</span>
          {:else}<div class="whitespace-pre-line text-l text-dida-text-muted">{lyrics}</div>{/if}
        </div>
      {:else if view === "queue"}
        {#if queueData.items.length}
          <ul class="divide-y divide-dida-border/40">
            {#each queueData.items as tr, i (i)}
              <li>
                <button type="button" onclick={() => playFrom(i)}
                        class="flex w-full items-center gap-3 px-3 py-2 text-left transition hover:bg-dida-panel-2/50 {i === queueData.current ? 'bg-dida-accent/10' : ''}">
                  <span class="grid w-5 shrink-0 place-items-center">
                    {#if i === queueData.current}<svg viewBox="0 0 24 24" fill="currentColor" class="size-3.5 text-dida-accent"><path d="M8 5v14l11-7z" /></svg>
                    {:else}<span class="font-mono text-s text-dida-text-faint">{i + 1}</span>{/if}
                  </span>
                  <span class="min-w-0 flex-1 truncate text-m {i === queueData.current ? 'font-semibold text-dida-accent' : ''}">{tr.title}</span>
                  <span class="hidden max-w-[40%] flex-1 truncate text-right text-s text-dida-text-faint sm:block">{tr.artist}</span>
                </button>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="px-4 py-10 text-center text-m text-dida-text-faint">{t("media.queueEmpty")}</p>
        {/if}
      {:else if view === "pipeline"}
        <div class="p-3"><SignalPath entityId={device.entityId} trackKey={`${heading}|${quality ?? ""}`} /></div>
      {/if}
    </div>

    {#if err}<p class="relative px-3 pb-2 text-center text-s text-dida-danger">{err}</p>{/if}
  </div>
{/if}
