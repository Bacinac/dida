<script lang="ts">
  // /media — the unified MediaHub: one source picker (internal AV via Harmony,
  // external streaming via an AVR zone) with a body that morphs to what's playing,
  // browse (local / Tidal / radio) folded in. No areaId → the whole home.
  import { t } from "$lib/i18n";
  import MediaHub from "$lib/MediaHub.svelte";
</script>

<svelte:head><title>{t("nav.media")}</title></svelte:head>

<div class="mx-auto h-[var(--frame-room)] max-w-4xl">
  <MediaHub />
</div>
