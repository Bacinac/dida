<script lang="ts">
  import { t } from "#lib/i18n/index.js";
  import MediaHub from "#lib/MediaHub.svelte";
</script>

<svelte:head><title>{t("nav.media")}</title></svelte:head>

<div class="mx-auto h-[var(--frame-room)] max-w-4xl">
  <MediaHub />
</div>
