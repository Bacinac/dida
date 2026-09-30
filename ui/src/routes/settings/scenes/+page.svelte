<script lang="ts">
  // Settings → Scenes. Capture the current state of CHOSEN devices as a named
  // scene, inspect and edit what a scene holds, recall it, delete it.
  //
  // Capture used to take everything in current_state, which is how the one scene
  // in production ended up with 226 entries — device config, every helper, a door
  // lock — behind a one-tap Recall nobody could inspect first. Picking devices is
  // now the normal path, and a saved scene can be opened and edited row by row.
  import { onMount } from "svelte";
  import { api, type Scene, type SceneState } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { capLabel, valueKind } from "$lib/capabilities";
  import { Button, Card, Notice, SaveButton, Toggle, dialog, toasts } from "$lib/kit";
  import { t } from "$lib/i18n";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";

  // A row carries the value as a STRING while being edited (one input type per
  // kind); it is coerced back on save, so a half-typed "-" never reaches the API.
  type Row = { entity_id: string; capability: string; value: string };

  let scenes = $state<Scene[]>([]);
  let name = $state("");
  let pickedIds = $state<string[]>([]);
  let pickOne = $state("");           // EntityPicker is single-select; each pick appends
  let saving = $state(false);
  let recallingId = $state<number | null>(null);
  let err = $state<string | null>(null);

  // the open editor
  let editId = $state<number | null>(null);
  let editName = $state("");
  let rows = $state<Row[]>([]);
  let addOne = $state("");
  let loadingEdit = $state(false);
  let savingEdit = $state(false);
  const editForm = () => JSON.stringify({ editName, rows });
  let editSaved = $state("");
  const editDirty = $derived(editForm() !== editSaved);

  const pickerItems = $derived(Object.values(devices.byId).map(pickerItem));

  const entLabel = (id: string) => devices.byId[id]?.name ?? id;
  const entArea = (id: string) =>
    devices.byId[id] ? devices.areaName(devices.byId[id].areaId) : "";

  /** The device's live value for one capability, as an edit-box string. */
  function liveValue(entityId: string, capability: string): string | null {
    const v = devices.byId[entityId]?.caps?.[capability]?.value;
    return v === undefined || v === null ? null : String(v);
  }

  async function load() {
    try {
      scenes = await api.scenes();
    } catch (e) {
      err = errMsg(e);
    }
  }
  onMount(load);

  function addPicked(id: string) {
    if (id && !pickedIds.includes(id)) pickedIds.push(id);
    pickOne = "";
  }

  async function save() {
    const n = name.trim();
    if (!n || saving) return;
    // Capturing the whole house is still possible but never accidental: it is what
    // happens with nothing picked, and it now says so first.
    if (pickedIds.length === 0) {
      const ok = await dialog.confirm({
        title: t("scenes.wholeHouseTitle"),
        message: t("scenes.wholeHouseMsg"),
        confirmLabel: t("scenes.save"),
      });
      if (!ok) return;
    }
    err = null;
    saving = true;
    try {
      await api.createScene(n, pickedIds);
      name = "";
      pickedIds = [];
      await load();
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = false;
    }
  }

  async function openEdit(s: Scene) {
    if (editId === s.id) {
      editId = null;
      return;
    }
    err = null;
    loadingEdit = true;
    editId = s.id;
    try {
      const detail = await api.scene(s.id);
      editName = detail.name;
      rows = detail.states.map((st) => ({
        entity_id: st.entity_id,
        capability: st.capability,
        value: String(st.value),
      }));
      editSaved = editForm();
    } catch (e) {
      err = errMsg(e);
      editId = null;
    } finally {
      loadingEdit = false;
    }
  }

  /** Append every capability the picked device currently reports a value for. */
  function addDevice(id: string) {
    addOne = "";
    const d = devices.byId[id];
    if (!d) return;
    for (const cap of Object.keys(d.caps ?? {})) {
      if (rows.some((r) => r.entity_id === id && r.capability === cap)) continue;
      const v = liveValue(id, cap);
      if (v === null) continue;
      rows.push({ entity_id: id, capability: cap, value: v });
    }
  }

  function refreshRow(r: Row) {
    const v = liveValue(r.entity_id, r.capability);
    if (v === null) {
      toasts.error(t("scenes.noLiveValue"));
      return;
    }
    r.value = v;
  }

  function refreshAll() {
    for (const r of rows) {
      const v = liveValue(r.entity_id, r.capability);
      if (v !== null) r.value = v;
    }
  }

  /** Row strings → the API's typed values. */
  function toStates(): SceneState[] {
    return rows.map((r) => {
      const kind = valueKind(r.capability);
      if (kind === "bool") return { ...r, value: r.value === "true" };
      if (kind === "number") return { ...r, value: Number(r.value) };
      return { ...r, value: r.value };
    });
  }

  async function saveEdit() {
    if (editId === null || savingEdit) return;
    err = null;
    savingEdit = true;
    try {
      await api.updateScene(editId, editName.trim(), toStates());
      editId = null;
      await load();
      toasts.success(t("scenes.saved"));
    } catch (e) {
      err = errMsg(e);
    } finally {
      savingEdit = false;
    }
  }

  async function recall(s: Scene) {
    err = null;
    recallingId = s.id;
    try {
      const r = await api.recallScene(s.id);
      toasts.success(t("scenes.recalled", { applied: r.applied, skipped: r.skipped }));
    } catch (e) {
      err = errMsg(e);
    } finally {
      recallingId = null;
    }
  }

  async function remove(s: Scene) {
    const ok = await dialog.confirm({
      title: t("scenes.confirmDelTitle"),
      message: t("scenes.confirmDelMsg", { name: s.name }),
      confirmLabel: t("scenes.delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteScene(s.id);
      if (editId === s.id) editId = null;
      await load();
    } catch (e) {
      err = errMsg(e);
    }
  }
</script>

{#if err}
  <div class="mb-4"><Notice tone="err">{t("scenes.err")}: {err}</Notice></div>
{/if}

<div class="grid max-w-3xl gap-4">
  <Card>
    <div class="grid gap-3">
      <div class="flex flex-wrap items-center gap-2">
        <input
          class="w-full max-w-xs flex-1"
          placeholder={t("scenes.newName")}
          bind:value={name}
          onkeydown={(e) => e.key === "Enter" && save()}
          maxlength="80"
        />
        <Button tone="primary" onclick={save} disabled={!name.trim() || saving}>
          {saving ? t("scenes.saving") : t("scenes.save")}
        </Button>
      </div>

      <div class="grid gap-2">
        <EntityPicker
          bind:value={pickOne}
          items={pickerItems}
          placeholder={t("scenes.addDevice")}
          onchange={addPicked}
        />
        {#if pickedIds.length}
          <div class="flex flex-wrap gap-1">
            {#each pickedIds as id (id)}
              <span class="flex items-center gap-1 rounded bg-dida-panel-2 px-2 py-0.5 text-s">
                {entLabel(id)}
                <Button size="small" label={t("scenes.removeDevice")} title={t("scenes.removeDevice")} onclick={() => (pickedIds = pickedIds.filter((x) => x !== id))}>✕</Button>
              </span>
            {/each}
          </div>
        {:else}
          <p class="text-s text-dida-text-faint">{t("scenes.pickHint")}</p>
        {/if}
      </div>
    </div>
  </Card>

  {#if scenes.length === 0}
    <p class="text-m text-dida-text-faint">{t("scenes.empty")}</p>
  {:else}
    <div class="grid gap-2">
      {#each scenes as s (s.id)}
        <Card>
          <div class="flex flex-wrap items-center justify-between gap-3">
            <button class="min-w-0 text-left" onclick={() => openEdit(s)}>
              <div class="truncate font-semibold">
                {editId === s.id ? "▾" : "▸"}
                {s.name}
              </div>
              <div class="text-s text-dida-text-faint">{t("scenes.count", { n: s.count })}</div>
            </button>
            <div class="flex items-center gap-2">
              <Button tone="primary" size="small" onclick={() => recall(s)} disabled={recallingId === s.id}>
                {recallingId === s.id ? t("scenes.recalling") : t("scenes.recall")}
              </Button>
              <Button tone="danger" size="small" onclick={() => remove(s)}>
                {t("scenes.delete")}
              </Button>
            </div>
          </div>

          {#if editId === s.id}
            <div class="mt-3 grid gap-3 border-t border-dida-border pt-3">
              {#if loadingEdit}
                <p class="text-m text-dida-text-faint">{t("scenes.loading")}</p>
              {:else}
                <input
                  class="w-full max-w-xs"
                  bind:value={editName}
                  maxlength="80"
                  aria-label={t("scenes.newName")}
                />

                <div class="grid gap-1">
                  {#each rows as r (r.entity_id + r.capability)}
                    <div class="flex flex-wrap items-center gap-2 rounded bg-dida-panel-2 px-2 py-1 text-m">
                      <div class="min-w-0 flex-1">
                        <div class="truncate">{entLabel(r.entity_id)}</div>
                        <div class="truncate text-s text-dida-text-faint">
                          {entArea(r.entity_id)} · {capLabel(r.capability)}
                        </div>
                      </div>

                      {#if valueKind(r.capability) === "bool"}
                        <Toggle
                          size="small"
                          checked={r.value === "true"}
                          label={capLabel(r.capability)}
                          onclick={() => (r.value = r.value === "true" ? "false" : "true")}
                        />
                      {:else if valueKind(r.capability) === "number"}
                        <input class="w-24" type="number" bind:value={r.value} />
                      {:else}
                        <input class="w-40" bind:value={r.value} />
                      {/if}

                      <Button
                        size="small" title={t("scenes.fromNow")} label={t("scenes.fromNow")}
                        onclick={() => refreshRow(r)}
                      >↻</Button>
                      <Button
                        size="small" title={t("scenes.removeRow")} label={t("scenes.removeRow")}
                        onclick={() => (rows = rows.filter((x) => x !== r))}
                      >✕</Button>
                    </div>
                  {/each}
                  {#if rows.length === 0}
                    <p class="text-s text-dida-text-faint">{t("scenes.noRows")}</p>
                  {/if}
                </div>

                <EntityPicker
                  bind:value={addOne}
                  items={pickerItems}
                  placeholder={t("scenes.addDevice")}
                  onchange={addDevice}
                />

                <div class="flex flex-wrap items-center gap-2">
                  <SaveButton size="small" dirty={editDirty} saving={savingEdit} blocked={!editName.trim()} onclick={saveEdit} label={t("scenes.saveChanges")} />
                  <Button size="small" onclick={refreshAll}>
                    {t("scenes.refreshAll")}
                  </Button>
                  <Button size="small" onclick={() => (editId = null)}>
                    {t("scenes.cancel")}
                  </Button>
                </div>
              {/if}
            </div>
          {/if}
        </Card>
      {/each}
    </div>
  {/if}
</div>
