<script lang="ts">
  // Page-level wrapper — constrains width, consistent rhythm. `wide` opts into max-w-7xl.
  type Props = { wide?: boolean; children: import("svelte").Snippet };
  let { wide = false, children }: Props = $props();
</script>

<div class={wide ? "w-full max-w-7xl" : "w-full max-w-4xl"}>{@render children()}</div>
