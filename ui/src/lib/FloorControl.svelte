<script lang="ts">
  // Slim control panel for the floor-plan long-press popover: ONLY the relevant
  // adjustments (an LED strip → brightness + colour), no Power toggle (the tap
  // handles on/off and long-press already turned it on), no area/seen/badge.
  import { onDestroy } from "svelte";
  import { Button } from "$lib/kit";
  import CapabilityControl from "$lib/CapabilityControl.svelte";
  import ClimateCard from "$lib/ClimateCard.svelte";
  import MediaPlayer from "$lib/MediaPlayer.svelte";
  import Remote from "$lib/Remote.svelte";
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import { capMeta, capRank } from "$lib/capabilities";
  import { t } from "$lib/i18n";
  import { isMediaPlayer, MEDIA_CAPS } from "$lib/media";
  import { isRemote, remoteButtonIds } from "$lib/remote";
  import { isClimate, CLIMATE_CAPS } from "$lib/climate";
  import type { CapState, Device } from "$lib/store.svelte";

  let { members, name, icon, showToggle = false, onHistory, ondetail }: {
    members: Device[]; name: string; icon: string; showToggle?: boolean;
    onHistory?: (entityId: string, cap: string) => void;
    ondetail?: (entityId: string) => void;   // open the device-detail panel
  } = $props();

  // Hold on the card HEADER → the device's on/off timeline. The power toggle
  // itself lives on the marker (tap), not in the card — the title stands in for
  // it as the hold target on multi-field devices.
  const powerMember = $derived(members.find((m) => "on_off" in m.caps) ?? null);

  // Hold on ONE value row → its history (the popover never stacks charts itself).
  // Only chartable controls get the hold; a drag (slider) or an early release
  // cancels it, and the click that follows a fired hold is swallowed so the
  // underlying control doesn't also act.
  const HOLDABLE = new Set(["sensor", "number", "slider", "toggle", "binary", "mower"]);
  // Read-only rows: nothing on them to operate, so a plain click is the inspection.
  const CLICK_HISTORY = new Set(["sensor", "binary", "event", "mower"]);
  const HOLD_MS = 450, HOLD_CANCEL = 8;
  let holdTimer: ReturnType<typeof setTimeout> | null = null;
  let holdX = 0, holdY = 0, holdFired = false;
  const clearHold = () => { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } };
  // A pending hold must die with the popover: if it closes mid-press (the selection
  // cleared elsewhere), the timer would still fire and pop a history card over a
  // control that is no longer there.
  onDestroy(clearHold);
  function holdDownFor(e: PointerEvent, entityId: string, cap: string) {
    if (!onHistory) return;
    holdX = e.clientX; holdY = e.clientY; holdFired = false;
    clearHold();
    holdTimer = setTimeout(() => { holdFired = true; onHistory(entityId, cap); }, HOLD_MS);
  }
  function holdDown(e: PointerEvent, f: Field) {
    if (!HOLDABLE.has(capMeta(f.cap).control)) return;
    holdDownFor(e, f.entity.entityId, f.cap);
  }
  function holdMove(e: PointerEvent) {
    if (holdTimer && Math.hypot(e.clientX - holdX, e.clientY - holdY) > HOLD_CANCEL) clearHold();
  }
  function holdClick(e: MouseEvent) {
    if (holdFired) { holdFired = false; e.preventDefault(); e.stopPropagation(); }
  }

  // on_off is normally driven by the marker tap, so it's hidden here — except for
  // a plug opened via tap (showToggle), where this popover IS the only place to
  // switch it. *_options are metadata for their control.
  // scene_state: the marker's colour already says whether the place is taken, and the
  // vehicle row below says it in words — spelling out "present" made a third claim.
  const HIDDEN = new Set([
    "on_off", "enum_options", "effect_options", "number_options", "hvac_mode_options", "fan_mode_options",
    "vacuum_mode_options",
    "source", "source_options", "scene_state",
    // footnote of identity_presence, never its own row
    "identity_since",
  ]);
  const climateMember = $derived(members.find(isClimate) ?? null);
  const mediaMember = $derived(members.find(isMediaPlayer) ?? null);
  const remoteMode = $derived(mediaMember ? false : isRemote(members));
  const remoteBtnIds = $derived(remoteMode ? remoteButtonIds(members) : new Set<string>());

  // Generic caps whose capability name ("Number", "Flag", …) says nothing about what the
  // control IS — for these the entity's own name is the meaningful label (e.g. a grouped
  // "Mower intensity" number helper reads "Mower intensity", not "Number").
  const GENERIC = new Set(["number", "enum", "boolean", "text", "time"]);

  interface Field { entity: Device; cap: string; cs: CapState; }
  const fields = $derived.by<Field[]>(() => {
    const out: Field[] = [];
    // Same name + same capability = the same reading told twice. A parking place
    // watched by two cameras is exactly that: two entities named "P1", each with
    // its own scene_state — one row, not two.
    const seen = new Set<string>();
    for (const m of members) {
      if (remoteBtnIds.has(m.entityId)) continue; // a remote key → shown in the Remote hero
      // Write-only caps (`press`) carry no live state, so they're absent from `caps` —
      // pull them from the declared `capabilities` too, or a standalone press device
      // (a laser, a projector button) renders an empty card. Harmony keys are already
      // filtered out above (remoteBtnIds), so this only reaches lone press buttons.
      const capKeys = new Set<string>(Object.keys(m.caps));
      for (const c of m.capabilities) if (c === "press") capKeys.add(c);
      for (const cap of capKeys) {
        if (m.hiddenCaps.includes(cap)) continue; // user hid it in the adapter → not here either
        if (HIDDEN.has(cap) && !(showToggle && cap === "on_off")) continue;
        if (m === climateMember && CLIMATE_CAPS.has(cap)) continue;
        if (m === mediaMember && MEDIA_CAPS.has(cap)) continue;
        const key = `${m.name}|${cap}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ entity: m, cap, cs: m.caps[cap] ?? { value: null, unit: null, updatedAt: 0 } });
      }
    }
    return out.sort((a, b) => capRank(a.cap) - capRank(b.cap));
  });

  function jsonList(entity: Device, cap: string): string[] {
    const v = entity.caps[cap]?.value;
    if (typeof v !== "string") return [];
    try { const a = JSON.parse(v); return Array.isArray(a) ? a.map(String) : []; } catch { return []; }
  }
  const optionsFor = (f: Field): string[] =>
    f.cap === "enum" ? jsonList(f.entity, "enum_options")
      : f.cap === "vacuum_mode" ? jsonList(f.entity, "vacuum_mode_options")
      : f.cap === "effect" ? jsonList(f.entity, "effect_options") : [];
  function rangeFor(f: Field): { min?: number; max?: number; step?: number } | undefined {
    if (f.cap !== "number") return undefined;
    const v = f.entity.caps["number_options"]?.value;
    if (typeof v !== "string") return undefined;
    try { const o = JSON.parse(v); return { min: o.min, max: o.max, step: o.step }; } catch { return undefined; }
  }
</script>

<div class="rounded-lg border border-dida-border bg-dida-panel p-3 shadow-xl">
  <!-- svelte-ignore a11y_no_static_element_interactions -- hold-to-inspect over the row header; the power toggle beside it is a real control and keeps its own keyboard path -->
  <div class="mb-1 flex items-center gap-2 pr-7"
    onpointerdown={(e) => powerMember && !(e.target as Element).closest("button") && holdDownFor(e, powerMember.entityId, "on_off")}
    onpointermove={holdMove} onpointerup={clearHold} onpointercancel={clearHold} onclickcapture={holdClick}>
    {#if icon}<DeviceIcon type={icon} class="size-4 shrink-0 text-dida-text-muted" />{/if}
    <h3 class="truncate text-m font-semibold leading-tight">{name}</h3>
    {#if ondetail && members.length}
      <!-- Deliberately a BUTTON, not another gesture: tap acts and hold inspects a
           value are the settled vocabulary here, and a fourth gesture on an already
           saturated surface would be the wrong move. The header's hold skips a press
           that starts on a button, so pressing it doesn't arm hold-for-power-history. -->
      <Button size="small" label={t("detail.title")} title={t("detail.title")} onclick={() => ondetail?.(members[0].entityId)}>ⓘ</Button>
    {/if}
  </div>

  {#if climateMember}<ClimateCard device={climateMember} {onHistory} />{/if}
  {#if mediaMember}<div class="mt-1"><MediaPlayer device={mediaMember} compact /></div>{/if}
  {#if remoteMode}<div class="mt-1"><Remote {members} /></div>{/if}

  <div class="divide-y divide-dida-border/60">
    {#snippet control(f: Field)}
      <CapabilityControl
        entityId={f.entity.entityId}
        capability={f.cap}
        cs={f.cs}
        options={optionsFor(f)}
        range={rangeFor(f)}
        name={GENERIC.has(f.cap) || f.cap === "press" ? f.entity.name : undefined}
        sinceIso={f.cap === "identity_presence"
          ? String(f.entity.caps["identity_since"]?.value ?? "")
          : undefined}
      />
    {/snippet}
    {#each fields as f (f.entity.entityId + f.cap)}
      {#if CLICK_HISTORY.has(capMeta(f.cap).control) && onHistory}
        <!-- A row that only DISPLAYS has nothing else a click could mean, so the click
             IS the inspection — the same act as on a sensor anywhere else, and no
             button of its own. Holding still works, for the rows that also operate. -->
        <div role="button" tabindex="0" class="cursor-pointer"
          onclick={() => onHistory?.(f.entity.entityId, f.cap)}
          onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onHistory?.(f.entity.entityId, f.cap); } }}>
          {@render control(f)}
        </div>
      {:else}
        <!-- svelte-ignore a11y_no_static_element_interactions -- hold wrapper around a CapabilityControl, which renders the actual focusable control -->
        <div onpointerdown={(e) => holdDown(e, f)} onpointermove={holdMove} onpointerup={clearHold}
          onpointercancel={clearHold} onclickcapture={holdClick}>
          {@render control(f)}
        </div>
      {/if}
    {/each}
  </div>
</div>
