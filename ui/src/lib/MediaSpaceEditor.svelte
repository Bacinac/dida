<script lang="ts">
  // Inline source editor for ONE space at a time (Living Room / Outdoor). A source
  // maps an experience to its mechanism — a Harmony activity (internal AV) or a
  // streaming player optionally routed through an AVR zone (external). Every field
  // is a dropdown fed from the LIVE device list, so the mapping can't be mistyped.
  // Opened from the MediaHub by an admin; not a standalone settings page.
  import { untrack } from "svelte";
  import { Button, SaveButton } from "$lib/kit";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { ICONS } from "$lib/deviceIcons";
  import { isAvrZone, sourceOptions } from "$lib/media";
  import { deriveSources } from "$lib/mediaSources";
  import { devices, type Device } from "$lib/store.svelte";
  import VolumePresetEditor from "$lib/VolumePresetEditor.svelte";
  import type { MediaSource } from "$lib/api";

  let { initialArea, onclose }: { initialArea: number | null; onclose: () => void } = $props();

  const areas = $derived(devices.sortedAreas);
  const remotes = $derived(devices.list.filter((d) => d.deviceType === "remote" && d.capabilities.includes("source")));
  const players = $derived(devices.mediaPlayers);
  const zones = $derived(devices.list.filter(isAvrZone).sort((a, b) => a.name.localeCompare(b.name, "hr")));

  const MEDIA_ICONS = ["tv", "player", "media", "speaker", "avr", "remote", "camera"];
  const BROWSE = ["opus", "radio"];
  const browseLabel = (b: string): string => (b === "opus" ? t("nav.library") : t("nav.radio"));

  // The room beside the name — two same-named players in two rooms must read apart.
  const withRoom = (d: Device): string =>
    d.areaId !== null ? `${d.name} · ${devices.areaName(d.areaId)}` : d.name;

  const activitiesOf = (remoteId: string | undefined): string[] => {
    const d = remoteId ? devices.byId[remoteId] : null;
    return d ? sourceOptions(d).filter((x) => x !== "PowerOff") : [];
  };
  const inputsOf = (zoneId: string | undefined): string[] => {
    const d = zoneId ? devices.byId[zoneId] : null;
    return d ? sourceOptions(d) : [];
  };
  // Live-TV tuning: the now-playing device (a Shield) that can type channel numbers.
  const npDev = (s: MediaSource): Device | null => (s.nowplaying ? devices.byId[s.nowplaying] ?? null : null);
  const canTune = (s: MediaSource): boolean => !!npDev(s)?.capabilities.includes("channel");
  const appsOf = (id: string | null | undefined): string[] => {
    const d = id ? devices.byId[id] : null;
    return d ? sourceOptions(d) : [];
  };

  // Seed from the space the hub was showing; then the dropdown owns it. Seeded via
  // an effect (not $state(initialArea)) so it isn't a stale prop capture.
  let areaId = $state<number | null>(null);
  $effect(() => {
    if (areaId !== null) return;
    areaId = initialArea ?? areas.find((a) => (a.media_config?.sources?.length ?? 0) > 0)?.id ?? areas[0]?.id ?? null;
  });

  const savedOf = (id: number): MediaSource[] => {
    const a = devices.areas.find((x) => x.id === id);
    return a?.media_config?.sources ? JSON.parse(JSON.stringify(a.media_config.sources)) : [];
  };

  // One draft per space so switching spaces keeps unsaved edits. `drafts` is read
  // via untrack so seeding it doesn't re-trigger this effect (no update-depth loop).
  let drafts = $state<Record<number, MediaSource[]>>({});
  $effect(() => {
    const id = areaId;
    if (id === null) return;
    if (!untrack(() => id in drafts)) drafts[id] = savedOf(id);
  });
  const list = $derived(areaId !== null ? drafts[areaId] ?? [] : []);
  const dirty = $derived(
    areaId !== null &&
      JSON.stringify(drafts[areaId] ?? []) !== JSON.stringify(devices.areas.find((a) => a.id === areaId)?.media_config?.sources ?? []),
  );

  let saving = $state(false);
  let err = $state<string | null>(null);

  const newKey = (): string => "src-" + Math.random().toString(36).slice(2, 8);
  function addSource(): void {
    if (areaId === null) return;
    drafts[areaId] = [...(drafts[areaId] ?? []), { key: newKey(), label: t("media.newSource"), kind: "player", browse: ["opus", "radio"] }];
  }
  function generate(): void {
    if (areaId === null) return;
    // Additive + de-duped: append Harmony activities not already in this space,
    // so it never wipes a hand-added streaming source.
    const have = new Set((drafts[areaId] ?? []).filter((s) => s.kind === "activity").map((s) => `${s.remote}|${s.activity}`));
    const add = deriveSources().filter((s) => !have.has(`${s.remote}|${s.activity}`));
    if (add.length) drafts[areaId] = [...(drafts[areaId] ?? []), ...add];
  }
  function move(i: number, dir: -1 | 1): void {
    if (areaId === null) return;
    const l = [...(drafts[areaId] ?? [])];
    const j = i + dir;
    if (j < 0 || j >= l.length) return;
    [l[i], l[j]] = [l[j], l[i]];
    drafts[areaId] = l;
  }
  function toggleBrowse(s: MediaSource, b: string): void {
    const set = new Set(s.browse ?? []);
    if (set.has(b)) set.delete(b);
    else set.add(b);
    s.browse = BROWSE.filter((x) => set.has(x)); // canonical order
  }

  async function save(): Promise<void> {
    if (areaId === null) return;
    saving = true;
    err = null;
    try {
      const sources = drafts[areaId] ?? [];
      await devices.setAreaMediaConfig(areaId, sources.length ? { sources } : null);
      drafts[areaId] = savedOf(areaId); // clear dirty from the fresh saved value
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = false;
    }
  }
</script>

<div class="space-y-4">
  <!-- header: which space + Save/Done — ALWAYS pinned at the top of the editor -->
  <div class="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 border-b border-dida-border bg-dida-bg py-2">
    <label class="flex items-center gap-2 text-m">
      <span class="text-dida-text-muted">{t("media.space")}</span>
      <select
        value={areaId ?? ""} onchange={(e) => (areaId = Number((e.currentTarget as HTMLSelectElement).value))}
       
      >
        {#each areas as a (a.id)}
          <option value={a.id}>{devices.roomLabel(a)}{(a.media_config?.sources?.length ?? 0) > 0 ? " ●" : ""}</option>
        {/each}
      </select>
    </label>
    <div class="flex items-center gap-1.5">
      <SaveButton size="small" {dirty} {saving} onclick={save} />
      <Button size="small" onclick={onclose}>{t("common.done")}</Button>
    </div>
  </div>

  <!-- Global volume presets (Quiet/Normal/Loud …) — apply to every zone's volume row. -->
  <VolumePresetEditor />

  {#if err}<p class="text-m text-dida-danger">{err}</p>{/if}

  {#if !list.length}
    <p class="py-4 text-center text-m text-dida-text-faint">{t("media.noSourcesRoom")}</p>
  {/if}

  <div class="space-y-3">
    {#each list as s, i (s.key)}
      <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3 transition-opacity {s.enabled === false ? 'opacity-60' : ''}">
        <!-- Row 1: show/hide toggle · rename (Naziv) · reorder/delete -->
        <div class="flex items-center gap-2">
          <input
            type="checkbox" checked={s.enabled !== false}
            onchange={(e) => (s.enabled = e.currentTarget.checked ? undefined : false)}
            title={t("media.sourceVisible")} aria-label={t("media.sourceVisible")}
            class="size-4 shrink-0 accent-dida-accent"
          />
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" class="size-5 shrink-0 text-dida-text-muted">{@html ICONS[s.icon ?? (s.kind === "activity" ? "tv" : "player")] ?? ICONS.media}</svg>
          <label class="flex min-w-0 flex-1 items-center gap-2">
            <span class="shrink-0 text-s text-dida-text-muted">{t("media.sourceLabel")}</span>
            <input
              bind:value={s.label} placeholder={t("media.sourceLabel")}
              class="min-w-0 flex-1"
            />
          </label>
          <div class="flex shrink-0 items-center gap-0.5">
            <Button size="small" label={t("common.moveUp")} title={t("common.moveUp")} disabled={i === 0} onclick={() => move(i, -1)}>↑</Button>
            <Button size="small" label={t("common.moveDown")} title={t("common.moveDown")} disabled={i === list.length - 1} onclick={() => move(i, 1)}>↓</Button>
          </div>
        </div>
        <!-- Row 2: icon · kind -->
        <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
          <label class="flex items-center gap-1.5 text-s text-dida-text-muted">
            <span>{t("media.icon")}</span>
            <select bind:value={s.icon}>
              <option value={undefined}>{t("media.iconAuto")}</option>
              {#each MEDIA_ICONS as ic (ic)}<option value={ic}>{ic}</option>{/each}
            </select>
          </label>
          <label class="flex items-center gap-1.5 text-s text-dida-text-muted">
            <span>{t("media.kind")}</span>
            <select bind:value={s.kind}>
              <option value="activity">{t("media.kindActivity")}</option>
              <option value="player">{t("media.kindPlayer")}</option>
            </select>
          </label>
        </div>

        {#if s.kind === "activity"}
          <div class="mt-2 grid gap-2 sm:grid-cols-3">
            <label class="text-s text-dida-text-muted">{t("media.remote")}
              <select bind:value={s.remote} class="mt-0.5 w-full">
                <option value={undefined}>—</option>
                {#each remotes as r (r.entityId)}<option value={r.entityId}>{withRoom(r)}</option>{/each}
              </select>
            </label>
            <label class="text-s text-dida-text-muted">{t("media.activity")}
              <select bind:value={s.activity} class="mt-0.5 w-full">
                <option value={undefined}>—</option>
                {#each activitiesOf(s.remote) as act (act)}<option value={act}>{act}</option>{/each}
              </select>
            </label>
            <label class="text-s text-dida-text-muted">{t("media.nowPlayingFrom")}
              <select bind:value={s.nowplaying} class="mt-0.5 w-full">
                <option value={null}>{t("media.none")}</option>
                {#each players as p (p.entityId)}<option value={p.entityId}>{withRoom(p)}</option>{/each}
              </select>
            </label>
          </div>
        {:else}
          <div class="mt-2 grid gap-2 sm:grid-cols-3">
            <label class="text-s text-dida-text-muted">{t("media.player")}
              <select bind:value={s.player} class="mt-0.5 w-full">
                <option value={undefined}>—</option>
                {#each players as p (p.entityId)}<option value={p.entityId}>{withRoom(p)}</option>{/each}
              </select>
            </label>
            <label class="text-s text-dida-text-muted">{t("media.zone")}
              <select bind:value={s.zone} class="mt-0.5 w-full">
                <option value={undefined}>{t("media.none")}</option>
                {#each zones as z (z.entityId)}<option value={z.entityId}>{withRoom(z)}</option>{/each}
              </select>
            </label>
            {#if s.zone}
              <label class="text-s text-dida-text-muted">{t("media.zoneInput")}
                <select bind:value={s.zone_input} class="mt-0.5 w-full">
                  <option value={undefined}>—</option>
                  {#each inputsOf(s.zone) as inp (inp)}<option value={inp}>{inp}</option>{/each}
                </select>
              </label>
            {/if}
          </div>
        {/if}

        <!-- Browse tabs — for any source backed by a DIDA player (an activity's
             now-playing device, or the player). ZEN drives the iFi → the shelf / radio. -->
        {#if (s.kind === "player" && s.player) || (s.kind === "activity" && s.nowplaying)}
          <div class="mt-2">
            <span class="text-s text-dida-text-muted">{t("media.browseTabs")}</span>
            <div class="mt-1 flex flex-wrap gap-1.5">
              {#each BROWSE as b (b)}
                <Button size="small" selected={(s.browse ?? []).includes(b)} onclick={() => toggleBrowse(s, b)}>{browseLabel(b)}</Button>
              {/each}
            </div>
          </div>
        {/if}

        <!-- Live-TV channels — when the now-playing device is a Shield that can tune
             (Xplore). Number = position in this list; one channel per line. -->
        {#if canTune(s)}
          <div class="mt-2 space-y-2">
            <label class="flex flex-wrap items-center gap-2 text-s text-dida-text-muted">
              <span>{t("media.tvApp")}</span>
              <select bind:value={s.tv_app}>
                <option value={undefined}>{t("media.none")}</option>
                {#each appsOf(s.nowplaying) as app (app)}<option value={app}>{app}</option>{/each}
              </select>
            </label>
            <label class="block text-s text-dida-text-muted">
              {t("media.channels")}
              <textarea
                value={(s.channels ?? []).join("\n")}
                onchange={(e) => (s.channels = e.currentTarget.value.split("\n").map((x) => x.trim()).filter(Boolean))}
                rows="6" placeholder={t("media.channelsHint")}
                class="mt-1 w-full font-mono"
              ></textarea>
            </label>
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="flex flex-wrap gap-2">
    <Button size="small" onclick={addSource}>+ {t("media.addSource")}</Button>
    <Button size="small" onclick={generate}>{t("media.generate")}</Button>
  </div>
</div>
