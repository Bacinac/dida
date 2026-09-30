<script lang="ts">
  import { onMount } from "svelte";
  import { api, type Zone } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { persons } from "$lib/presence";
  import { Button, Card, SaveButton, Tag, dialog, formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import ZoneMap from "$lib/ZoneMap.svelte";

  let zones = $state<Zone[]>([]);
  let nzName = $state("");
  let nzLat = $state("");
  let nzLon = $state("");
  let nzRadius = $state("100");
  let nzHome = $state(false);
  let zoneMsg = $state<string | null>(null);
  let editZoneId = $state<number | null>(null);
  let ezLat = $state("");
  let ezLon = $state("");
  let ezRadius = $state("");

  async function loadZones() {
    if (!auth.isAdmin) return;
    try { zones = await api.listZones(); } catch { zones = []; }
  }
  async function addZone() {
    zoneMsg = null;
    const name = nzName.trim();
    const latitude = Number(nzLat), longitude = Number(nzLon), radius_m = Number(nzRadius);
    if (!name || !Number.isFinite(latitude) || !Number.isFinite(longitude) || !(radius_m > 0)) { zoneMsg = t("zones.invalid"); return; }
    try {
      await api.createZone({ name, latitude, longitude, radius_m, is_home: nzHome });
      nzName = ""; nzLat = ""; nzLon = ""; nzRadius = "100"; nzHome = false;
      await loadZones();
    } catch (e) { zoneMsg = errMsg(e); }
  }
  function startEditZone(z: Zone) {
    editZoneId = z.id; ezLat = String(z.latitude); ezLon = String(z.longitude); ezRadius = String(z.radius_m); zoneMsg = null;
  }
  async function saveZone(z: Zone) {
    const latitude = Number(ezLat), longitude = Number(ezLon), radius_m = Number(ezRadius);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || !(radius_m > 0)) { zoneMsg = t("zones.invalid"); return; }
    try { await api.updateZone(z.id, { latitude, longitude, radius_m }); editZoneId = null; await loadZones(); }
    catch (e) { zoneMsg = errMsg(e); }
  }
  async function setHomeZone(z: Zone) {
    try { await api.updateZone(z.id, { is_home: true }); await loadZones(); }
    catch (e) { zoneMsg = errMsg(e); }
  }
  async function removeZone(z: Zone) {
    const ok = await dialog.confirm({ title: t("common.delete"), message: t("zones.confirmDelete", { name: z.name }), confirmLabel: t("common.delete"), danger: true });
    if (!ok) return;
    try { await api.deleteZone(z.id); await loadZones(); }
    catch (e) { zoneMsg = errMsg(e); }
  }
  async function createZoneAt(latitude: number, longitude: number) {
    zoneMsg = null;
    try { await api.createZone({ name: t("zones.newName"), latitude, longitude, radius_m: 100, is_home: false }); await loadZones(); }
    catch (e) { zoneMsg = errMsg(e); }
  }
  async function moveZone(id: number, latitude: number, longitude: number) {
    try { await api.updateZone(id, { latitude, longitude }); await loadZones(); }
    catch (e) { zoneMsg = errMsg(e); }
  }

  const people = $derived(
    persons(devices.list, Date.now())
      .filter((p) => p.lat !== null && p.lon !== null)
      .map((p) => ({ name: p.name, lat: p.lat as number, lon: p.lon as number, location: p.location, stale: p.stale })),
  );

  // Clicking a zone row flies the map to it (a fresh object re-triggers the map).
  let focusZone = $state<{ lat: number; lon: number } | null>(null);
  function focusOn(z: Zone) {
    focusZone = { lat: z.latitude, lon: z.longitude };
  }
  onMount(loadZones);
</script>

<svelte:head><title>{t("zones.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  <Card>
    <div class="mb-4"><ZoneMap {zones} {people} focus={focusZone} onCreate={createZoneAt} onMove={moveZone} /></div>
    <div class="mb-4 flex flex-wrap items-end gap-2">
      <input bind:value={nzName} placeholder={t("zones.name")}
        />
      <input bind:value={nzLat} inputmode="decimal" placeholder={t("zones.lat")}
        class="w-32 font-mono" />
      <input bind:value={nzLon} inputmode="decimal" placeholder={t("zones.lon")}
        class="w-32 font-mono" />
      <input bind:value={nzRadius} inputmode="numeric" placeholder={t("zones.radius")}
        class="w-24 font-mono" />
      <label class="flex items-center gap-1.5 text-m text-dida-text-muted">
        <input type="checkbox" bind:checked={nzHome} class="accent-dida-accent" />{t("zones.home")}
      </label>
      <Button tone="primary" onclick={addZone}>{t("zones.add")}</Button>
      {#if zoneMsg}<span class="text-s text-dida-danger">{zoneMsg}</span>{/if}
    </div>
    <table class="w-full text-m">
      <thead class="text-left text-s text-dida-text-muted">
        <tr>
          <th class="py-1 font-medium">{t("zones.name")}</th>
          <th class="py-1 font-medium">{t("zones.lat")}</th>
          <th class="py-1 font-medium">{t("zones.lon")}</th>
          <th class="py-1 font-medium">{t("zones.radius")}</th>
          <th class="py-1"></th>
        </tr>
      </thead>
      <tbody>
        {#each zones as z (z.id)}
          <tr class="border-t border-dida-border/60">
            <td class="py-1.5 font-medium">
              <button type="button" onclick={() => focusOn(z)} class="text-left hover:text-dida-accent" title={t("zones.zoomTo")}>{z.name}</button>
              {#if z.is_home}<Tag tone="busy">{t("zones.home")}</Tag>{/if}
            </td>
            {#if editZoneId === z.id}
              <td class="py-1.5"><input bind:value={ezLat} inputmode="decimal" class="w-28 font-mono" /></td>
              <td class="py-1.5"><input bind:value={ezLon} inputmode="decimal" class="w-28 font-mono" /></td>
              <td class="py-1.5"><input bind:value={ezRadius} inputmode="numeric" class="w-20 font-mono" /></td>
              <td class="py-1.5 text-right">
                <SaveButton size="small" dirty={!(Number(ezLat) === z.latitude && Number(ezLon) === z.longitude && Number(ezRadius) === z.radius_m)} onclick={() => saveZone(z)} />
                <Button size="small" onclick={() => { editZoneId = null; }} label={t("common.cancel")}>✕</Button>
              </td>
            {:else}
              <td class="py-1.5 font-mono tabular-nums">{formatNumber(z.latitude, { maximumFractionDigits: 5 })}</td>
              <td class="py-1.5 font-mono tabular-nums">{formatNumber(z.longitude, { maximumFractionDigits: 5 })}</td>
              <td class="py-1.5 font-mono tabular-nums">{formatNumber(z.radius_m, { maximumFractionDigits: 1 })}</td>
              <td class="py-1.5 text-right">
                {#if !z.is_home}
                  <Button size="small" onclick={() => setHomeZone(z)}>{t("zones.setHome")}</Button>
                {/if}
                <span class="ml-1"><Button size="small" onclick={() => startEditZone(z)}>{t("common.edit")}</Button></span>
                <span class="ml-1"><Button tone="danger" size="small" onclick={() => removeZone(z)}>{t("common.delete")}</Button></span>
              </td>
            {/if}
          </tr>
        {:else}
          <tr><td class="py-2 text-s text-dida-text-faint" colspan="5">{t("zones.empty")}</td></tr>
        {/each}
      </tbody>
    </table>
  </Card>
{/if}
