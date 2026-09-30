<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Card, dialog } from "$lib/kit";
  import { api, type Area } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { t, type MessageKey } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";

  // A room's kind drives its fallback name and floor-plan icon.
  const KINDS = [
    "bedroom", "bathroom", "kitchen", "living", "dining", "toilet", "hallway",
    "entrance", "backdoor", "stairs", "downstairs", "upstairs", "office", "garage",
    "gate", "patio", "outdoor", "garden", "utility",
    "cellar", "attic", "balcony", "laundry", "pantry", "workshop", "boiler_room",
    "dressing_room", "pool", "other",
  ];

  let newName = $state("");
  let msg = $state<string | null>(null);
  let busy = $state(false);

  // Alphabetical by the SHOWN name (custom name or translated type label) — sorted
  // client-side because most areas display via their i18n type, which the DB can't
  // order. Re-sorts on language change (label() is reactive to the locale).
  const sortedAreas = $derived(
    [...devices.areas].sort((a, b) => label(a).localeCompare(label(b), undefined, { sensitivity: "base" })),
  );
  // Type dropdown, alphabetical by translated label with the "other" catch-all last.
  const kindOptions = $derived(
    KINDS.filter((k) => k !== "other")
      .sort((a, b) =>
        t(`room.kind.${a}` as MessageKey).localeCompare(t(`room.kind.${b}` as MessageKey), undefined, { sensitivity: "base" }),
      )
      .concat("other"),
  );

  async function load() {
    if (!auth.isAdmin) return;
    try { await devices.refreshAreas(); } catch { /* transient — keep the last known list */ }
  }
  // Deleting a room reassigns its members, and auto-assign moves many at once:
  // the entities' own area_id changed, not just the room list, so resync both.
  async function reloadAll() {
    if (!auth.isAdmin) return;
    try { await devices.load(); } catch { /* transient */ }
  }
  function label(a: Area): string {
    return a.name ?? (a.kind ? t(`room.kind.${a.kind}` as MessageKey) : "—");
  }
  function memberCount(id: number): number {
    // Count physical DEVICES, not entities — one device now spans many entities
    // (a primary reading + its hidden facets), so filtering the entity list would
    // wildly overcount. Group by device_key (fall back to entity_id if ungrouped).
    const keys = new Set<string>();
    for (const d of devices.list) {
      if (d.areaId === id) keys.add(d.deviceKey ?? d.entityId);
    }
    return keys.size;
  }
  async function addArea() {
    msg = null;
    const name = newName.trim();
    if (!name) { msg = t("rooms.needName"); return; }
    try { await api.createArea(name); newName = ""; await load(); }
    catch (e) { msg = errMsg(e); }
  }
  function kindLabel(a: Area): string {
    return a.kind ? t(`room.kind.${a.kind}` as MessageKey) : "";
  }
  // The field shows the EFFECTIVE name (custom name, or the type label as a real
  // value — not a greyed placeholder that reads as "empty"). Typing the type's own
  // label (or clearing) means "no custom name" → store null so it stays generic.
  async function rename(a: Area, value: string) {
    const v = value.trim();
    const name = !v || v === kindLabel(a) ? null : v;
    if (name === (a.name ?? null)) return;   // no real change → skip
    msg = null;
    try { await api.updateArea(a.id, { name }); await load(); }
    catch (e) { msg = errMsg(e); }
  }
  async function setKind(a: Area, value: string) {
    const kind = value || null;
    if (kind === (a.kind ?? null)) return;
    msg = null;
    try { await api.updateArea(a.id, { kind }); await load(); }
    catch (e) { msg = errMsg(e); }
  }
  async function remove(a: Area) {
    const n = memberCount(a.id);
    const ok = await dialog.confirm({
      title: t("common.delete"),
      message: n > 0 ? t("rooms.confirmDeleteMembers", { name: label(a), n }) : t("rooms.confirmDelete", { name: label(a) }),
      confirmLabel: t("common.delete"), danger: true,
    });
    if (!ok) return;
    try { await api.deleteArea(a.id); await reloadAll(); }
    catch (e) { msg = errMsg(e); }
  }
  async function autoAssign() {
    busy = true; msg = null;
    try { await api.autoAssignAreas(); await reloadAll(); }
    catch (e) { msg = errMsg(e); }
    finally { busy = false; }
  }
  onMount(load);
</script>

<svelte:head><title>{t("rooms.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  <Card>

    <div class="mb-4 flex flex-wrap items-end gap-2">
      <input bind:value={newName} placeholder={t("rooms.newName")}
        onkeydown={(e) => e.key === "Enter" && addArea()}
        />
      <Button tone="primary" onclick={addArea}>{t("rooms.add")}</Button>
      <Button onclick={autoAssign} disabled={busy}>{t("rooms.autoAssign")}</Button>
      {#if msg}<span class="text-s text-dida-danger">{msg}</span>{/if}
    </div>

    <table class="w-full text-m">
      <thead class="text-left text-s text-dida-text-muted">
        <tr>
          <th class="py-1 font-medium">{t("rooms.name")}</th>
          <th class="py-1 font-medium">{t("rooms.kind")}</th>
          <th class="py-1 font-medium text-right">{t("rooms.members")}</th>
          <th class="py-1"></th>
        </tr>
      </thead>
      <tbody>
        {#each sortedAreas as a (a.id)}
          <tr class="border-t border-dida-border/60">
            <td class="py-1.5 pr-2">
              <input value={a.name ?? kindLabel(a)}
                placeholder={t("rooms.namePlaceholder")}
                onchange={(e) => rename(a, e.currentTarget.value)}
                class="w-full max-w-56" />
            </td>
            <td class="py-1.5 pr-2">
              <select value={a.kind ?? ""} onchange={(e) => setKind(a, e.currentTarget.value)}
               >
                <option value="">{t("rooms.noKind")}</option>
                {#each kindOptions as k (k)}
                  <option value={k}>{t(`room.kind.${k}` as MessageKey)}</option>
                {/each}
              </select>
            </td>
            <td class="py-1.5 text-right font-mono tabular-nums text-dida-text-muted">{memberCount(a.id)}</td>
            <td class="py-1.5 text-right">
              <Button tone="danger" size="small" onclick={() => remove(a)}>{t("common.delete")}</Button>
            </td>
          </tr>
        {:else}
          <tr><td class="py-2 text-s text-dida-text-faint" colspan="4">{t("rooms.empty")}</td></tr>
        {/each}
      </tbody>
    </table>
  </Card>
{/if}
