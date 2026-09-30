<script lang="ts">
  // A 40×14 shape-of-the-day, drawn inline on a device card.
  //
  // Deliberately NOT a chart: no axes, no grid, no tooltip, no library. At this
  // size a chart's furniture is bigger than its data, and the question a card
  // answers is "has this been steady, climbing or falling", not "what was it at
  // 14:20" — that's what the detail panel and the history card are for.
  let { values, width = 40, height = 14 }: {
    values: number[]; width?: number; height?: number;
  } = $props();

  // Each line is scaled to its OWN range, so a battery at 64 % and a power reading
  // at 300 W both fill their box. A flat series draws down the middle rather than
  // dividing by a zero range.
  const path = $derived.by(() => {
    if (values.length < 2) return "";
    const lo = Math.min(...values), hi = Math.max(...values);
    const span = hi - lo;
    const stepX = width / (values.length - 1);
    return values
      .map((v, i) => {
        const x = i * stepX;
        const y = span === 0 ? height / 2 : height - ((v - lo) / span) * height;
        return `${i === 0 ? "M" : "L"}${Math.round(x * 10) / 10},${Math.round(y * 10) / 10}`;
      })
      .join(" ");
  });
</script>

{#if path}
  <svg {width} {height} viewBox="0 0 {width} {height}" class="shrink-0 overflow-visible" aria-hidden="true">
    <path d={path} fill="none" stroke="currentColor" stroke-width="1.25"
      stroke-linecap="round" stroke-linejoin="round" opacity="0.7" />
  </svg>
{/if}
