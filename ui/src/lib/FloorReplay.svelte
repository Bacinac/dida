<script lang="ts">
  // The replay bar under the plan: a window, a cursor, and playback. The plan
  // itself needs no replay code — it renders whatever the device store holds,
  // and while this is open the store holds a past instant.

  import { replay, SPEEDS } from "$lib/replay.svelte";
  import { Button } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dayTime } from "$lib/dt";

  let { onclose }: { onclose: () => void } = $props();

  const RANGES: { hours: number; key: MessageKey }[] = [
    { hours: 6, key: "replay.range6h" },
    { hours: 24, key: "replay.range24h" },
    { hours: 72, key: "replay.range3d" },
    { hours: 168, key: "replay.range7d" },
  ];

  const span = $derived(Math.max(1, replay.to - replay.frm));
  const frac = $derived((replay.cursor - replay.frm) / span);

  const stamp = (ms: number): string => dayTime(ms);

  function onslide(e: Event): void {
    replay.pause();
    replay.seek(replay.frm + (Number((e.currentTarget as HTMLInputElement).value) / 1000) * span);
  }

  function onkey(e: KeyboardEvent): void {
    if (e.key === " ") { e.preventDefault(); replay.playing ? replay.pause() : replay.play(); }
    else if (e.key === "ArrowLeft") replay.seek(replay.cursor - replay.hold);
    else if (e.key === "ArrowRight") replay.seek(replay.cursor + replay.hold);
    else if (e.key === "Escape") onclose();
  }
</script>

<svelte:window onkeydown={onkey} />

<div class="rounded-lg border border-dida-warn/40 bg-dida-warn/5 p-3 space-y-2">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-s font-semibold uppercase tracking-wide text-dida-warn">
      {t("replay.badge")}
    </span>
    {#if replay.active}
      <span class="font-mono text-m tabular-nums">{stamp(replay.cursor)}</span>
    {/if}
    {#if replay.loading}
      <span class="text-s opacity-60">{t("common.loading")}</span>
    {/if}
    <div class="ml-auto flex items-center gap-1">
      {#if replay.active}
        {#each RANGES as r (r.hours)}
          <Button
            size="small"
            selected={replay.hours === r.hours}
            disabled={replay.loading}
            onclick={() => replay.reopen(r.hours)}
          >{t(r.key)}</Button>
        {/each}
      {/if}
      <Button size="small" onclick={onclose}>{t("replay.exit")}</Button>
    </div>
  </div>

  {#if replay.active}
  <div class="flex items-center gap-2">
    <Button tone="primary"
      size="small" disabled={replay.loading}
      onclick={() => (replay.playing ? replay.pause() : replay.play())}
      label={replay.playing ? t("replay.pause") : t("replay.play")}
    >{replay.playing ? "⏸" : "▶"}</Button>
    <input
      class="h-2 flex-1 cursor-pointer accent-dida-warn"
      type="range"
      min="0"
      max="1000"
      value={Math.round(frac * 1000)}
      aria-label={t("replay.cursor")}
      oninput={onslide}
    />
    <select
     
      bind:value={replay.speed}
      aria-label={t("replay.speed")}
    >
      {#each SPEEDS as s (s)}
        <option value={s}>{s}×</option>
      {/each}
    </select>
  </div>

  <div class="flex justify-between font-mono text-xs opacity-50 tabular-nums">
    <span>{stamp(replay.frm)}</span>
    <span>{stamp(replay.to)}</span>
  </div>

  <p class="text-xs leading-snug opacity-60">
    {t("replay.help")}
    {#if replay.truncated}
      <span class="text-dida-warn">{t("replay.truncated")}</span>
    {/if}
  </p>
  {/if}

  {#if replay.error}
    <p class="text-s text-dida-danger">{replay.error}</p>
  {/if}
</div>
