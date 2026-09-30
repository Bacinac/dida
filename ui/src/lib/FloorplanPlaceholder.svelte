<script lang="ts">
  // Stand-in plan for an installation that hasn't uploaded a scaffold yet. Drawn
  // inline rather than shipped as an image so it follows the theme's colours (the
  // tokens flip with light/dark) and stays a generic apartment — no room labels,
  // nothing anyone could mistake for their own home.
  let { class: klass = "" }: { class?: string } = $props();
</script>

<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" aria-hidden="true" class={klass}
  fill="none" stroke="var(--color-dida-text-faint)" stroke-width="1.1" stroke-linecap="round">
  <rect x="8" y="8" width="84" height="84" rx="1.5"
    fill="var(--color-dida-panel-2)" fill-opacity="0.45" stroke="none" />

  <!-- outer walls, broken at the entrance -->
  <path d="M8 8 H92 M92 8 V92 M92 92 H52 M40 92 H8 M8 92 V8" />

  <!-- partitions; every gap is a doorway -->
  <g stroke-width="0.8">
    <path d="M50 8 V28 M50 36 V54" />
    <path d="M8 54 H24 M32 54 H66 M74 54 H92" />
    <path d="M34 54 V70 M34 78 V92" />
    <path d="M68 54 V70 M68 78 V92" />
    <path d="M50 30 H64 M72 30 H92" />
  </g>
</svg>
