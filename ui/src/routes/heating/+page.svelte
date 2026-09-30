<script lang="ts">
  // Heating: one page that holds the whole system — what every room is doing right
  // now, and everything it takes to set it up. The live half is read from the
  // `heating:*` entities the controller publishes; the config half is the only
  // thing this page writes. Rooms are not "added": a room with a thermostatic valve
  // in it IS a heated room, and the server says which those are.
  import { onMount } from "svelte";
  import { api, type HeatingSettings, type HeatingView } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Field, Notice, SaveButton, Toggle, formatNumber } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";
  import {
    DEFAULT_TARGETS, MODES, PROFILES, STEP, TARGET_MAX, TARGET_MIN,
    boilerRunning, roomsCalling,
  } from "$lib/heating";
  import HeatingSchedule from "$lib/HeatingSchedule.svelte";
  import HeatingRoomCard from "$lib/HeatingRoomCard.svelte";
  import { devices } from "$lib/store.svelte";
  import { Section } from "$lib/ui";

  let view = $state<HeatingView | null>(null);
  let draft = $state<HeatingSettings | null>(null);
  let err = $state<string | null>(null);
  let busy = $state(false);
  let advanced = $state(false);

  const savedSettings = $derived(view ? JSON.stringify(view.settings) : "");
  const dirty = $derived(draft !== null && JSON.stringify(draft) !== savedSettings);

  // The only entity choices left, and all three are house-wide by nature: the relay
  // that fires the boiler, the thermometer that decides the season is over, and the
  // flag that says the house is empty. Each goes through the searchable picker —
  // sixty switches in a plain dropdown is a haystack, and the boiler is the needle.
  const relayItems = $derived(
    Object.values(devices.byId).filter((d) => "on_off" in d.caps).map(pickerItem),
  );
  const outdoorItems = $derived(
    Object.values(devices.byId)
      .filter((d) => "temperature" in d.caps && !("on_off" in d.caps) && !("target_temperature" in d.caps))
      .map(pickerItem),
  );
  const booleanItems = $derived(
    Object.values(devices.byId)
      .filter((d) => "boolean" in d.caps || "binary" in d.caps)
      .map(pickerItem),
  );
  const outdoorTemp = $derived(() => {
    const id = view?.settings.outdoor;
    const v = id ? devices.byId[id]?.caps["temperature"]?.value : undefined;
    return typeof v === "number" ? v : null;
  });

  async function load() {
    try {
      view = await api.getHeating();
      draft = JSON.parse(JSON.stringify(view.settings));
      err = null;
    } catch (e) { err = errMsg(e); }
  }
  async function saveSettings() {
    if (!draft) return;
    busy = true; err = null;
    try { await api.setHeatingSettings($state.snapshot(draft)); await load(); }
    catch (e) { err = errMsg(e); }
    finally { busy = false; }
  }
  onMount(load);
</script>

<svelte:head><title>{t("heating.title")}</title></svelte:head>

{#if err}<Notice tone="err">{err}</Notice>{/if}

{#if view && draft}
  <Card>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        {#if auth.isAdmin}
          <Toggle checked={draft.enabled} onclick={() => (draft && (draft.enabled = !draft.enabled))}
            label={t("heating.enabled")} />
        {/if}
        <div>
          <p class="font-medium">{draft.enabled ? t("heating.enabled") : t("heating.disabled")}</p>
          <p class="text-s text-dida-text-muted">
            {#if boilerRunning(devices.byId)}
              {t("heating.boilerOn", { n: roomsCalling(devices.byId) })}
            {:else}
              {t("heating.boilerOff")}
            {/if}
            {#if outdoorTemp() !== null}
              · {t("heating.outdoor")}: {formatNumber(outdoorTemp() as number, { maximumFractionDigits: 1 })}°
            {/if}
          </p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <select bind:value={draft.mode} disabled={!auth.isAdmin}>
          {#each MODES as m (m)}<option value={m}>{t(`heating.mode.${m}` as MessageKey)}</option>{/each}
        </select>
        {#if auth.isAdmin}<SaveButton size="small" {dirty} saving={busy} onclick={saveSettings} />{/if}
      </div>
    </div>

    {#if auth.isAdmin}
      <div class="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field label={t("heating.boiler")} hint={t("heating.boilerHint")}>
          <EntityPicker bind:value={draft.boiler} items={relayItems} placeholder={t("heating.pickBoiler")} />
        </Field>
        <Field label={t("heating.outdoorSensor")}>
          <EntityPicker bind:value={draft.outdoor} items={outdoorItems} placeholder={t("heating.pickOutdoor")} />
        </Field>
        <Field label={t("heating.summerCutoff")} hint={t("heating.summerCutoffHint")}>
          <input type="number" step="0.5" bind:value={draft.summer_cutoff} />
        </Field>
      </div>

      <!-- The house's own programme. Rooms are variations on this, not eight
           independent copies of it. -->
      <div class="mt-4 border-t border-dida-border/60 pt-3">
        <Field label={t("heating.houseTargets")} hint={t("heating.houseTargetsHint")}>
          <div class="flex flex-wrap gap-2">
            {#each PROFILES as p (p)}
              <label class="flex items-center gap-1 text-s text-dida-text-muted">
                {t(`heating.profile.${p}` as MessageKey)}
                <input type="number" step={STEP} min={TARGET_MIN} max={TARGET_MAX}
                  value={draft.targets[p] ?? DEFAULT_TARGETS[p]}
                  onchange={(e) => draft && (draft.targets = { ...draft.targets, [p]: Number(e.currentTarget.value) })}
                  class="w-16 tabular-nums" />
              </label>
            {/each}
          </div>
        </Field>
        <div class="mt-3">
          <HeatingSchedule bind:slots={draft.schedule} />
        </div>
      </div>

      <div class="mt-3">
        <Button size="small" selected={advanced} onclick={() => (advanced = !advanced)}>
          {advanced ? t("heating.hideAdvanced") : t("heating.advanced")}
        </Button>
      </div>

      {#if advanced}
        <div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Field label={t("heating.hysteresis")} hint={t("heating.hysteresisHint")}>
            <input type="number" step="0.1" bind:value={draft.hysteresis} />
          </Field>
          <Field label={t("heating.deadband")}>
            <input type="number" step="0.1" bind:value={draft.deadband} />
          </Field>
          <Field label={t("heating.minCalling")} hint={t("heating.minCallingHint")}>
            <input type="number" step="1" min="1" max="20" bind:value={draft.min_calling}
              />
          </Field>
          <Field label={t("heating.minOn")} hint={t("heating.cyclingHint")}>
            <input type="number" step="30" bind:value={draft.min_on_s} />
          </Field>
          <Field label={t("heating.minOff")}>
            <input type="number" step="30" bind:value={draft.min_off_s} />
          </Field>
          <Field label={t("heating.frost")} hint={t("heating.frostHint")}>
            <input type="number" step="0.5" bind:value={draft.frost_target} />
          </Field>
          <Field label={t("heating.staleAfter")} hint={t("heating.staleHint")}>
            <input type="number" step="600" bind:value={draft.stale_after_s} />
          </Field>
          <Field label={t("heating.awayHelper")} hint={t("heating.awayHelperHint")}>
            <EntityPicker bind:value={draft.away_helper} items={booleanItems}
              placeholder={t("heating.pickHelper")} />
          </Field>
          <Field label={t("heating.preheatRate")} hint={t("heating.preheatHint")}>
            <input type="number" step="1" min="1" max="120" bind:value={draft.preheat_rate}
              />
          </Field>
          <Field label={t("heating.frostPerDegree")} hint={t("heating.frostPerDegreeHint")}>
            <input type="number" step="0.1" min="0" max="2" bind:value={draft.frost_per_degree}
              />
          </Field>
          <div class="flex items-center gap-2 self-end">
            <Toggle size="small" checked={draft.preheat}
              onclick={() => (draft && (draft.preheat = !draft.preheat))}
              label={t("heating.preheat")} />
            <span class="text-m">{t("heating.preheat")}</span>
          </div>
          <Field label={t("heating.windowDrop")} hint={t("heating.windowDropHint")}>
            <input type="number" step="0.1" bind:value={draft.window_drop} />
          </Field>
          <Field label={t("heating.windowMinutes")}>
            <input type="number" step="1" min="2" max="60" bind:value={draft.window_minutes}
              />
          </Field>
          <div class="flex items-center gap-2 self-end">
            <Toggle size="small" checked={draft.window_detect}
              onclick={() => (draft && (draft.window_detect = !draft.window_detect))}
              label={t("heating.windowDetect")} />
            <span class="text-m">{t("heating.windowDetect")}</span>
          </div>
          <div class="flex items-center gap-2 self-end">
            <Toggle size="small" checked={draft.force_manual}
              onclick={() => (draft && (draft.force_manual = !draft.force_manual))}
              label={t("heating.forceManual")} />
            <span class="text-m">{t("heating.forceManual")}</span>
          </div>
          <div class="flex items-center gap-2 self-end">
            <Toggle size="small" checked={draft.require_open_valve}
              onclick={() => (draft && (draft.require_open_valve = !draft.require_open_valve))}
              label={t("heating.requireOpenValve")} />
            <span class="text-m">{t("heating.requireOpenValve")}</span>
          </div>
        </div>
      {/if}
    {/if}
  </Card>

  <Section title={t("heating.rooms")}>
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {#each view.rooms as room (room.area_id)}
        <HeatingRoomCard {room} house={draft.targets} onchange={load} />
      {:else}
        <Card><p class="text-m text-dida-text-faint">{t("heating.noRooms")}</p></Card>
      {/each}
    </div>

    {#if view.orphan_valves.length}
      <!-- A head in no room can't be heated, and the fix lives in Areas, not here. -->
      <p class="mt-3 text-s text-dida-warn">
        {t("heating.orphanValves", {
          names: view.orphan_valves.map((v) => devices.byId[v]?.name ?? v).join(", "),
        })}
        {#if auth.isAdmin}<a class="underline" href="/settings/areas">{t("nav.rooms")}</a>{/if}
      </p>
    {/if}
  </Section>
{:else if !err}
  <Notice>{t("common.loading")}</Notice>
{/if}
