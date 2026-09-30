<script lang="ts">
  // Universal-remote hero: an activity/source picker + a spatial D-pad, transport
  // row and volume row. Built for touch first (big, well-spaced targets laid out
  // like a physical remote — the thumb goes where the arrow is) and equally usable
  // with a mouse. Drives a device's `press` button siblings via the Remote lib;
  // only keys the device actually exposes are shown.
  import { api } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { errMsg } from "$lib/errors";
  import { toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { mediaCmd, nextTrack, prevTrack, setSource, sourceOptions, sourceValue } from "$lib/media";
  import { pressKey, remoteButtons, remoteHead, type RemoteKey } from "$lib/remote";
  import type { Device } from "$lib/store.svelte";

  // `showSource` hides the built-in activity picker when a parent (the MediaHub)
  // already owns the source selection. `layout` = "split" puts the D-pad on the
  // left and nav/transport/volume stacked on the right (the MediaHub overlay);
  // "stack" keeps the classic vertical remote (the device card). `player` /
  // `volumeDevice` make this a FACADE: transport/volume keys drive DIDA when the
  // source is DIDA-controlled, otherwise they fall back to Harmony IR.
  let { members, showSource = true, layout = "stack", player = null, volumeDevice = null }: {
    members: Device[];
    showSource?: boolean;
    layout?: "stack" | "split";
    player?: Device | null;       // DIDA transport target (media_transport device)
    volumeDevice?: Device | null; // DIDA volume target (an AVR zone)
  } = $props();

  const head = $derived(remoteHead(members));
  const buttons = $derived(remoteButtons(members));
  const opts = $derived(head ? sourceOptions(head) : []);
  const cur = $derived(head ? sourceValue(head) : null);
  const readOnly = $derived(!auth.canControl);

  // Per-key routing: transport → the DIDA player (radio-aware skip) when present,
  // volume → the AVR zone when present, everything else → Harmony. A key shows if
  // EITHER backend can service it.
  const TRANSPORT = new Set<RemoteKey>(["play", "pause", "stop", "skip_back", "skip_fwd"]);
  const VOLUME = new Set<RemoteKey>(["vol_down", "vol_up", "mute"]);
  const canTransport = $derived(!!player && "media_transport" in player.caps);

  const has = (k: RemoteKey): boolean => {
    if (TRANSPORT.has(k)) return canTransport || k in buttons;
    if (VOLUME.has(k)) return !!volumeDevice || k in buttons;
    return k in buttons;
  };

  function transportCmd(k: RemoteKey, p: Device): Promise<unknown> | null {
    switch (k) {
      case "play": return mediaCmd.play(p.entityId);
      case "pause": return mediaCmd.pause(p.entityId);
      case "stop": return mediaCmd.stop(p.entityId);
      case "skip_fwd": return nextTrack(p);
      case "skip_back": return prevTrack(p);
      default: return null;
    }
  }
  function volumeCmd(k: RemoteKey, d: Device): Promise<unknown> {
    if (k === "vol_up") return api.sendCommand({ entity_id: d.entityId, capability: "volume", command: "volume_up" });
    if (k === "vol_down") return api.sendCommand({ entity_id: d.entityId, capability: "volume", command: "volume_down" });
    return api.sendCommand({ entity_id: d.entityId, capability: "mute", command: "toggle" });
  }

  let busy = $state(false);
  async function press(k: RemoteKey) {
    if (readOnly) return;
    let action: Promise<unknown> | null = null;
    if (TRANSPORT.has(k) && canTransport && player) action = transportCmd(k, player);
    else if (VOLUME.has(k) && volumeDevice) action = volumeCmd(k, volumeDevice);
    else if (buttons[k]) action = pressKey(buttons[k]!);
    if (!action) return;
    busy = true;
    try {
      await action;
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }
  async function pickSource(value: string) {
    if (!head || readOnly) return;
    busy = true;
    try {
      await setSource(head.entityId, value);
    } catch (e) {
      toasts.error(errMsg(e));
    } finally {
      busy = false;
    }
  }

  // 24×24 stroke glyphs (same stroke style as DeviceIcon) per key.
  const G: Record<RemoteKey, string> = {
    up: '<path d="M6 15l6-6 6 6"/>',
    down: '<path d="M6 9l6 6 6-6"/>',
    left: '<path d="M15 6l-6 6 6 6"/>',
    right: '<path d="M9 6l6 6-6 6"/>',
    ok: "",
    back: '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a4 4 0 0 1 0 8h-2"/>',
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 9.5V20h14V9.5"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    play: '<path d="M8 5v14l11-7z"/>',
    pause: '<path d="M9 5v14M15 5v14"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    skip_back: '<path d="M18 6v12l-8-6z"/><path d="M7 6v12"/>',
    skip_fwd: '<path d="M6 6v12l8-6z"/><path d="M17 6v12"/>',
    vol_down: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 12h5"/>',
    vol_up: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M18.5 9.5v5M16 12h5"/>',
    mute: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M22 9.5l-5 5M17 9.5l5 5"/>',
  };
  const ariaKey: Record<RemoteKey, string> = {
    up: "remote.up", down: "remote.down", left: "remote.left", right: "remote.right", ok: "remote.ok",
    back: "remote.back", home: "remote.home", menu: "remote.menu",
    play: "remote.play", pause: "remote.pause", stop: "remote.stop",
    skip_back: "remote.skipBack", skip_fwd: "remote.skipFwd",
    vol_down: "remote.volDown", vol_up: "remote.volUp", mute: "remote.mute",
  } as const;
  const aria = (k: RemoteKey): string => t(ariaKey[k] as Parameters<typeof t>[0]);
</script>

{#snippet keyBtn(k: RemoteKey, cls: string)}
  {#if has(k)}
    <button
      type="button" onclick={() => press(k)} disabled={busy || readOnly}
      aria-label={aria(k)} title={aria(k)}
      class="flex items-center justify-center rounded-lg border border-dida-border bg-dida-panel-2 text-dida-text
             transition-colors hover:border-dida-accent hover:text-dida-accent active:bg-dida-accent/15
             disabled:opacity-40 disabled:hover:border-dida-border disabled:hover:text-dida-text {cls}"
    >
      {#if k === "ok"}
        <span class="text-m font-semibold">OK</span>
      {:else}
        <svg viewBox="0 0 24 24" class="size-6" fill="none" stroke="currentColor"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{@html G[k]}</svg>
      {/if}
    </button>
  {:else}
    <span class={cls}></span>
  {/if}
{/snippet}

{#snippet dpad(cell: string = "aspect-square")}
  <!-- Cell height is parametric: stacked layout keeps square keys; the split layout
       passes a fixed height so the 3 D-pad rows total EXACTLY the right column's
       3 rows (3×h-12 + 2×gap-1.5 = 156px = 3×h-11 + 2×gap-3) — otherwise the
       square keys (sized from the w-48 column) make the D-pad block taller. -->
  {#if has("up") || has("down") || has("left") || has("right") || has("ok")}
    <div class="grid grid-cols-3 gap-1.5">
      <span></span>{@render keyBtn("up", cell)}<span></span>
      {@render keyBtn("left", cell)}{@render keyBtn("ok", cell)}{@render keyBtn("right", cell)}
      <span></span>{@render keyBtn("down", cell)}<span></span>
    </div>
  {/if}
{/snippet}
{#snippet navRow()}
  {#if has("back") || has("home") || has("menu")}
    <div class="grid grid-cols-3 gap-1.5">
      {@render keyBtn("back", "h-11")}{@render keyBtn("home", "h-11")}{@render keyBtn("menu", "h-11")}
    </div>
  {/if}
{/snippet}
{#snippet transportRow()}
  {#if has("skip_back") || has("play") || has("pause") || has("stop") || has("skip_fwd")}
    <div class="flex gap-1.5">
      {@render keyBtn("skip_back", "h-11 flex-1")}
      {@render keyBtn("play", "h-11 flex-1")}
      {@render keyBtn("pause", "h-11 flex-1")}
      {@render keyBtn("stop", "h-11 flex-1")}
      {@render keyBtn("skip_fwd", "h-11 flex-1")}
    </div>
  {/if}
{/snippet}
{#snippet volumeRow()}
  {#if has("vol_down") || has("mute") || has("vol_up")}
    <div class="flex gap-1.5">
      {@render keyBtn("vol_down", "h-11 flex-1")}
      {@render keyBtn("mute", "h-11 flex-1")}
      {@render keyBtn("vol_up", "h-11 flex-1")}
    </div>
  {/if}
{/snippet}

<div class="flex flex-col gap-3">
  <!-- Activity / source picker -->
  {#if head && showSource}
    <div class="flex items-center gap-2">
      <span class="shrink-0 text-s font-medium text-dida-text-muted">{t("media.source")}</span>
      <select
        value={cur ?? ""} onchange={(e) => pickSource(e.currentTarget.value)} disabled={busy || readOnly}
        aria-label={t("media.source")}
        class="min-w-0 flex-1"
      >
        {#if cur && !opts.includes(cur)}<option value={cur}>{cur}</option>{/if}
        {#each opts as o (o)}<option value={o}>{o}</option>{/each}
      </select>
    </div>
  {/if}

  {#if layout === "split"}
    <div class="flex flex-wrap items-start justify-center gap-6">
      <div class="w-48 shrink-0">{@render dpad("h-12")}</div>
      <div class="flex min-w-[11rem] flex-1 flex-col gap-3">
        {@render navRow()}
        {@render transportRow()}
        {@render volumeRow()}
      </div>
    </div>
  {:else}
    <div class="mx-auto w-full max-w-56">{@render dpad()}</div>
    <div class="mx-auto w-full max-w-56">{@render navRow()}</div>
    {@render transportRow()}
    {@render volumeRow()}
  {/if}
</div>
