<script lang="ts">
  // Soft light glow overlay for the floor plan. Each lit light contributes a
  // shape — a radial gradient (radius override) or a filled room polygon — tinted
  // by the light's colour, opacity by its brightness. `mix-blend-mode: screen`
  // makes the shapes glow over the plan and add up where they overlap.
  import type { GlowSpec } from "$lib/floorGeometry";
  let { glows }: { glows: GlowSpec[] } = $props();
  const gid = (id: string): string => "fpg-" + id.replace(/[^a-zA-Z0-9_-]/g, "-");
</script>

<svg
  viewBox="0 0 100 100"
  preserveAspectRatio="none"
  class="pointer-events-none absolute inset-0 z-[3] h-full w-full"
  style="mix-blend-mode: screen"
  aria-hidden="true"
>
  <defs>
    <filter id="fp-soft" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="1.6" />
    </filter>
    {#each glows as g (g.id)}
      {#if g.kind === "radius"}
        <radialGradient id={gid(g.id)}>
          <stop offset="0%" stop-color={g.color} stop-opacity={g.op} />
          <stop offset="60%" stop-color={g.color} stop-opacity={g.op * 0.5} />
          <stop offset="100%" stop-color={g.color} stop-opacity="0" />
        </radialGradient>
      {/if}
    {/each}
  </defs>
  {#each glows as g (g.id)}
    {#if g.kind === "radius"}
      <circle cx={g.x} cy={g.y} r={g.r} fill="url(#{gid(g.id)})" />
    {:else if g.kind === "beams" && g.rays}
      {#each g.rays as ray, i (i)}
        <line x1={g.x} y1={g.y} x2={ray.x2} y2={ray.y2}
          stroke={ray.color} stroke-width="1.1" stroke-opacity={g.op * 0.3} stroke-linecap="round" filter="url(#fp-soft)" />
        <line x1={g.x} y1={g.y} x2={ray.x2} y2={ray.y2}
          stroke={ray.color} stroke-width="0.3" stroke-opacity={g.op * 0.8} stroke-linecap="round" />
      {/each}
      <circle cx={g.x} cy={g.y} r="1.2" fill={g.color} fill-opacity={g.op} filter="url(#fp-soft)" />
    {:else if g.points}
      <polygon points={g.points} fill={g.color} fill-opacity={g.op} filter="url(#fp-soft)" />
    {/if}
  {/each}
</svg>
