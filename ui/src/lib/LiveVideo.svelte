<script lang="ts">
  // A camera, live: its own encoded stream as fragmented MP4 through DIDA's proxy,
  // fed to MediaSource. Full resolution, no transcode, one plain HTTP GET that rides
  // any tunnel. go2rtc has no encoder behind its MJPEG route, so that one answers an
  // empty body — which is what the doorbell overlay used to show. A source with no
  // stream at all (a Frigate without go2rtc), or one in a codec this browser cannot
  // decode (HEVC outside Safari and hardware Chrome), gets its frame once a second —
  // the latter says so, since a picture that only looks live is the worse failure.
  import { t } from "$lib/i18n";
  import CameraSnap from "$lib/CameraSnap.svelte";

  let { entityId, alt = "", class: klass = "" }: { entityId: string; alt?: string; class?: string } = $props();

  let video = $state<HTMLVideoElement>();
  // Per camera, so pointing the component at another one tries that one afresh.
  let stills = $state<{ id: string; note: string } | null>(null);
  let tick = $state(0);

  // Safari only ships the managed variant; everything else has the plain one.
  const Source: typeof MediaSource | undefined =
    (globalThis as { ManagedMediaSource?: typeof MediaSource }).ManagedMediaSource ?? globalThis.MediaSource;

  // Latency cap: a decoder that fell behind (a tab in the background, a slow frame)
  // jumps back to the edge rather than showing the gate as it was a minute ago.
  const MAX_LAG_S = 1.5;
  const KEEP_S = 10;
  // A camera sends a keyframe every few seconds; a stream silent this long has
  // stalled without closing, and only a reconnect brings the picture back.
  const STALL_MS = 10_000;

  class NoStream extends Error {}

  const settled = (sb: SourceBuffer) =>
    sb.updating ? new Promise((r) => sb.addEventListener("updateend", r, { once: true })) : Promise.resolve();

  async function stream(el: HTMLVideoElement, id: string, signal: AbortSignal) {
    const res = await fetch(`/api/camera/${encodeURIComponent(id)}/mp4`, { signal });
    if (res.status === 404) throw new NoStream();
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const type = res.headers.get("content-type") ?? "";
    if (!Source || !Source.isTypeSupported(type)) throw new NoStream(t("cameras.liveUnsupported"));
    const ms = new Source();
    el.disableRemotePlayback = true;
    el.src = URL.createObjectURL(ms);
    await new Promise((r) => ms.addEventListener("sourceopen", r, { once: true }));
    URL.revokeObjectURL(el.src);
    const sb = ms.addSourceBuffer(type);
    const reader = res.body.getReader();
    signal.addEventListener("abort", () => void reader.cancel().catch(() => {}), { once: true });
    for (;;) {
      const stall = setTimeout(() => void reader.cancel().catch(() => {}), STALL_MS);
      const { done, value } = await reader.read().finally(() => clearTimeout(stall));
      if (done) throw new Error("stream ended");
      await settled(sb);
      sb.appendBuffer(value);
      await settled(sb);
      if (!sb.buffered.length) continue;
      const start = sb.buffered.start(0);
      const end = sb.buffered.end(sb.buffered.length - 1);
      if (end - el.currentTime > MAX_LAG_S) el.currentTime = end - 0.2;
      if (el.paused) void el.play().catch(() => {});
      if (el.currentTime - start > 2 * KEEP_S) {
        sb.remove(start, el.currentTime - KEEP_S);
        await settled(sb);
      }
    }
  }

  $effect(() => {
    const el = video;
    const id = entityId;
    if (!el) return;
    const ctl = new AbortController();
    (async () => {
      while (!ctl.signal.aborted) {
        try {
          await stream(el, id, ctl.signal);
        } catch (e) {
          if (ctl.signal.aborted) return;
          if (e instanceof NoStream) {
            stills = { id, note: e.message };
            return;
          }
          await new Promise((r) => setTimeout(r, 2000));
        }
      }
    })();
    return () => {
      ctl.abort();
      el.removeAttribute("src");
      el.load();
    };
  });

  $effect(() => {
    if (stills?.id !== entityId) return;
    const timer = setInterval(() => { if (!document.hidden) tick += 1; }, 1000);
    return () => clearInterval(timer);
  });
</script>

{#if stills?.id === entityId}
  <CameraSnap src={`/api/camera/${encodeURIComponent(entityId)}/snapshot?t=live${tick}`} {alt} class={klass} />
  {#if stills.note}
    <span class="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 rounded bg-black/70 px-2 py-1
                 text-center text-s text-white">{stills.note}</span>
  {/if}
{:else}
  <!-- svelte-ignore a11y_media_has_caption -- a live camera has no caption track to offer -->
  <video bind:this={video} class="{klass} bg-black" autoplay muted playsinline aria-label={alt}></video>
{/if}
