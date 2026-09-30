<script lang="ts">
  // MediaHub — one unified media surface. A space selector (Living Room / Outdoor)
  // picks WHERE, a source picker picks WHAT, and the body morphs to it: a
  // now-playing card + browse for a streaming player, a remote (+ now-playing when
  // the source has a screen) for an AV activity. The mechanism — a Harmony activity
  // (internal, keeps the physical remote in sync) vs an AVR-zone route (external) —
  // stays invisible.
  //
  // No `areaId` → every configured space (the media page). An `areaId` → just that
  // space (a floorplan room). Admins curate the sources inline (no settings page).
  import { api, type OpusTvItem, type OpusTvLink } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { ICONS } from "$lib/deviceIcons";
  import { errMsg } from "$lib/errors";
  import { Button, Dialog, Picks, i18n, toasts } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import Library from "$lib/Library.svelte";
  import { mediaSource, mediaTitle, setSource, sourceOptions, sourceValue, transport } from "$lib/media";
  import { mediaSpaces, resolvedSources, selectSource, sourceIcon, type ResolvedSource } from "$lib/mediaSources";
  import MediaPlayer from "$lib/MediaPlayer.svelte";
  import MediaSpaceEditor from "$lib/MediaSpaceEditor.svelte";
  import VolumeBar from "$lib/VolumeBar.svelte";
  import RadioBrowse from "$lib/RadioBrowse.svelte";
  import Remote from "$lib/Remote.svelte";
  import { remoteButtons } from "$lib/remote";
  import TvScreen from "$lib/TvScreen.svelte";
  import { volume, volumeColor } from "$lib/volume.svelte";
  import { devices } from "$lib/store.svelte";

  let { areaId }: { areaId?: number } = $props();

  const readOnly = $derived(!auth.canControl);
  let editing = $state(false);

  // Spaces = the areas that have configured sources; scoped mode pins to one.
  const spaces = $derived(areaId !== undefined ? mediaSpaces().filter((s) => s.areaId === areaId) : mediaSpaces());
  const allSources = $derived(resolvedSources(areaId));
  const liveSpace = $derived(allSources.find((s) => s.active)?.areaId ?? null);

  let space = $state<number | null>(null);
  const activeSpace = $derived(
    space !== null && spaces.some((s) => s.areaId === space) ? space : liveSpace ?? spaces[0]?.areaId ?? null,
  );
  const spaceSources = $derived(allSources.filter((s) => s.areaId === activeSpace));
  const spaceTabs = $derived(spaces.map((sp) => ({ key: String(sp.areaId), label: sp.name })));

  // Is a space powered/playing? A green dot on its tab answers "is anything on?"
  // without a separate status line — a source active (AVR zone/activity live).
  const spaceOn = (id: number): boolean => allSources.some((s) => s.areaId === id && s.active);

  // The current volume of a space's AVR zone — drives the green→red tint on the
  // active tab so a loud space reads red at a glance. null when it has no zone.
  function spaceZoneVol(id: number): number | null {
    const src = allSources.find((s) => s.areaId === id && (s.zone ?? s.avr));
    const z = src?.zone ?? src?.avr;
    const v = z?.caps["volume"]?.value;
    return typeof v === "number" ? v : null;
  }

  let pickedKey = $state<string | null>(null);
  const selected = $derived(
    spaceSources.find((s) => s.src.key === pickedKey) ?? spaceSources.find((s) => s.active) ?? spaceSources[0] ?? null,
  );

  // A source can be BOTH a Harmony activity (power/route) and a DIDA player (browse +
  // transport) — e.g. ZEN = the iFi on the ZEN input, which plays the shelf and radio.
  // transportTarget is that player (nowplaying for an activity, the player for a
  // player source); volumeTarget is the AVR zone when the source routes through one.
  // Both halves have to hold: the box's app name is only the app table's label,
  // and the player cannot say which box it is open on.
  const opusTv = $derived(devices.byId["opus:tv"] ?? null);
  const onOpus = $derived.by(() => {
    const box = selected?.nowplaying;
    if (!box || box.adapter !== "androidtv" || !opusTv) return false;
    return /opus/i.test(sourceValue(box) ?? "") && transport(opusTv) !== "idle";
  });
  const transportTarget = $derived.by(() => {
    const d = selected?.src.kind === "player" ? selected.player : selected?.nowplaying;
    return d && "media_transport" in d.caps ? d : null;
  });

  // OPUS in front: its own places and what is in the one picked, in the row the
  // live-TV channels stand in. A place sends the television there; a thing in
  // it opens on the television the way a press on the television would.
  let opusLinks = $state<OpusTvLink[]>([]);
  let opusPlace = $state("");
  let opusItems = $state<OpusTvItem[]>([]);
  const placeLabel = (key: string): string => t(`opus.place.${key}` as MessageKey);
  const opusPlaces = $derived([...opusLinks].sort((a, b) => placeLabel(a.key).localeCompare(placeLabel(b.key), "hr")));
  const opusNow = $derived(
    onOpus && opusTv && ["playing", "paused"].includes(transport(opusTv)) ? mediaTitle(opusTv) : null,
  );
  $effect(() => {
    if (!onOpus) {
      opusPlace = "";
      opusItems = [];
      return;
    }
    if (opusLinks.length) return;
    api.opusTvLinks().then((links) => (opusLinks = links)).catch((e) => toasts.error(errMsg(e)));
  });
  async function goPlace(key: string): Promise<void> {
    opusPlace = key;
    opusItems = [];
    if (!key || readOnly) return;
    busy = true;
    try {
      await api.opusTvOpen({ link: key });
      if (opusLinks.find((l) => l.key === key)?.items) opusItems = await api.opusTvItems(key, i18n.locale);
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  async function openItem(value: string): Promise<void> {
    const [kind, id] = value.split(":");
    if (!kind || !id || readOnly) return;
    busy = true;
    try {
      await api.opusTvOpen({ kind, id: Number(id), lang: i18n.locale });
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  // Volume lives on the AVR that carries the source: a source's own zone (patio =
  // Marantz Zone 2) or, when it routes through the Harmony-managed main zone with no
  // zone of its own (living room = Marantz MAIN), its `avr`. Applies to players AND
  // activities — every living-room source shares avr=denon:marantz_main.
  const volumeTarget = $derived(selected ? selected.zone ?? selected.avr : null);

  // Primary structured control: a source whose device exposes a picker gets a
  // dependent dropdown (the Shield's installed apps via androidtv `source` — pick
  // "A1 Xplore TV" and it launches). The D-pad remote is only a secondary overlay.
  const optionDevice = $derived.by(() => {
    const d = selected?.src.kind === "player" ? selected.player : selected?.nowplaying;
    return d && d.capabilities.includes("source") ? d : null;
  });
  const optionValues = $derived(optionDevice ? sourceOptions(optionDevice) : []);
  const optionCurrent = $derived(optionDevice ? sourceValue(optionDevice) : null);
  const optionLabel = $derived(optionDevice?.adapter === "androidtv" ? t("media.app") : t("media.option"));

  // Curated live-TV channels (Xplore on the Shield): the tune number is the 1-based
  // position. Shown only when the device can tune AND its current app is the TV app.
  const tvChannels = $derived.by(() => {
    const src = selected?.src;
    if (!optionDevice?.capabilities.includes("channel") || !src?.channels?.length) return [];
    // Hide only when we KNOW the current app is a different one (app detection is
    // flaky on some devices → don't hide the picker just because it's unknown).
    if (src.tv_app && optionCurrent && optionCurrent !== src.tv_app) return [];
    return src.channels;
  });
  async function tune(number: string): Promise<void> {
    if (!optionDevice || readOnly || !number) return;
    busy = true;
    try {
      await api.sendCommand({ entity_id: optionDevice.entityId, capability: "channel", command: "set_channel", args: { value: number } });
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  // The live-TV channel DIDA last tuned (the androidtv publishes `channel`) → its name,
  // shown on the screen since a DRM app (Xplore) blacks out the screencap.
  const tunedChannel = $derived.by(() => {
    const src = selected?.src;
    const num = transportTarget?.caps["channel"]?.value;
    if (!src?.channels?.length || num == null || num === "") return null;
    // Stale once a different app is foregrounded — only label while the TV app is on.
    if (src.tv_app && optionCurrent && optionCurrent !== src.tv_app) return null;
    return src.channels[Number(num) - 1] ?? null;
  });

  function pickSpace(id: number): void {
    // Consistent with the source pills: clicking the space you're viewing, while
    // it's on, powers it down (its zone + the playing source). Clicking any other
    // space just switches to it — turning that space on is done by picking a source.
    if (id === activeSpace && spaceOn(id) && !readOnly) {
      void turnOffSpace(id);
      return;
    }
    space = id;
    pickedKey = null; // default to whatever's live/first in the new space
    remoteOpen = false;
  }
  async function turnOffSpace(id: number): Promise<void> {
    busy = true;
    try {
      for (const s of allSources.filter((x) => x.areaId === id && x.active)) await turnOffSource(s);
      toasts.success(t("media.turnedOff", { name: spaces.find((s) => s.areaId === id)?.name ?? "" }));
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }

  let busy = $state(false);
  async function pick(s: ResolvedSource): Promise<void> {
    pickedKey = s.src.key;
    remoteOpen = false;
    if (readOnly) return; // view-only: reflect, don't actuate
    busy = true;
    // Clicking the LIVE source turns it off (Harmony PowerOff for an activity, the
    // AVR zone for a player) — the pill IS the on/off toggle, no separate button.
    const off = s.active;
    try {
      if (off) await turnOffSource(s);
      else await selectSource(s);
      toasts.success(t(off ? "media.turnedOff" : "media.turnedOn", { name: s.src.label }));
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  async function turnOffSource(s: ResolvedSource): Promise<void> {
    // Power path mirrors selectSource/computeActive: Harmony (remote+activity) vs a
    // direct AVR zone. Then stop the player so it isn't left spinning into a dead zone.
    if (s.src.remote && s.src.activity) {
      // End the Harmony activity, then make sure the AVR main zone it left on goes
      // to standby too (Harmony's activity power-off doesn't switch it off).
      await api.sendCommand({ entity_id: s.src.remote, capability: "source", command: "set_source", args: { value: "PowerOff" } });
      if (s.src.avr) await api.sendCommand({ entity_id: s.src.avr, capability: "on_off", command: "turn_off" });
      // A media box (Shield) stays on after the activity ends, so its live screen
      // keeps showing — sleep it so "off" is really off.
      if (s.src.nowplaying?.startsWith("androidtv:")) await api.sendCommand({ entity_id: s.src.nowplaying, capability: "on_off", command: "turn_off" });
    } else if (s.zone) {
      await api.sendCommand({ entity_id: s.zone.entityId, capability: "on_off", command: "turn_off" });
    }
    if (s.player) await api.sendCommand({ entity_id: s.player.entityId, capability: "media_transport", command: "pause" });
  }
  async function launchOption(value: string): Promise<void> {
    if (!optionDevice || readOnly) return;
    busy = true;
    try {
      await setSource(optionDevice.entityId, value);
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  // Open the shelf or the stations, powering the source's route on if it isn't
  // already (so picking a source turns on the amp — no separate button).
  function openBrowse(b: string): void {
    browseTab = b;
    musicBrowseOpen = true;
    if (selected && !selected.active && !readOnly) selectSource(selected).catch((e) => toasts.error(errMsg(e)));
  }

  // --- browse — for ANY source with a DIDA player (an activity like ZEN drives the
  // iFi, so it browses the shelf and the stations too, not just player sources) ---
  const browseTabs = $derived(transportTarget ? selected?.src.browse ?? [] : []);
  let browseTab = $state<string | null>(null);
  const playingTab = $derived.by(() => {
    const p = transportTarget;
    const ms = p ? mediaSource(p) : null;
    return ms === "library" ? "opus" : ms === "radio" ? "radio" : null;
  });
  const curBrowse = $derived(
    browseTab && browseTabs.includes(browseTab)
      ? browseTab
      : playingTab && browseTabs.includes(playingTab)
        ? playingTab
        : browseTabs[0] ?? null,
  );
  const browseLabel = (b: string): string => (b === "opus" ? t("nav.library") : b === "radio" ? t("nav.radio") : b);

  // The remote is a facade: available whenever ANYTHING is controllable — Harmony
  // keys, a DIDA player (transport), or an AVR zone (volume). Keys route per-function.
  const hasRemote = $derived(
    !!selected &&
      (Object.keys(remoteButtons(selected.remoteMembers)).length > 0 || !!transportTarget || !!volumeTarget),
  );
  let remoteOpen = $state(false);

  // The shelf and the stations in a modal, launched from a top selector; a pick
  // plays and closes it.
  let musicBrowseOpen = $state(false);
  $effect(() => {
    void volume.load(); // idempotent — the global preset list for the zone volume rows
  });
</script>

<svelte:window onkeydown={(e) => {
  if (e.key === "Escape" && musicBrowseOpen) musicBrowseOpen = false;
}} />

{#if editing}
  <MediaSpaceEditor initialArea={activeSpace} onclose={() => (editing = false)} />
{:else}
  <div class="flex h-full min-h-0 flex-col gap-4">
    <!-- Space selector (Living Room / Outdoor) + edit affordance -->
    {#if spaces.length}
      <div class="flex items-center justify-between gap-3">
        {#if spaces.length > 1}
          <Picks picks={spaceTabs} chosen={[String(activeSpace)]} onpick={(k) => pickSpace(Number(k))}>
            {#snippet face(tab)}
              {@const id = Number(tab.key)}
              {@const zvol = activeSpace === id && spaceOn(id) ? spaceZoneVol(id) : null}
              {tab.label}
              {#if spaceOn(id)}<span class="size-1.5 shrink-0 self-center rounded-full {String(activeSpace) === tab.key ? 'bg-dida-on-accent' : 'bg-dida-accent'}"
                style={zvol !== null ? `background-color:${volumeColor(zvol)}` : undefined} title={t("media.active")}></span>{/if}
            {/snippet}
          </Picks>
        {:else}
          <span class="text-m font-semibold">{spaces[0]?.name}</span>
        {/if}
        {#if auth.isAdmin}
          <Button size="small" onclick={() => (editing = true)} title={t("media.editSources")} label={t("media.editSources")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5"><path d="M4 20h4L18 8l-4-4L2 16v4M14 6l4 4" /></svg>
            <span class="max-sm:hidden">{t("media.editSources")}</span>
          </Button>
        {/if}
      </div>

      <!-- Zone volume — pinned above the source selection, shown only while the
           space is actually playing (a live now-playing card is up). -->
      {#if selected?.active && volumeTarget}
        <VolumeBar device={volumeTarget} {readOnly} />
      {/if}

      <!-- Source picker within the space — only when there's a real choice; a
           one-source space (patio) turns on via its shelf / radio selector. -->
      {#if spaceSources.length > 1}
        <div class="flex flex-wrap gap-1.5 *:min-w-[6rem] *:flex-1 sm:*:min-w-[7rem]">
          {#each spaceSources as s (s.src.key)}
            <Button selected={!!selected && selected.src.key === s.src.key} onclick={() => pick(s)} disabled={busy}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
                   stroke-linejoin="round" class="size-4 shrink-0">{@html ICONS[sourceIcon(s.src)] ?? ICONS.media}</svg>
              <span class="truncate">{s.src.label}</span>
              {#if s.active}<span class="size-1.5 shrink-0 rounded-full bg-dida-accent" title={t("media.active")}></span>{/if}
            </Button>
          {/each}
        </div>
      {/if}

      <!-- Structured control: the app picker + its second menu (the Shield's
           installed apps + the live-TV channel). Only while the source is ON —
           meaningless on a powered-off box. -->
      {#if selected?.active && optionDevice && optionValues.length}
        <div class="flex flex-wrap items-center gap-3">
          <label class="flex min-w-[13rem] flex-1 items-center gap-2 text-m">
            <span class="shrink-0 text-dida-text-muted">{optionLabel}</span>
            <select
              value={optionCurrent ?? ""} onchange={(e) => launchOption(e.currentTarget.value)} disabled={busy}
              class="min-w-0 flex-1"
            >
              {#if optionCurrent && !optionValues.includes(optionCurrent)}<option value={optionCurrent}>{optionCurrent}</option>{/if}
              {#each optionValues as o (o)}<option value={o}>{o}</option>{/each}
            </select>
          </label>
          {#if onOpus && opusPlaces.length}
            <label class="flex min-w-[13rem] flex-1 items-center gap-2 text-m">
              <span class="shrink-0 text-dida-text-muted">{t("media.opusPlace")}</span>
              <select
                value={opusPlace} onchange={(e) => goPlace(e.currentTarget.value)} disabled={busy}
                class="min-w-0 flex-1"
              >
                <option value="">{t("media.pickPlace")}</option>
                {#each opusPlaces as place (place.key)}<option value={place.key}>{placeLabel(place.key)}</option>{/each}
              </select>
            </label>
            {#if opusItems.length}
              <label class="flex min-w-[13rem] flex-1 items-center gap-2 text-m">
                <span class="shrink-0 text-dida-text-muted">{t(`media.opusItem.${opusPlace}` as MessageKey)}</span>
                <select
                  onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.selectedIndex = 0; openItem(v); }} disabled={busy}
                  class="min-w-0 flex-1"
                >
                  <option value="">{t("media.pickItem")}</option>
                  {#each opusItems as item (item.kind + item.id)}
                    <option value="{item.kind}:{item.id}">{item.title}{item.subtitle ? ` · ${item.subtitle}` : ""}</option>
                  {/each}
                </select>
              </label>
            {/if}
          {/if}
          {#if tvChannels.length}
            <label class="flex min-w-[13rem] flex-1 items-center gap-2 text-m">
              <span class="shrink-0 text-dida-text-muted">{t("media.tvProgram")}</span>
              <select
                onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.selectedIndex = 0; tune(v); }} disabled={busy}
                class="min-w-0 flex-1"
              >
                <option value="">{t("media.pickChannel")}</option>
                {#each tvChannels as name, i (i)}<option value={i + 1}>{i + 1} · {name}</option>{/each}
              </select>
            </label>
          {/if}
        </div>
      {/if}

      <!-- Browse launcher: the shelf / stations selector for a player source.
           Picking one powers the source's route on (e.g. the patio zone) AND opens
           the library — no separate turn-on button. -->
      {#if transportTarget && transportTarget.adapter !== "androidtv" && browseTabs.length}
        <div class="flex flex-wrap gap-1.5 *:min-w-[6rem] *:flex-1 sm:*:min-w-[7rem]">
          {#each browseTabs as b (b)}
            <Button selected={b === playingTab} onclick={() => openBrowse(b)}>{browseLabel(b)}</Button>
          {/each}
        </div>
      {/if}

      <!-- Body: the now-playing card. The remote overlay (bottom-left button) is a
           secondary surface for extra actions / navigation. -->
      {#if selected}
        {#if !selected.active}
          <!-- Source off: a clear off card + turn-on action, not a stale live view
               (a shared player like the iFi may still be spinning for another space
               / into no zone). The pill is also a toggle. -->
          <div class="grid aspect-video w-full place-items-center rounded-xl border border-dida-border bg-dida-panel-2 text-center">
            <div class="text-dida-text-faint">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" class="mx-auto size-10"><path d="M12 3v9M6.4 6.4a8 8 0 1 0 11.2 0" /></svg>
              <p class="mt-2 text-m font-medium">{selected.src.label} · {t("media.ampOff")}</p>
            </div>
          </div>
        {:else if transportTarget && transportTarget.adapter === "androidtv"}
          <TvScreen device={transportTarget} channelLabel={tunedChannel ?? opusNow} />
        {:else}
          {@const dev = selected.src.kind === "player" ? selected.player : selected.nowplaying}
          <div class="relative min-h-0 flex-1">
          {#if dev}
            <MediaPlayer device={dev} />
          {:else}
            <!-- internal source with no screen (Phono/Tuner/…), or a missing player -->
            <div class="grid h-full min-h-[14rem] max-h-[34rem] place-items-center rounded-2xl border border-dida-border bg-dida-panel text-center sm:max-h-[38rem]">
              <div>
                <p class="text-xl font-semibold">{selected.src.label}</p>
                <p class="mt-1 text-m text-dida-text-faint">
                  {selected.src.kind === "player" ? t("media.playerUnavailable") : t("media.activitySelected")}
                </p>
              </div>
            </div>
          {/if}

          <!-- Remote overlay, above the now-playing card. -->
          {#if remoteOpen && hasRemote}
            <div class="absolute inset-0 z-20 flex flex-col rounded-2xl border border-dida-accent/40 bg-dida-panel/80 p-4 backdrop-blur-sm">
              <div class="flex min-h-0 flex-1 items-center justify-center overflow-auto">
                <div class="w-full max-w-xl">
                  <Remote members={selected.remoteMembers} player={transportTarget} volumeDevice={volumeTarget} showSource={false} layout="split" />
                </div>
              </div>
            </div>
          {/if}

          <!-- Remote toggle — same bottom-left spot opens AND closes the overlay, so
               the thumb never travels. Active only for the internal (Harmony) zone. -->
          <button
            type="button" onclick={() => (remoteOpen = !remoteOpen)} disabled={!hasRemote}
            aria-label={remoteOpen ? t("common.close") : t("media.remote")}
            title={remoteOpen ? t("common.close") : t("media.remote")}
            class="absolute bottom-3 left-3 z-30 grid size-11 place-items-center rounded-full border shadow-lg backdrop-blur transition
                   disabled:opacity-30
                   {remoteOpen
                    ? 'border-dida-accent bg-dida-accent/20 text-dida-accent'
                    : 'border-dida-border bg-dida-panel-2/90 text-dida-text hover:border-dida-accent hover:text-dida-accent disabled:hover:border-dida-border disabled:hover:text-dida-text'}"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="size-5">
              {#if remoteOpen}<path d="M6 6l12 12M18 6L6 18" />{:else}{@html ICONS.remote}{/if}
            </svg>
          </button>
        </div>
        {/if}
      {/if}
    {:else}
      <!-- Nothing configured yet -->
      <div class="rounded-2xl border border-dashed border-dida-border p-8 text-center">
        <p class="text-dida-text-muted">{t("media.noSources")}</p>
        {#if auth.isAdmin}
          <div class="mt-3"><Button tone="primary" onclick={() => (editing = true)}>{t("media.configureSources")}</Button></div>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Browse modal: the shelf or the stations; a pick plays + closes. -->
  {#if musicBrowseOpen && transportTarget}
    <Dialog title={t("media.library")} size="wide" onclose={() => (musicBrowseOpen = false)}>
      <div>
        {#if curBrowse === "opus"}
          <Library player={transportTarget} onplayed={() => (musicBrowseOpen = false)} />
        {:else if curBrowse === "radio"}
          <RadioBrowse player={transportTarget} onplayed={() => (musicBrowseOpen = false)} />
        {/if}
      </div>
    </Dialog>
  {/if}
{/if}
