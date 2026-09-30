<script lang="ts">
  // The OPUS shelf: artists → an artist's records → a record's songs. Picking
  // a song puts the whole record on the chosen player from there on (the device
  // fetches the sound from OPUS itself). The shelf is asked on the spot — the
  // house keeps no copy of it.
  import { onMount, type Snippet } from "svelte";
  import { api, type OpusArtist, type OpusArtistDetail, type OpusRelease } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { Button, Tag } from "$lib/kit";
  import { fmtTime, gradient } from "$lib/media";
  import type { Device } from "$lib/store.svelte";

  let { player, controls, onplayed }: { player: Device | null; controls?: Snippet; onplayed?: () => void } = $props();

  let q = $state("");
  let artists = $state<OpusArtist[]>([]);
  let artist = $state<OpusArtistDetail | null>(null);
  let release = $state<OpusRelease | null>(null);
  let bioOpen = $state(false);
  let bioEl = $state<HTMLElement | null>(null);
  let bioOverflows = $state(false);
  $effect(() => {
    void artist;
    if (bioOpen) return;
    const el = bioEl;
    if (!el) return;
    requestAnimationFrame(() => {
      bioOverflows = el.scrollHeight - el.clientHeight > 2;
    });
  });
  let loading = $state(false);
  let err = $state<string | null>(null);
  let nowId = $state<number | null>(null);
  let failed = $state<Record<string, boolean>>({});

  // The shelf is a few hundred names: filtered here, so typing costs nothing.
  const shown = $derived.by(() => {
    const needle = q.trim().toLocaleLowerCase("hr");
    return needle ? artists.filter((a) => a.name.toLocaleLowerCase("hr").includes(needle)) : artists;
  });

  async function loadArtists(): Promise<void> {
    loading = true;
    err = null;
    try {
      artists = await api.opusArtists();
    } catch (e) {
      err = errMsg(e);
    } finally {
      loading = false;
    }
  }
  onMount(loadArtists);

  async function openArtist(a: OpusArtist): Promise<void> {
    err = null;
    release = null;
    bioOpen = false;
    loading = true;
    try {
      artist = await api.opusArtist(a.id);
    } catch (e) {
      err = errMsg(e);
    } finally {
      loading = false;
    }
  }
  async function openRelease(id: number): Promise<void> {
    err = null;
    failed["__d"] = false;
    loading = true;
    try {
      release = await api.opusRelease(id);
    } catch (e) {
      err = errMsg(e);
    } finally {
      loading = false;
    }
  }
  function back(): void {
    if (release) {
      release = null;
      return;
    }
    artist = null;
  }

  // Play the record from `start` (the rest follows, gapless, on the device).
  async function playFrom(start: number): Promise<void> {
    if (!player || !release || !release.tracks.length) return;
    err = null;
    nowId = release.tracks[start]?.id ?? null;
    try {
      await api.opusPlay({ player_id: player.entityId, kind: "release", id: release.id, start });
      onplayed?.();
    } catch (e) {
      err = errMsg(e);
    }
  }

  const heldLabel = (n: number): string => `${n} ${t(n === 1 ? "media.albumOne" : "media.albumMany")}`;
  const art = (u: string | null | undefined, w: number): string | null => api.opusArt(u, w);
</script>

<div>
  <div class="sticky top-0 z-10 -mx-1 mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-dida-border bg-dida-panel/95 px-3 py-2 backdrop-blur">
    <input
      bind:value={q} placeholder={t("media.searchMusic")}
      class="min-w-[8rem] flex-1"
    />
    {#if controls}<div class="w-full shrink-0 sm:w-64">{@render controls()}</div>{/if}
  </div>

  {#if artist || release}
    <div class="mb-3 flex min-w-0 items-center gap-1.5 text-m">
      <Button size="small" onclick={back} label={t("common.back")}>←</Button>
      {#if artist}<span class="truncate font-medium">{artist.name}</span>{/if}
      {#if release}<span class="shrink-0 text-dida-text-faint">›</span><span class="truncate text-dida-text-muted">{release.title}</span>{/if}
    </div>
  {/if}

  {#if err}<p class="mb-2 text-s text-dida-danger">{err}</p>{/if}
  {#if !player}<p class="mb-2 text-s text-dida-text-faint">{t("media.noPlayers")}</p>{/if}

  {#if release}
    <div class="flex items-start gap-3">
      <div class="size-24 shrink-0 overflow-hidden rounded-lg">
        {#if art(release.cover_url, 256) && !failed["__d"]}
          <img src={art(release.cover_url, 256)} alt="" onerror={() => (failed["__d"] = true)} class="h-full w-full object-cover" />
        {:else}
          <div class="h-full w-full" style="background:{gradient(release.title)}"></div>
        {/if}
      </div>
      <div class="min-w-0">
        <h3 class="truncate font-semibold">{release.title}</h3>
        <p class="truncate text-m text-dida-text-muted">{release.artist}{release.release_date ? ` · ${release.release_date.slice(0, 4)}` : ""}</p>
        <p class="text-s text-dida-text-faint">{release.tracks.length} {t("media.tracks")}</p>
        <div class="mt-2"><Button tone="primary" onclick={() => playFrom(0)} disabled={!player}>
          <svg viewBox="0 0 24 24" fill="currentColor" class="size-4"><path d="M8 5v14l11-7z" /></svg>{t("media.playAlbum")}
        </Button></div>
      </div>
    </div>
    <ul class="mt-3 divide-y divide-dida-border/60">
      {#each release.tracks as tr, i (tr.id)}
        <li>
          <button
            type="button" onclick={() => playFrom(i)} disabled={!player}
            class="flex w-full items-center gap-3 py-1.5 text-left hover:text-dida-accent disabled:opacity-40 {tr.id === nowId ? 'text-dida-accent' : ''}"
          >
            <span class="w-6 shrink-0 text-right font-mono text-s text-dida-text-faint">{tr.position ?? "·"}</span>
            <span class="min-w-0 flex-1 truncate text-m">{tr.title}</span>
            {#if (tr.channels ?? 0) > 2}<Tag tone="quiet" kind="hires">{tr.channels}ch</Tag>{/if}
            {#if tr.duration_s}<span class="shrink-0 font-mono text-s text-dida-text-faint">{fmtTime(tr.duration_s)}</span>{/if}
          </button>
        </li>
      {/each}
    </ul>
  {:else if artist}
    {@const span = artist.begin_year ? `${artist.begin_year}${artist.end_year ? `–${artist.end_year}` : ""}` : null}
    {@const meta = [artist.country, span].filter(Boolean).join(" · ")}
    {#if artist.image || artist.bio || meta}
      <div class="mb-5 flex gap-4 rounded-2xl border border-dida-border bg-dida-panel-2 p-4 sm:gap-5 sm:p-5">
        {#if art(artist.image, 512)}
          <img src={art(artist.image, 512)} alt="" class="size-44 shrink-0 self-start rounded-xl object-cover shadow-md ring-1 ring-dida-border sm:size-52" />
        {/if}
        <div class="flex min-w-0 flex-1 flex-col {bioOpen ? '' : 'h-44 overflow-hidden sm:h-52'}">
          <h2 class="text-2xl font-bold tracking-tight">{artist.name}</h2>
          {#if meta}<p class="mt-0.5 text-m text-dida-text-muted">{meta}</p>{/if}
          {#if artist.bio}
            <button
              bind:this={bioEl}
              type="button" onclick={() => (bioOverflows || bioOpen) && (bioOpen = !bioOpen)}
              class="relative mt-2 min-h-0 flex-1 text-left text-m leading-relaxed text-dida-text-muted {bioOverflows || bioOpen ? 'cursor-pointer' : 'cursor-default'} {bioOpen ? '' : 'overflow-hidden'}"
            >
              <span class="block">{artist.bio}</span>
              {#if bioOverflows && !bioOpen}
                <span class="pointer-events-none absolute inset-x-0 bottom-0 block h-8 bg-gradient-to-t from-dida-panel-2 to-transparent"></span>
              {/if}
            </button>
          {/if}
        </div>
      </div>
    {/if}
    {#if loading && !artist.releases.length}
      <p class="py-6 text-center text-m text-dida-text-faint">{t("common.loading")}</p>
    {:else if !artist.releases.length}
      <p class="py-6 text-center text-m text-dida-text-faint">{t("media.noMusic")}</p>
    {:else}
      <div class="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-5">
        {#each artist.releases as r (r.id)}
          <button type="button" onclick={() => openRelease(r.id)} class="group text-left">
            <div class="aspect-square w-full overflow-hidden rounded-lg">
              {#if art(r.cover, 256) && !failed[`r${r.id}`]}
                <img src={art(r.cover, 256)} alt="" loading="lazy" onerror={() => (failed[`r${r.id}`] = true)} class="h-full w-full object-cover transition group-hover:opacity-90" />
              {:else}
                <div class="grid h-full w-full place-items-center" style="background:{gradient(r.title)}">
                  <svg viewBox="0 0 24 24" fill="currentColor" class="size-8 text-white/80"><path d="M12 3v10.55A4 4 0 1 0 14 17V7h4V3h-6z" /></svg>
                </div>
              {/if}
            </div>
            <p class="mt-1 truncate text-s font-medium">{r.title}</p>
            <p class="truncate text-xs text-dida-text-faint">{r.year}{r.tracks ? ` · ${r.tracks} ${t("media.tracks")}` : ""}</p>
          </button>
        {/each}
      </div>
    {/if}
  {:else if loading && artists.length === 0}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("common.loading")}</p>
  {:else if shown.length === 0}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("media.noMusic")}</p>
  {:else}
    <div class="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">
      {#each shown as a (a.id)}
        <button type="button" onclick={() => openArtist(a)} class="group text-center">
          <div class="mx-auto aspect-square w-full overflow-hidden rounded-full ring-1 ring-dida-border">
            {#if art(a.image, 256) && !failed[`a${a.id}`]}
              <img src={art(a.image, 256)} alt="" loading="lazy" onerror={() => (failed[`a${a.id}`] = true)} class="h-full w-full object-cover transition group-hover:opacity-90" />
            {:else}
              <div class="grid h-full w-full place-items-center" style="background:{gradient(a.name)}">
                <svg viewBox="0 0 24 24" fill="currentColor" class="size-8 text-white/80"><path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10zm0 2c-5 0-9 2.5-9 6v2h18v-2c0-3.5-4-6-9-6z" /></svg>
              </div>
            {/if}
          </div>
          <p class="mt-1 truncate text-s font-medium">{a.name}</p>
          <p class="truncate text-xs text-dida-text-faint">{heldLabel(a.held)}</p>
        </button>
      {/each}
    </div>
  {/if}
</div>
