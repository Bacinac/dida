<script lang="ts">
  // One live marker on the floor plan (HA-floorplan style): a state-aware icon
  // that glows when on, plus a glanceable value badge for covers / climate /
  // sensors. Takes the placement item's member entities + resolved type/name.
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import { glance, hasPresenceCap, presenceOccupied, sceneLit } from "$lib/capabilities";
  import { t } from "$lib/i18n";
  import { clock } from "$lib/dt";
  import type { CapState, Device } from "$lib/store.svelte";

  let { members, icon, name, selected = false, plug = false, tint = null }: {
    members: Device[]; icon: string; name: string; selected?: boolean; plug?: boolean;
    // Permanent pill tint (user style) — an ACTION marker (gate opener) stays
    // recognizable in every state. Live states (running/on/glow) still win.
    tint?: string | null;
  } = $props();

  // Merge the members' caps, but a cap the user HID in the adapter (hidden_caps) is
  // treated as absent — the plan stays consistent with the adapter's expose toggles.
  const caps = $derived.by<Record<string, CapState>>(() => {
    const out: Record<string, CapState> = {};
    for (const m of members) for (const c of Object.keys(m.caps)) if (!m.hiddenCaps.includes(c)) out[c] = m.caps[c];
    return out;
  });
  const num = (c: string): number | null => (typeof caps[c]?.value === "number" ? (caps[c].value as number) : null);
  const bool = (c: string): boolean => caps[c]?.value === true;

  // A device the adapter reports as unreachable: dim and desaturate the marker so
  // the plan stops asserting a stale value as current. This is the Cabin case — a
  // ceiling light glowing "on" for hours after it lost power. The last value is
  // still shown (it is the best guess), but muted, and the title says since when.
  const unreachable = $derived(members.length > 0 && members.some((m) => !m.reachable));
  const downSince = $derived(Math.max(0, ...members.map((m) => (m.reachable ? 0 : m.reachableSince))));
  const heldSince = $derived(Math.max(0, ...members.map((m) => m.manualSince)));

  const on = $derived(bool("on_off"));
  const open = $derived(num("open_close"));
  // An appliance lights up while it RUNS — it's drawing real power (its on/off relay
  // is NOT the signal: a plug can be on but idle), or its run-state enum says so (a
  // washer between fills draws ~0 W yet is still running).
  const running = $derived.by(() => {
    const s = caps["enum"]?.value;
    return typeof s === "string" && /^(run|running|active|washing|drying|cooking|on)$/i.test(s);
  });
  const drawing = $derived.by(() => {
    const pw = num("power"), cur = num("current");
    return running || (pw != null && pw > 1) || (cur != null && cur > 0.02);
  });
  // An appliance reporting its time-left (a washer/dryer) shifts RED → amber → GREEN as
  // it finishes: hue 0 while there's ≥90 min to go, 120 (green) at 0. So a glance at the
  // marker says how close the wash is to done. A live countdown (remaining > 0) is ITSELF
  // proof it's running — the adapter zeroes `remaining` the instant the cycle ends, so a
  // finished unit reads idle (gray), never an eternal green. Deliberately INDEPENDENT of
  // `drawing`: mid-rinse a washer draws ~0 W (and its machine-state enum may be hidden or
  // report a phase name), yet the countdown must still color the marker.
  const runColor = $derived.by<string | null>(() => {
    const left = num("remaining");
    if (left == null || left <= 0) return null;
    const hue = Math.round(120 * (1 - Math.min(1, left / 5400)));
    return `hsl(${hue} 70% 55%)`; // 55% L reads on the chip in both light/dark plans (ring + glyph, not a fill)
  });
  // A climate unit (AC / heat pump) is "running" when its hvac_mode isn't off.
  const hvacOn = $derived.by(() => {
    const v = caps["hvac_mode"]?.value;
    return typeof v === "string" && v !== "" && v !== "off";
  });
  // A heater with stepped levels (enum: Off / Level 1 / Level 2 / …) runs WARMER as
  // it climbs: amber at the lowest step → red at max, nothing when Off. Gated to the
  // heater glyphs so a generic enum (fan speed, a mode picker) doesn't run warm.
  const heatColor = $derived.by<string | null>(() => {
    if (icon !== "heater" && icon !== "patio_heater") return null;
    const v = caps["enum"]?.value;
    let opts = caps["enum_options"]?.value as unknown;
    if (typeof opts === "string") { try { opts = JSON.parse(opts); } catch { opts = null; } }
    if (typeof v !== "string" || !Array.isArray(opts) || opts.length < 2) return null;
    const idx = opts.indexOf(v);
    if (idx <= 0) return null;                    // Off / unknown → idle, no glow
    const hue = Math.round(40 - 40 * (idx / (opts.length - 1))); // 40° amber → 0° red
    return `hsl(${hue} 85% 55%)`;
  });
  // A robot vacuum colours by WORK: while cleaning the tint follows the selected
  // suction mode (cool blue at silent → red at max, the heater-ladder idea), the
  // trip back to the dock keeps the plain accent glow, and an error burns red.
  // Docked/charging is not work — the chip stays idle.
  const vacState = $derived.by<string | null>(() => {
    const s = caps["vacuum"]?.value;
    return typeof s === "string" ? s : null;
  });
  const vacActive = $derived(vacState === "cleaning" || vacState === "returning" || vacState === "starting" || vacState === "error");
  const vacColor = $derived.by<string | null>(() => {
    if (vacState === "error") return "hsl(0 75% 55%)";
    if (vacState !== "cleaning") return null;
    const v = caps["enum"]?.value;
    let opts = caps["enum_options"]?.value as unknown;
    if (typeof opts === "string") { try { opts = JSON.parse(opts); } catch { opts = null; } }
    const idx = typeof v === "string" && Array.isArray(opts) ? opts.indexOf(v) : -1;
    if (idx < 0 || !Array.isArray(opts) || opts.length < 2) return "hsl(150 70% 45%)"; // cleaning, mode unknown → green
    const hue = Math.round(200 - 200 * (idx / (opts.length - 1)));
    return `hsl(${hue} 75% 50%)`;
  });
  // A scene region (a parking place, a gate) lights AMBER while it reads live — the
  // same colour the camera wall gives BABA's occupancy fact, so the plan and the wall
  // say the same thing in the same tone.
  const sceneOn = $derived.by(() => {
    const v = caps["scene_state"]?.value;
    return typeof v === "string" && sceneLit(v);
  });
  // A place (P1, P2) is drawn as its NAME — whether it's taken is the colour, and the
  // car standing in it is a detail behind a tap. Its name is the marker's only
  // content, so unlike a value badge it survives on a narrow plan.
  const placeName = $derived("scene_state" in caps);
  // Others glow when their actuator is on / open / running. Presence has its own dot so
  // an empty room still reads clearly (not just "no glow").
  const activeGlow = $derived(!plug && (on || hvacOn || heatColor != null || sceneOn || vacActive || (open != null && open > 0)));
  // The pip reads the CONSIDERED presence verdict (person_count ▸ occupancy ▸ motion)
  // across each member, ignoring raw latches — a camera's `motion` fires on any object,
  // a mmWave's raw binary can stick "true". hiddenCaps stay excluded (per member).
  const presenceCaps = $derived(members.map((m) =>
    Object.fromEntries(Object.entries(m.caps).filter(([c]) => !m.hiddenCaps.includes(c)))));
  const hasPresence = $derived(hasPresenceCap(presenceCaps));
  const occupied = $derived(presenceOccupied(presenceCaps));
  // An RGB light glows in its actual colour, not the generic accent.
  const color = $derived.by<string | null>(() => {
    const v = caps["color_rgb"]?.value;
    return on && typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v) ? v : null;
  });
  // The glow colour when active: an RGB light's own colour, else a heater's warm
  // level tint — both drive the same coloured-marker path below.
  const glowColor = $derived(color ?? heatColor ?? vacColor ?? (sceneOn ? "#f59e0b" : null));

  const badge = $derived.by<string | null>(() => {
    if (plug) return null;                 // an appliance is JUST an icon that lights when
                                           // running — power / energy / state are on tap
    if ("hvac_mode" in caps) return null; // a climate unit is just an icon + glow, no value
    if (placeName) return null;   // a place carries its own name instead (below)
    return glance(caps);
  });
</script>

<div
  class="relative flex items-center gap-1 rounded-full px-1.5 py-1 shadow-md backdrop-blur transition-colors
    {unreachable ? 'opacity-40 grayscale' : ''}
    {!(plug && drawing) && !activeGlow && tint ? 'border-2' : 'border'}
    {selected ? 'ring-2 ring-dida-accent ring-offset-1 ring-offset-dida-panel' : ''}
    {plug && runColor
      ? 'bg-black/30'
      : plug && drawing
        ? 'border-dida-danger bg-dida-danger text-white shadow-dida-danger/40'
        : activeGlow
          ? (glowColor ? 'text-white shadow-black/30' : 'border-dida-accent bg-dida-accent-strong text-dida-on-accent shadow-dida-accent/40')
          : tint
            ? 'bg-black/30 shadow-black/30'
            : 'border-white/15 bg-black/30 text-white/90'}"
  style={plug && runColor ? `border-color:${runColor}; color:${runColor}`
       : activeGlow && glowColor ? `background:${glowColor}; border-color:${glowColor}`
       : !(plug && drawing) && !activeGlow && tint ? `border-color:${tint}; color:${tint}` : ''}
  title={unreachable
    ? `${name} — ${downSince ? t("card.unreachableSince", { t: clock(downSince) }) : t("card.unreachable")}`
    : heldSince ? `${name} — ${t("card.manualSince", { t: clock(heldSince) })}` : name}
>
  <!-- idle frost matches the sensor labels (bg-black/30 + blur): the pill takes its
       tone from the PLAN under it, not from the theme — a theme-locked panel colour
       reads near-black on dark and near-white on light over the mid-tone plan -->
  {#if icon}<DeviceIcon type={icon} class="size-4 shrink-0" />{/if}
  {#if placeName}<span class="px-0.5 text-s font-semibold">{name}</span>{/if}
  <!-- the badge is the marker's widest part — on a narrow plan (@container = the
       plan) it drives marker collisions, so drop it; a tap still shows the value -->
  {#if badge}<span class="@max-md:hidden pr-0.5 text-s font-semibold tabular-nums">{badge}</span>{/if}
  {#if hasPresence}
    <span
      title={occupied ? t("fp.occupied") : t("fp.empty")}
      class="absolute -right-0.5 -top-0.5 size-2.5 rounded-full border-2 border-black/40 {occupied ? 'bg-dida-ok' : 'bg-dida-text-faint'}"
    ></span>
  {/if}
</div>
