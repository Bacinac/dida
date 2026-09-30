<script lang="ts">
  // Audio signal-path — the chain the now-playing audio passes through, source
  // → speaker, with a live measurement (and bit-perfect proof) at every stage
  // DIDA can see. Data is fetched from /media/pipeline; this owns presentation.
  import { api, type Pipeline, type PipeNode } from "$lib/api";
  import { SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { Tag } from "$lib/kit";

  let { entityId, trackKey }: { entityId: string; trackKey: string } = $props();

  let data = $state<Pipeline | null>(null);
  let error = $state<string | null>(null);

  // (Re)load on entity or track change — the chain updates per track (a 44.1/16
  // track and a 96/24 track take the same path but read out differently).
  $effect(() => {
    void entityId;
    void trackKey;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const d = await api.mediaPipeline(entityId);
        if (alive) { data = d; error = null; }
      } catch (e) {
        if (alive) error = errMsg(e);
      } finally {
        if (alive) timer = setTimeout(refresh, 5000);
      }
    }
    refresh();
    return () => { alive = false; clearTimeout(timer); };
  });

  function srcLabel(s: string | undefined): string {
    switch (s) {
      case "library": return t("pipeline.src.library");
      case "radio": return t("pipeline.src.radio");
      case "external": return t("pipeline.src.external");
      default: return t("pipeline.src.unknown");
    }
  }
  const modeLabel = (): string => t("pipeline.mode.direct");

  // Rate colour = the ZEN DAC V2's own LED for what it receives, so the dot and
  // the box never disagree. The exact rate is in the node's text. Full class
  // strings so Tailwind's JIT keeps them. DSD is kept forward-compatible (the
  // library is FLAC-only today, but the DAC takes DSD natively).
  const RATE_COLOR: Record<string, string> = {
    r48: "bg-yellow-400",  // PCM 44.1/48 kHz
    hires: "bg-white",     // PCM 88.2–384 kHz
    dsd: "bg-cyan-400",    // DSD64/128
    dsd256: "bg-red-500",  // DSD256
  };
  const RATE_LABEL: Record<string, string> = {
    r48: "PCM 44.1 / 48 kHz",
    hires: "PCM 88.2–384 kHz",
    dsd: "DSD64 / DSD128",
    dsd256: "DSD256",
  };

  // Heading for each node kind.
  function nodeTitle(n: PipeNode): string {
    switch (n.kind) {
      case "source": return srcLabel(n.source);
      case "transform": return t("pipeline.server");
      case "transport": return t(n.via === "internet" ? "pipeline.transport.internet" : "pipeline.transport.lan");
      case "renderer": return n.name ?? "";
      case "dac": return n.name ?? "DAC";
    }
  }

  // SVG path per node kind (source uses a per-source glyph).
  const ICON: Record<string, string> = {
    cloud: "M6 18a4 4 0 0 1-.6-7.95 5 5 0 0 1 9.7-1.2A4 4 0 0 1 18 18H6z",
    disk: "M4 4h12l4 4v12H4V4zm4 0v5h7V4M8 13h8v5H8z",
    radio: "M4 9h13V6l3-1v14H4V9zm3 3v4h6v-4H7z",
    external: "M14 4h6v6m0-6L10 14M5 7v12h12",
    server: "M5 4h14v6H5V4zm0 10h14v6H5v-6zM8 7h.01M8 17h.01",
    link: "M9 12h6M8 8a4 4 0 0 0 0 8h2m4-8h2a4 4 0 0 1 0 8h-2",
    speaker: "M7 4h10v16H7V4zm5 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    dac: "M7 7h10v10H7V7zM4 9v6m16-6v6M9 4h6m-6 16h6",
  };
  const srcIcon = (s?: string): string =>
    ICON[s === "library" ? "disk" : s === "radio" ? "radio" : s === "external" ? "external" : "cloud"];
  function nodeIcon(n: PipeNode): string {
    switch (n.kind) {
      case "source": return srcIcon(n.source);
      case "transform": return ICON.server;
      case "transport": return ICON.link;
      case "renderer": return ICON.speaker;
      case "dac": return ICON.dac;
    }
  }
</script>

{#if error}
  <p class="px-1 text-s text-dida-danger">{error}</p>
{:else if data}
  <div class="rounded-xl border border-dida-border bg-dida-panel-2/40 p-2.5">
    <!-- Header: just the title; the verdict badge lives in the status row below. -->
    <div class="mb-2">
      <span class="{SUBSECTION_TITLE_CLASS}">{t("pipeline.title")}</span>
    </div>

    <!-- Flow: vertical on mobile, horizontal on sm+. Nodes are equal width
         (flex-1 basis-0 min-w-0) so content length never widens a card. -->
    <div class="flex flex-col items-stretch gap-1 sm:flex-row sm:items-stretch">
      {#each data.nodes as node, i (i)}
        {#if i > 0}
          <!-- connector -->
          <div class="flex shrink-0 items-center justify-center self-center text-dida-text-faint">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-3.5 rotate-90 sm:rotate-0"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
          </div>
        {/if}

        {@const measured = node.kind === "renderer" || node.kind === "dac" ? node.measured : node.measured !== false}
        <div
          class="flex min-w-0 flex-1 basis-0 flex-col gap-0.5 rounded-lg border px-1.5 py-2 text-center
                 {measured
                  ? 'border-dida-border bg-dida-panel'
                  : 'border-dida-border/60 bg-dida-panel/60'}"
        >
          <!-- icon + title (+ iFi-style rate-colour dot on the renderer / DAC) -->
          <div class="flex min-w-0 items-center justify-center gap-1">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
                 class="size-3.5 shrink-0 text-dida-accent"><path d={nodeIcon(node)} /></svg>
            <span class="truncate text-s font-semibold">{nodeTitle(node)}</span>
            {#if node.rate_class && RATE_COLOR[node.rate_class]}
              <span class="size-2 shrink-0 rounded-full ring-1 ring-black/20 dark:ring-white/25 {RATE_COLOR[node.rate_class]}" title={RATE_LABEL[node.rate_class] ?? ""}></span>
            {/if}
          </div>

          <!-- detail line(s) per kind -->
          {#if node.kind === "source"}
            <span class="whitespace-nowrap font-mono text-xs text-dida-text-muted">{node.format ?? "—"}</span>
          {:else if node.kind === "transform"}
            <span class="text-xs text-dida-text-muted">{modeLabel()}</span>
            {#if node.bitperfect}<span class="text-2xs font-medium text-dida-ok">{t("pipeline.bitperfect")}</span>{/if}
          {:else if node.kind === "transport"}
            <span class="font-mono text-2xs uppercase text-dida-text-faint">{node.via}</span>
            {#if node.from || node.to}
              <div class="flex flex-col items-center gap-px font-mono text-2xs leading-tight text-dida-text-faint">
                {#if node.from}<span><span class="mr-0.5 opacity-60">{t("pipeline.from")}</span>{node.from}</span>{/if}
                {#if node.to}<span><span class="mr-0.5 opacity-60">{t("pipeline.to")}</span>{node.to}</span>{/if}
              </div>
            {/if}
          {:else if node.kind === "renderer"}
            <span class="text-2xs uppercase tracking-wide text-dida-text-faint">{node.proto}</span>
            {#if data.health && ['recovering', 'failed', 'unavailable'].includes(data.health.state)}
              <span class="text-2xs text-dida-warn">
                {data.health.state === 'recovering' ? t("pipeline.health.recovering") : t("pipeline.health.failed")}
              </span>
            {/if}
            {#if node.format}
              <!-- Neutral like every other node; only a real MISMATCH (resampling
                   detected) goes loud red. The bit-perfect verdict is the badge below. -->
              <span class="whitespace-nowrap font-mono text-xs {node.match === false ? 'text-dida-danger' : 'text-dida-text-muted'}">{node.format}</span>
              {#if node.bitrate}<span class="font-mono text-2xs text-dida-text-faint">{node.bitrate}</span>{/if}
            {:else if node.bitrate || node.channels}
              <!-- Radio/stream: the renderer reports no samplerate/bitdepth (it
                   decodes a live stream on the fly), but may still measure a stream
                   bitrate and/or channel count — show whatever it actually does. -->
              {#if node.bitrate}<span class="whitespace-nowrap font-mono text-xs text-dida-text-muted">{node.bitrate}</span>{/if}
              {#if node.channels}<span class="font-mono text-2xs text-dida-text-faint">{node.channels}ch</span>{/if}
            {:else}
              <span class="text-2xs text-dida-text-faint">—</span>
            {/if}
          {:else if node.kind === "dac"}
            {#if node.link}<span class="font-mono text-2xs uppercase tracking-wide text-dida-text-faint">{node.link} · {t("pipeline.digital")}</span>{/if}
            {#if data.health?.output}<span class="font-mono text-2xs text-dida-text-faint">{data.health.output.state}</span>{/if}
            {#if node.format}
              <span class="whitespace-nowrap font-mono text-xs text-dida-text-muted">{node.format}</span>
            {:else}
              <span class="text-2xs text-dida-text-faint">—</span>
            {/if}
          {/if}
        </div>
      {/each}
    </div>

    <!-- Verdict + live output-stage status, all in one row. -->
    {#if data.output || data.verified || data.format_match}
      <div class="mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 font-mono text-2xs text-dida-text-faint">
        {#if data.verified}
          <span class="font-sans"><Tag tone="ok">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="size-3"><path d="M5 13l4 4L19 7" /></svg>
            {t("pipeline.verified")}
          </Tag></span>
        {:else if data.format_match}
          <span class="font-sans"><Tag tone="quiet">{t("pipeline.lossless")}</Tag></span>
        {/if}
        {#if data.output}
        <span>
          <span class="opacity-60">{t("pipeline.swvol")}:</span>
          <span class={data.output.volume_control_disabled ? "text-dida-ok" : "text-dida-warn"}>
            {data.output.volume_control_disabled ? t("pipeline.bypassed") : t("pipeline.active")}
          </span>
        </span>
        <span><span class="opacity-60">{t("pipeline.mixer")}:</span> {data.output.mixer ?? t("pipeline.none")}</span>
        {#if data.output.volume != null}<span><span class="opacity-60">{t("pipeline.vol")}:</span> {data.output.volume}%</span>{/if}
        {#if data.output.replay_gain != null}
          <span><span class="opacity-60">ReplayGain:</span>
            <span class={data.output.replay_gain === "off" ? "text-dida-ok" : "text-dida-warn"}>
              {data.output.replay_gain === "off" ? t("pipeline.off") : data.output.replay_gain}
            </span></span>
        {/if}
        {#if data.output.crossfade != null}
          <span><span class="opacity-60">{t("pipeline.crossfade")}:</span>
            <span class={data.output.crossfade === 0 ? "text-dida-ok" : "text-dida-warn"}>
              {data.output.crossfade === 0 ? t("pipeline.off") : `${data.output.crossfade} s`}
            </span></span>
        {/if}
        {#if data.output.mute}<span class="text-dida-warn">{t("media.mute")}</span>{/if}
        {/if}
      </div>
    {/if}
  </div>
{/if}
