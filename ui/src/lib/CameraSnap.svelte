<script lang="ts">
  // A polled camera snapshot that never flashes black. Naively swapping <img src>
  // clears the picture while the new frame loads (and go2rtc's frame.jpeg is
  // occasionally slow or drops a frame) → the tile blinks. Here we preload the
  // next frame off-screen and swap in the visible one ONLY once it has decoded,
  // so the last good frame stays put through a slow/failed fetch.
  let { src, alt = "", class: klass = "" }: { src: string; alt?: string; class?: string } = $props();

  let shown = $state("");
  let latest = 0;
  let seq = 0;

  $effect(() => {
    const url = src; // reactive dependency — re-runs each poll tick
    const my = ++seq;
    const img = new Image();
    img.onload = () => { if (my > latest) { latest = my; shown = url; } };
    img.src = url;
    // No cleanup: a superseded onload is ignored via the `my > latest` guard.
  });
</script>

{#if shown}
  <img src={shown} {alt} draggable="false" class={klass} />
{:else}
  <!-- aspect-video keeps an auto-height tile from collapsing to 0 before the first
       frame; a sized tile (h-full) overrides it, so fixed grids are unaffected. -->
  <div class="{klass} aspect-video bg-black"></div>
{/if}
