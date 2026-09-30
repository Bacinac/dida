<script lang="ts">
  // The entry surface: the property's access points (car gate, pedestrian gate,
  // door lock) as large one-tap actions. Built as a reusable widget so it can
  // also drop onto the wall panel. The tiles come from GET /entry/config (server-
  // resolved, so a locked-down guest renders them without the full device store);
  // actuation goes through /entry/action, which enforces the same per-user
  // boundary as /command. Each tile can show a LIVE status badge from a separate
  // state source (the door's Zigbee contact, a gate's BABA occupancy zone).
  // Admins get an inline editor to pick the backing devices.
  import { onMount } from "svelte";
  import { Button, Notice, SaveButton, Tag, dialog } from "$lib/kit";
  import { api, type EntryConfig, type EntrySlot } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { devices } from "$lib/store.svelte";
  import { t } from "$lib/i18n";
  import { tr } from "$lib/translations.svelte";
  import { errMsg } from "$lib/errors";
  import { LABEL_CLASS, SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import EntityPicker, { pickerItem, type PickerItem } from "$lib/EntityPicker.svelte";

  let cfg = $state<EntryConfig | null>(null);
  let err = $state<string | null>(null);
  let loaded = $state(false);

  // Per-slot transient UI state (open in flight / brief "opened ✓" flash).
  let busy = $state<Record<string, boolean>>({});
  let flash = $state<Record<string, boolean>>({});

  const SLOT_ICON: Record<EntrySlot["slot"], string> = { car: "car", pedestrian: "gate", door: "door" };

  async function load() {
    try {
      cfg = await api.entryConfig();
      err = null;
    } catch (e) {
      err = errMsg(e);
    } finally {
      loaded = true;
    }
  }
  onMount(load);

  // Live status for a slot from its state source (a DIFFERENT entity than the
  // actuator). Primary wins whenever its entity is live in the store; a dead
  // sensor simply isn't there, so it falls through to the fallback. No staleness
  // logic — absence, not age, is the signal. The cap decides the wording:
  // contact/open_close → open/closed; occupancy (a BABA gate zone) → active/clear;
  // scene_state → the evaluated label, localised like any adapter descriptor.
  type Status = { label: string; alert: boolean };
  function slotStatus(s: EntrySlot): Status | null {
    for (const id of [s.state_entity, s.state_fallback]) {
      if (!id) continue;
      const caps = devices.byId[id]?.caps;
      if (!caps) continue;
      const contact = caps["contact"]?.value;
      if (typeof contact === "boolean") return { label: contact ? t("entry.state.open") : t("entry.state.closed"), alert: contact };
      const oc = caps["open_close"]?.value;
      if (typeof oc === "number") return { label: oc > 0 ? t("entry.state.open") : t("entry.state.closed"), alert: oc > 0 };
      const occ = caps["occupancy"]?.value;
      if (typeof occ === "boolean") return { label: occ ? t("entry.state.active") : t("entry.state.clear"), alert: occ };
      const scene = caps["scene_state"]?.value;
      if (typeof scene === "string" && scene) return { label: tr(scene), alert: /open|otvor/i.test(scene) };
    }
    return null;
  }
  const statuses = $derived.by(() => {
    const m: Record<string, Status | null> = {};
    for (const s of cfg?.slots ?? []) m[s.slot] = slotStatus(s);
    return m;
  });

  async function open(slot: EntrySlot["slot"]) {
    if (busy[slot]) return;
    // The door physically opens on unlock and cannot be closed remotely — a
    // deliberate, honest danger-confirm guards the only irreversible action.
    if (slot === "door") {
      const ok = await dialog.confirm({
        title: t("entry.door.confirmTitle"),
        message: t("entry.door.confirmMsg"),
        confirmLabel: t("entry.door.confirmBtn"),
        danger: true,
      });
      if (!ok) return;
    }
    busy = { ...busy, [slot]: true };
    err = null;
    try {
      await api.entryAction(slot);
      flash = { ...flash, [slot]: true };
      setTimeout(() => { flash = { ...flash, [slot]: false }; }, 2000);
    } catch (e) {
      err = errMsg(e);
    } finally {
      busy = { ...busy, [slot]: false };
    }
  }

  // ── admin editor: which device backs each slot + its status source ──────────
  let editing = $state(false);
  let fCar = $state(""), fPed = $state(""), fDoor = $state("");
  let fStateCar = $state(""), fStatePed = $state(""), fStateDoor = $state(""), fStateDoorFb = $state("");
  let fNotify = $state(true);
  let saving = $state(false);
  let savedFlash = $state(false);

  function slot(k: EntrySlot["slot"]): EntrySlot | undefined {
    return cfg?.slots.find((s) => s.slot === k);
  }
  // Seed the editor fields from the loaded config (and re-seed after a save, which
  // resets the dirty state). Only reads cfg, so no feedback loop with the fields.
  $effect(() => {
    if (!cfg) return;
    fCar = slot("car")?.entity_id ?? "";
    fPed = slot("pedestrian")?.entity_id ?? "";
    fDoor = slot("door")?.entity_id ?? "";
    fStateCar = slot("car")?.state_entity ?? "";
    fStatePed = slot("pedestrian")?.state_entity ?? "";
    fStateDoor = slot("door")?.state_entity ?? "";
    fStateDoorFb = slot("door")?.state_fallback ?? "";
    fNotify = cfg.notify_on_open;
  });

  const dirty = $derived.by(() => {
    if (!cfg) return false;
    return fCar !== (slot("car")?.entity_id ?? "")
      || fPed !== (slot("pedestrian")?.entity_id ?? "")
      || fDoor !== (slot("door")?.entity_id ?? "")
      || fStateCar !== (slot("car")?.state_entity ?? "")
      || fStatePed !== (slot("pedestrian")?.state_entity ?? "")
      || fStateDoor !== (slot("door")?.state_entity ?? "")
      || fStateDoorFb !== (slot("door")?.state_fallback ?? "")
      || fNotify !== cfg.notify_on_open;
  });

  // Actuator candidates = plausible ACCESS hardware only: relays/covers/locks/gate
  // buttons (device_type switch/cover/lock/other with an openable cap). This drops
  // the house lights, media players, sensors and the harmony/androidtv remote
  // buttons that otherwise flood the list. A gate relay is indistinguishable from
  // an irrigation relay, so ~20 is the floor — the picker's search ("gate"/
  // "entrance") narrows the rest.
  const ACCESS_TYPES = new Set(["switch", "cover", "lock", "other"]);
  const actionItems = $derived(
    devices.list
      .filter((d) =>
        ACCESS_TYPES.has(d.deviceType ?? "other")
        && ["on_off", "open_close", "lock", "press"].some((c) => d.capabilities.includes(c)))
      .map(pickerItem),
  );
  // Status-source candidates: anything that reports an open/closed or presence
  // state (a door contact, a BABA occupancy zone, a scene evaluation).
  const stateItems = $derived(
    devices.list
      .filter((d) => ["contact", "occupancy", "open_close", "scene_state"].some((c) => d.capabilities.includes(c)))
      .map(pickerItem),
  );

  async function save() {
    saving = true;
    err = null;
    try {
      await api.setEntryConfig({
        car: fCar || null,
        pedestrian: fPed || null,
        door: fDoor || null,
        state_car: fStateCar || null,
        state_pedestrian: fStatePed || null,
        state_door: fStateDoor || null,
        state_door_fallback: fStateDoorFb || null,
        notify_on_open: fNotify,
      });
      await load();
      savedFlash = true;
      setTimeout(() => (savedFlash = false), 1500);
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = false;
    }
  }
</script>

<!-- One labelled picker row (custom control → caption is a span, not a <label>). -->
{#snippet prow(label: string, value: string, onpick: (id: string) => void, items: PickerItem[], hint?: string)}
  <div>
    <span class="{LABEL_CLASS} block">{label}</span>
    <div class="flex items-center gap-2">
      <div class="min-w-0 flex-1 sm:max-w-xs">
        <EntityPicker {value} {items} onchange={onpick} />
      </div>
      {#if value}
        <Button size="small" label={t("common.clear")} onclick={() => onpick("")}>×</Button>
      {/if}
    </div>
    {#if hint}<p class="mt-1 text-s text-dida-text-faint">{hint}</p>{/if}
  </div>
{/snippet}

<div class="mx-auto max-w-2xl">
  {#if err}
    <Notice tone="err">{err}</Notice>
  {/if}

  {#if cfg && cfg.slots.length > 0}
    <div class="grid gap-3 sm:grid-cols-2">
      {#each cfg.slots as s (s.slot)}
        {@const isDoor = s.slot === "door"}
        {@const st = statuses[s.slot]}
        <div class="flex flex-col gap-3 rounded-xl border p-4
                    {isDoor ? 'border-dida-warn/30 bg-dida-warn/5' : 'border-dida-border bg-dida-panel-2'}">
          <div class="flex items-center gap-3">
            <span class="grid size-11 shrink-0 place-items-center rounded-lg
                         {isDoor ? 'bg-dida-warn/15 text-dida-warn' : 'bg-dida-accent/15 text-dida-accent'}">
              <DeviceIcon type={SLOT_ICON[s.slot]} class="size-6" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate font-semibold">{t(`entry.slot.${s.slot}` as Parameters<typeof t>[0])}</p>
              <p class="truncate text-s text-dida-text-faint">{s.name}</p>
            </div>
            {#if st}
              <Tag tone={st.alert ? "warn" : "quiet"}>{st.label}</Tag>
            {/if}
          </div>
          <!-- No coarse canControl gate here: this surface targets baseline-deny +
               flip-allow logins, so the server (/entry/action) is the real boundary. -->
          <button
            type="button" onclick={() => open(s.slot)} disabled={busy[s.slot]}
            class="w-full rounded-lg py-3 text-center text-l font-semibold transition-colors disabled:opacity-60
                   {isDoor ? 'bg-dida-warn text-dida-bg hover:bg-dida-warn/90' : 'bg-dida-accent-strong text-dida-on-accent hover:bg-dida-accent-strong/90'}"
          >
            {#if flash[s.slot]}✓ {t("entry.opened")}
            {:else if busy[s.slot]}{t("entry.opening")}
            {:else}{isDoor ? t("entry.door.confirmBtn") : t("entry.open")}{/if}
          </button>
        </div>
      {/each}
    </div>
  {:else if loaded}
    <div class="rounded-xl border border-dashed border-dida-border p-8 text-center text-dida-text-muted">
      <p>{t("entry.empty")}</p>
      {#if auth.isAdmin}<p class="mt-1 text-m text-dida-text-faint">{t("entry.emptyHint")}</p>{/if}
    </div>
  {/if}

  {#if auth.isAdmin}
    <div class="mt-6">
      <Button size="small" onclick={() => (editing = !editing)}>
        {editing ? t("entry.editClose") : t("entry.edit")}
      </Button>

      {#if editing}
        <div class="mt-3 space-y-5 rounded-xl border border-dida-border bg-dida-panel p-4">
          <!-- actuators -->
          <div class="space-y-3">
            <p class="{SUBSECTION_TITLE_CLASS}">{t("entry.cfg.opener")}</p>
            {@render prow(t("entry.slot.car"), fCar, (id) => (fCar = id), actionItems)}
            {@render prow(t("entry.slot.pedestrian"), fPed, (id) => (fPed = id), actionItems)}
            {@render prow(t("entry.slot.door"), fDoor, (id) => (fDoor = id), actionItems)}
          </div>

          <!-- status sources (optional) -->
          <div class="space-y-3 border-t border-dida-border/60 pt-4">
            <p class="{SUBSECTION_TITLE_CLASS}">{t("entry.cfg.status")}</p>
            <p class="-mt-1 text-s text-dida-text-faint">{t("entry.cfg.statusHint")}</p>
            {@render prow(t("entry.slot.car"), fStateCar, (id) => (fStateCar = id), stateItems)}
            {@render prow(t("entry.slot.pedestrian"), fStatePed, (id) => (fStatePed = id), stateItems)}
            {@render prow(t("entry.slot.door"), fStateDoor, (id) => (fStateDoor = id), stateItems)}
            {@render prow(t("entry.cfg.statusFallback"), fStateDoorFb, (id) => (fStateDoorFb = id), stateItems, t("entry.cfg.statusFallbackHint"))}
          </div>

          <label class="flex items-center gap-2 border-t border-dida-border/60 pt-4 text-m">
            <input type="checkbox" bind:checked={fNotify} class="accent-dida-accent" />
            {t("entry.cfg.notify")}
          </label>

          <div class="flex items-center gap-3">
            <SaveButton size="small" {dirty} {saving} onclick={save} />
            {#if savedFlash}<span class="text-s text-dida-ok">✓ {t("entry.cfg.saved")}</span>{/if}
          </div>
        </div>
      {/if}
    </div>
  {/if}
</div>
