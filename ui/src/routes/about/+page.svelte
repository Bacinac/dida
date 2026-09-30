<script lang="ts">
  import { About } from "$lib/kit";
  import Brand from "$lib/Brand.svelte";
  import { t } from "$lib/i18n";
  import { version } from "$lib/version.svelte";
</script>

<svelte:head><title>DIDA</title></svelte:head>

<About
  tagline={t("about.tagline")}
  intro={t("about.intro")}
  motto={t("about.acronymHtml")}
  what={t("about.what")}
  why={t("about.why")}
  stack={t("about.stack")}
  aside={t("about.disclaimer")}
  build={version.booted}
>
  {#snippet brand()}<Brand />{/snippet}
</About>
