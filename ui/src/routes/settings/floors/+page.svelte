<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Card, dialog } from "$lib/kit";
  import { api, type Floor } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";

  let floors = $state<Floor[]>([]);
  let newName = $state("");
  let msg = $state<string | null>(null);
  let busy = $state(false);

  const sorted = $derived([...floors].sort((a, b) => a.sort_order - b.sort_order));

  async function load() {
    if (!auth.isAdmin) return;
    try { floors = await api.listFloors(); } catch { floors = []; }
  }

  async function addFloor() {
    const name = newName.trim();
    if (!name) return;
    msg = null;
    try { await api.createFloor(name); newName = ""; await load(); }
    catch (e) { msg = errMsg(e); }
  }

  async function rename(f: Floor, value: string) {
    const name = value.trim();
    if (!name || name === f.name) return;
    msg = null;
    try { await api.updateFloor(f.id, { name }); await load(); }
    catch (e) { msg = errMsg(e); }
  }

  // Swap sort_order with the neighbour in the move direction.
  async function move(f: Floor, dir: -1 | 1) {
    const i = sorted.findIndex((x) => x.id === f.id);
    const j = i + dir;
    if (j < 0 || j >= sorted.length) return;
    const a = sorted[i], b = sorted[j];
    msg = null;
    try {
      await api.updateFloor(a.id, { sort_order: b.sort_order });
      await api.updateFloor(b.id, { sort_order: a.sort_order });
      await load();
    } catch (e) { msg = errMsg(e); }
  }

  async function remove(f: Floor) {
    const ok = await dialog.confirm({
      title: t("common.delete"),
      message: t("floors.confirmDelete", { name: f.name }),
      confirmLabel: t("common.delete"), danger: true,
    });
    if (!ok) return;
    msg = null;
    try { await api.deleteFloor(f.id); await load(); }
    catch (e) { msg = errMsg(e); }
  }

  // Decode the picked file once for its natural pixel dims, then upload raw bytes.
  // The floor plan page vectorizes it into rooms; this is only the scaffold.
  async function pickImage(f: Floor, input: HTMLInputElement) {
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    busy = true; msg = null;
    try {
      const dim = await imageDims(file);
      await api.uploadFloorImage(f.id, file, dim.w, dim.h);
      await load();
    } catch (e) { msg = errMsg(e); }
    finally { busy = false; }
  }

  function imageDims(file: Blob): Promise<{ w: number; h: number }> {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve({ w: img.naturalWidth, h: img.naturalHeight }); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode")); };
      img.src = url;
    });
  }

  // Cache-bust the preview by the stored filename so a re-upload shows at once.
  const imgUrl = (f: Floor): string => `/api/floorplan/${f.key}?v=${encodeURIComponent(f.img_path ?? "")}`;

  onMount(load);
</script>

<svelte:head><title>{t("floors.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  <Card>

    <p class="mb-3 text-s text-dida-text-muted">{t("floors.hint")}</p>

    <div class="mb-4 flex flex-wrap items-end gap-2">
      <input bind:value={newName} placeholder={t("floors.newName")}
        onkeydown={(e) => e.key === "Enter" && addFloor()}
        />
      <Button tone="primary" onclick={addFloor}>{t("floors.add")}</Button>
      {#if busy}<span class="text-s text-dida-text-muted">{t("floors.uploading")}</span>{/if}
      {#if msg}<span class="text-s text-dida-danger">{msg}</span>{/if}
    </div>

    <div class="flex flex-col gap-2">
      {#each sorted as f, i (f.id)}
        <div class="flex items-center gap-3 rounded border border-dida-border bg-dida-panel-2 p-2">
          <div class="flex flex-col leading-none">
            <Button size="small" label={t("floors.moveUp")} title={t("floors.moveUp")} disabled={i === 0} onclick={() => move(f, -1)}>↑</Button>
            <Button size="small" label={t("floors.moveDown")} title={t("floors.moveDown")} disabled={i === sorted.length - 1} onclick={() => move(f, 1)}>↓</Button>
          </div>

          <div class="grid size-16 shrink-0 place-items-center overflow-hidden rounded border border-dida-border bg-dida-panel">
            {#if f.img_path}
              <img src={imgUrl(f)} alt={f.name} class="h-full w-full object-contain" />
            {:else}
              <span class="px-1 text-center text-2xs text-dida-text-faint">{t("floors.noImage")}</span>
            {/if}
          </div>

          <input value={f.name} onchange={(e) => rename(f, e.currentTarget.value)}
            class="w-40" />

          <label class="cursor-pointer">
            <input type="file" accept="image/png,image/jpeg,image/webp" class="hidden"
              onchange={(e) => pickImage(f, e.currentTarget)} />
            <span class="inline-block rounded border border-dida-border px-2 py-1 text-s text-dida-text-muted hover:border-dida-accent">
              {f.img_path ? t("floors.replace") : t("floors.upload")}
            </span>
          </label>

          <div class="ml-auto">
            <Button tone="danger" size="small" onclick={() => remove(f)}>{t("common.delete")}</Button>
          </div>
        </div>
      {:else}
        <p class="text-s text-dida-text-faint">{t("floors.empty")}</p>
      {/each}
    </div>
  </Card>
{/if}
