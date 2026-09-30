<script lang="ts">
  import { auth } from "$lib/auth.svelte";
  import { Button, formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import type { Device } from "$lib/store.svelte";
  import {
    HVAC_MODES, FAN_MODES, TEMP_MIN, TEMP_MAX,
    hvacMode, fanMode, targetTemp, currentTemp, isOff,
    setHvacMode, setFanMode, setTargetTemp,
  } from "$lib/climate";

  let { device, onHistory }: { device: Device; onHistory?: (entityId: string, cap: string) => void } = $props();
  const readOnly = $derived(!auth.canControl); // view-only user: no actuation

  // Hold on the current-temperature readout → its history (floorplan popover only;
  // pages that don't pass onHistory get a plain readout).
  let holdTimer: ReturnType<typeof setTimeout> | null = null;
  let holdX = 0, holdY = 0;
  const clearHold = () => { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } };
  function holdDown(e: PointerEvent) {
    if (!onHistory) return;
    holdX = e.clientX; holdY = e.clientY;
    clearHold();
    holdTimer = setTimeout(() => onHistory(device.entityId, "temperature"), 450);
  }
  function holdMove(e: PointerEvent) {
    if (holdTimer && Math.hypot(e.clientX - holdX, e.clientY - holdY) > 8) clearHold();
  }

  const mode = $derived(hvacMode(device));
  const fan = $derived(fanMode(device));
  const target = $derived(targetTemp(device));
  const current = $derived(currentTemp(device));

  function hvacLabel(m: string): string {
    switch (m) {
      case "off": return t("hvac.off");
      case "cool": return t("hvac.cool");
      case "heat": return t("hvac.heat");
      case "auto": return t("hvac.auto");
      case "dry": return t("hvac.dry");
      default: return t("hvac.fan_only");
    }
  }
  function fanLabel(f: string): string {
    switch (f) {
      case "auto": return t("fan.auto");
      case "low": return t("fan.low");
      case "medium": return t("fan.medium");
      case "high": return t("fan.high");
      case "silent": return t("fan.silent");
      default: return t("fan.max");
    }
  }

  function nudge(delta: number) {
    const base = target ?? 22;
    const next = Math.min(TEMP_MAX, Math.max(TEMP_MIN, Math.round(base + delta)));
    setTargetTemp(device.entityId, next);
  }
</script>

<div class="flex flex-col gap-3 py-2">
  <!-- current temperature + mode -->
  <div class="flex items-end justify-between">
    <!-- svelte-ignore a11y_no_static_element_interactions -- hold-to-inspect is a SECOND affordance over a card whose controls are real buttons; the primary path is unaffected -->
    <div onpointerdown={holdDown} onpointermove={holdMove} onpointerup={clearHold} onpointercancel={clearHold}>
      <p class="text-xs uppercase tracking-wide text-dida-text-muted">{t("climate.current")}</p>
      <p class="text-2xl font-semibold leading-none">
        {current === null ? "—" : `${formatNumber(current, { maximumFractionDigits: 1 })}°`}
      </p>
    </div>
    <select
      value={mode} disabled={readOnly}
      onchange={(e) => setHvacMode(device.entityId, e.currentTarget.value)}
     
      aria-label={t("climate.mode")}
    >
      {#each HVAC_MODES as m (m)}<option value={m}>{hvacLabel(m)}</option>{/each}
    </select>
  </div>

  <!-- target setpoint stepper -->
  <div class="flex items-center justify-between gap-2" class:opacity-50={isOff(device)}>
    <span class="text-s text-dida-text-muted">{t("climate.target")}</span>
    <div class="flex items-center gap-2">
      <Button size="small" onclick={() => nudge(-1)} disabled={isOff(device) || readOnly} label={t("heating.cooler")}>−</Button>
      <span class="w-12 text-center text-xl font-semibold">{target === null ? "—" : `${formatNumber(target, { maximumFractionDigits: 1 })}°`}</span>
      <Button size="small" onclick={() => nudge(1)} disabled={isOff(device) || readOnly} label={t("heating.warmer")}>+</Button>
    </div>
  </div>

  <!-- fan -->
  <div class="flex items-center justify-between gap-2" class:opacity-50={isOff(device)}>
    <span class="text-s text-dida-text-muted">{t("climate.fan")}</span>
    <select
      value={fan} disabled={isOff(device) || readOnly}
      onchange={(e) => setFanMode(device.entityId, e.currentTarget.value)}
     
      aria-label={t("climate.fan")}
    >
      {#each FAN_MODES as f (f)}<option value={f}>{fanLabel(f)}</option>{/each}
    </select>
  </div>
</div>
