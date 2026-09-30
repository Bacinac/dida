<script lang="ts">
  // One-tap loudness presets (mute → Quiet/Normal/Loud …) for a single AVR/volume
  // device, tinted green→red by level. Pinned at the TOP of the media view (above the
  // source selection) and shown only while a zone is actually playing. Extracted from
  // the now-playing card so the same row can live above the source picker.
  //
  // Labels render through tr(): the stored label is the ENGLISH source; a Croatian
  // session localises via the translations dictionary (Settings → Prijevodi).
  import { errMsg } from "$lib/errors";
  import { toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { mediaCmd, mediaMuted, mediaVolume } from "$lib/media";
  import type { Device } from "$lib/store.svelte";
  import { tr } from "$lib/translations.svelte";
  import { volume, volumeColor } from "$lib/volume.svelte";

  let { device, readOnly = false }: { device: Device; readOnly?: boolean } = $props();

  const vol = $derived(mediaVolume(device) ?? 0);
  const muted = $derived(mediaMuted(device));
  // The current level "sits on" a preset (±2%) → that preset shows the readout, so
  // the mute card doesn't also print it.
  const hasActive = $derived(volume.presets.some((p) => Math.abs(vol - p.value) <= 2));

  async function run(p: Promise<unknown>): Promise<void> {
    try {
      await p;
    } catch (e) {
      toasts.error(errMsg(e));
    }
  }

  const SPK_ON = "M3 9v6h4l5 5V4L7 9H3zm11 .8a3.5 3.5 0 0 1 0 4.4M16.5 7a7 7 0 0 1 0 10";
  const SPK_MUTE = "M3 9v6h4l5 5V4L7 9H3zm13.5 3l2.7-2.7-1.4-1.4L15 10.6V12zm0 0l2.7 2.7-1.4 1.4L15 13.4z";
</script>

{#if "volume" in device.caps && volume.presets.length}
  <div class="flex items-center gap-1.5">
    <button
      type="button" onclick={() => run(mediaCmd.toggleMute(device.entityId))} disabled={readOnly}
      aria-label={muted ? t("media.unmute") : t("media.mute")} title={muted ? t("media.unmute") : t("media.mute")}
      class="flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md border px-2 py-1.5 text-s font-semibold transition disabled:opacity-50
             {muted ? 'border-dida-accent bg-dida-accent/10 text-dida-accent' : 'border-dida-border text-dida-text-muted hover:border-dida-accent hover:text-dida-text'}"
    >
      <svg viewBox="0 0 24 24" fill={muted ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-4"><path d={muted ? SPK_MUTE : SPK_ON} /></svg>
      {#if !muted && !hasActive}<span class="tabular-nums" style="color:{volumeColor(vol, { light: 62 })}">{Math.round(vol)}%</span>{/if}
    </button>
    {#each volume.presets as p (p.label)}
      <!-- Muted → no preset reads as "active": the sound is off, so highlighting a
           level alongside the lit mute button is a contradiction. -->
      {@const on = !muted && Math.abs(vol - p.value) <= 2}
      <button
        type="button" onclick={() => run(mediaCmd.setVolume(device.entityId, p.value))} disabled={readOnly}
        title="{tr(p.label)} · {p.value}%"
        style={on
          ? `background-color:${volumeColor(p.value)};border-color:${volumeColor(p.value)}`
          : `border-color:${volumeColor(p.value, { alpha: 0.45 })};color:${volumeColor(p.value, { light: 62 })}`}
        class="min-w-0 flex-1 truncate rounded-md border px-2 py-1.5 text-s font-semibold transition disabled:opacity-50 {on ? 'text-white' : 'hover:brightness-125'}"
      >{tr(p.label)}{#if on} · {Math.round(vol)}%{/if}</button>
    {/each}
  </div>
{/if}
