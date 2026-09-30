<script lang="ts">
  // Renders one catalog icon by name (a DeviceType, "strip", or any pickable
  // name from deviceIcons). Stroke uses currentColor so it inherits colour.
  import { ICONS } from "$lib/deviceIcons";

  let { type, class: klass = "size-4" }: { type: string; class?: string } = $props();
  const inner = $derived(ICONS[type] ?? ICONS.other);
</script>

<svg
  class={klass}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.7"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>{@html inner}</svg>
