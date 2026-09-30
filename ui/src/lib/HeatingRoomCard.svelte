<script lang="ts">
  // One room on the Heating page: what it is doing now, a ± that holds it warmer
  // for an hour, and — folded away — the little that is actually worth setting.
  // The room's thermometer and valves are NOT asked for: they are whatever is
  // assigned to the room, which the server derives and this only displays.
  import { api, type HeatingRoom, type RoomHeatingConfig } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Field, SaveButton, Tag, Toggle, formatNumber, type TagTone } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { clock } from "$lib/dt";
  import {
    BOOST_MINUTES, DEFAULT_TARGETS, MAX_OFFSET, PROFILES, STEP, TARGET_MAX, TARGET_MIN,
    boost, clearBoost, effectiveTarget, emptyRoom, roomLive, valveLive,
  } from "$lib/heating";
  import HeatingSchedule from "$lib/HeatingSchedule.svelte";
  import { devices } from "$lib/store.svelte";

  let { room, house, onchange }: {
    room: HeatingRoom;
    house: Record<string, number>;   // the house setpoints this room is a variation on
    onchange: () => Promise<void>;
  } = $props();

  let open = $state(false);
  let busy = $state(false);
  let err = $state<string | null>(null);
  // The edit buffer: a copy, so Save stays disabled until something really moved.
  // Seeded by the effect below rather than from the prop directly, which would
  // capture only the value this component mounted with.
  let draft = $state<RoomHeatingConfig>(emptyRoom());
  const saved = $derived(JSON.stringify(room.config));
  const dirty = $derived(JSON.stringify(draft) !== saved);

  const live = $derived(roomLive(devices.byId, room.area_id));
  const label = $derived(room.name ?? (room.kind ? t(`room.kind.${room.kind}` as MessageKey) : `#${room.area_id}`));
  const override = $derived(
    draft.override_target !== null && (draft.override_until ?? 0) * 1000 > Date.now()
      ? draft.override_until
      : null,
  );
  const name = (id: string): string => devices.byId[id]?.name ?? id;
  const sensorNames = $derived(room.sensors.map(name).join(", "));
  const valveNames = $derived(room.valves.map(name).join(", "));

  // A fresh server copy replaces the buffer only when the setup panel is closed —
  // otherwise a reload landing mid-edit would eat what the user is typing.
  $effect(() => {
    const incoming = saved;
    if (!open) draft = JSON.parse(incoming);
  });

  const STATUS_TONE: Record<string, TagTone> = {
    heating: "warn",
    preheat: "warn",
    idle: "quiet",
    window: "busy",
    summer: "quiet",
    off: "quiet",
    stale: "err",
    no_sensor: "err",
    valve_error: "err",
  };
  const statusLabel = (s: string): string => (s ? t(`heating.status.${s}` as MessageKey) : "—");

  async function nudge(delta: number) {
    const base = live.target ?? effectiveTarget(house, "comfort", draft.offset);
    const next = Math.min(TARGET_MAX, Math.max(TARGET_MIN, base + delta));
    err = null;
    try { await boost(room.area_id, next); await onchange(); }
    catch (e) { err = errMsg(e); }
  }
  async function dropBoost() {
    err = null;
    try { await clearBoost(room.area_id); await onchange(); }
    catch (e) { err = errMsg(e); }
  }
  async function save() {
    busy = true; err = null;
    try { await api.setHeatingRoom(room.area_id, $state.snapshot(draft)); await onchange(); }
    catch (e) { err = errMsg(e); }
    finally { busy = false; }
  }
  function untilLabel(until: number | null): string {
    if (until === null) return "";
    return clock(until * 1000);
  }
</script>

<Card>
  <div class="flex items-start justify-between gap-3">
    <div class="min-w-0">
      <p class="truncate font-medium">{label}</p>
      <div class="mt-1"><Tag tone={STATUS_TONE[live.status] ?? "quiet"}>{statusLabel(live.status)}</Tag></div>
    </div>
    <div class="text-right">
      <p class="text-2xl font-semibold leading-none tabular-nums">
        {live.temperature === null ? "—" : `${formatNumber(live.temperature, { maximumFractionDigits: 1 })}°`}
      </p>
      <p class="mt-1 text-s text-dida-text-muted tabular-nums">
        {t("heating.target")}: {live.target === null ? "—" : `${formatNumber(live.target, { maximumFractionDigits: 1 })}°`}
      </p>
    </div>
  </div>

  {#if auth.canControl}
    <div class="mt-3 flex items-center gap-2">
      <Button size="small" onclick={() => nudge(-STEP)} label={t("heating.cooler")}>−</Button>
      <Button size="small" onclick={() => nudge(STEP)} label={t("heating.warmer")}>+</Button>
      {#if override !== null}
        <span class="text-s text-dida-text-muted">{t("heating.until", { time: untilLabel(override) })}</span>
        <Button size="small" onclick={dropBoost}>{t("heating.clearBoost")}</Button>
      {:else}
        <span class="text-s text-dida-text-faint">{t("heating.boostHint", { minutes: BOOST_MINUTES })}</span>
      {/if}
    </div>
  {/if}

  <!-- What the room contains, stated rather than asked for. -->
  <p class="mt-2 truncate text-xs text-dida-text-faint" title="{sensorNames} · {valveNames}">
    {sensorNames || t("heating.noRoomSensor")} · {valveNames}
  </p>

  {#each room.valves as valve (valve)}
    {@const v = valveLive(devices.byId, valve)}
    {#if v.batteryLow || !v.reporting || v.windowOpen}
      <p class="mt-1 text-s text-dida-warn">
        {name(valve)}:
        {#if !v.reporting}{t("heating.valveSilent")}{:else if v.batteryLow}{t("heating.valveBattery")}{:else}{t("heating.valveWindow")}{/if}
      </p>
    {/if}
  {/each}

  {#if auth.isAdmin}
    <div class="mt-3 border-t border-dida-border/60 pt-2">
      <Button size="small" selected={open} onclick={() => (open = !open)}>
        {open ? t("heating.hideSetup") : t("heating.setup")}
      </Button>
    </div>
  {/if}

  {#if open}
    <div class="mt-3 space-y-3">
      <div class="flex items-start gap-2">
        <Toggle size="small" checked={draft.enabled} onclick={() => (draft.enabled = !draft.enabled)}
          label={t("heating.roomEnabled")} />
        <div>
          <p class="text-m">{t("heating.roomEnabled")}</p>
          <p class="text-xs text-dida-text-faint">{t("heating.roomEnabledHint")}</p>
        </div>
      </div>

      <div class="flex items-start gap-2">
        <Toggle size="small" checked={draft.can_call_boiler}
          onclick={() => (draft.can_call_boiler = !draft.can_call_boiler)}
          label={t("heating.canCallBoiler")} />
        <div>
          <p class="text-m">{t("heating.canCallBoiler")}</p>
          <p class="text-xs text-dida-text-faint">{t("heating.canCallBoilerHint")}</p>
        </div>
      </div>

      <Field label={t("heating.offset")} hint={t("heating.offsetHint")}>
        <div class="flex flex-wrap items-center gap-3">
          <input type="number" step={STEP} min={-MAX_OFFSET} max={MAX_OFFSET}
            value={draft.offset}
            onchange={(e) => (draft.offset = Number(e.currentTarget.value))}
            class="w-20 tabular-nums" />
          <span class="text-xs text-dida-text-faint tabular-nums">
            {#each PROFILES as p (p)}
              {t(`heating.profile.${p}` as MessageKey)} {formatNumber(effectiveTarget(house, p, draft.offset), { maximumFractionDigits: 1 })}°&nbsp;
            {/each}
          </span>
        </div>
      </Field>

      <!-- Empty = the house schedule. A room only carries one when it disagrees. -->
      <HeatingSchedule bind:slots={draft.schedule} inheritLabel={t("heating.scheduleFromHouse")} />

      <div class="flex items-center gap-2">
        <Toggle size="small" checked={draft.window_pause} onclick={() => (draft.window_pause = !draft.window_pause)}
          label={t("heating.windowPause")} />
        <span class="text-m">{t("heating.windowPause")}</span>
      </div>

      <SaveButton size="small" {dirty} saving={busy} onclick={save} />
    </div>
  {/if}

  {#if err}<p class="mt-2 text-s text-dida-danger">{err}</p>{/if}
</Card>
