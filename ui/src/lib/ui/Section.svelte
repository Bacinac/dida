<script lang="ts">
  // A titled section of a page — uppercase muted title, content below.
  import { SECTION_TITLE_CLASS } from "$lib/ui/tokens";
  type Props = { title?: string; children: import("svelte").Snippet };
  let { title, children }: Props = $props();
</script>

<section class="mt-8 space-y-3 first:mt-0">
  {#if title}<h3 class={SECTION_TITLE_CLASS}>{title}</h3>{/if}
  {@render children()}
</section>
