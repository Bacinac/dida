<script lang="ts">
  // Admin editor for the GLOBAL volume presets (Quiet/Normal/Loud …) shown on every
  // zone's volume row. Rendered at the top of the media source editor. Edits a local
  // draft; saves on demand (the server sorts ascending, so buttons read low→high).
  import { errMsg } from "$lib/errors";
  import { Button, SaveButton } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { volume, volumeColor } from "$lib/volume.svelte";
  import type { VolumePreset } from "$lib/api";

  const clone = (ps: VolumePreset[]): VolumePreset[] => ps.map((p) => ({ label: p.label, value: p.value }));

  let draft = $state<VolumePreset[]>([]);
  let seeded = false;
  $effect(() => {
    void volume.load();
    if (!seeded && volume.presets.length) {
      draft = clone(volume.presets);
      seeded = true;
    }
  });

  const dirty = $derived(JSON.stringify(draft) !== JSON.stringify(volume.presets));
  const valid = $derived(
    draft.length > 0 && draft.every((p) => p.label.trim().length > 0 && p.value >= 0 && p.value <= 100),
  );

  let saving = $state(false);
  let err = $state<string | null>(null);

  const add = () => (draft = [...draft, { label: "", value: 50 }]);
  const remove = (i: number) => (draft = draft.filter((_, j) => j !== i));

  async function save(): Promise<void> {
    saving = true;
    err = null;
    try {
      await volume.save(draft.map((p) => ({ label: p.label.trim(), value: Math.round(p.value) })));
      draft = clone(volume.presets); // reflect the server's sorted result → clears dirty
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = false;
    }
  }
</script>

<div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
  <div class="mb-1 flex items-center justify-between gap-2">
    <span class="text-m font-semibold">{t("media.volumePresets")}</span>
    <SaveButton size="small" {dirty} {saving} blocked={!valid} onclick={save} />
  </div>
  <p class="mb-2 text-s text-dida-text-faint">{t("media.volumePresetsHint")}</p>

  <div class="space-y-1.5">
    {#each draft as p, i (i)}
      <div class="flex items-center gap-2">
        <span class="size-3.5 shrink-0 rounded-full" style="background-color:{volumeColor(p.value)}" title="{p.value}%"></span>
        <input
          bind:value={p.label} placeholder={t("media.presetName")}
          class="min-w-0 flex-1"
        />
        <input
          type="number" min="0" max="100" bind:value={p.value}
          class="w-16 shrink-0 tabular-nums"
        />
        <span class="shrink-0 text-s text-dida-text-faint">%</span>
        <Button size="small" label={t("common.delete")} title={t("common.delete")} onclick={() => remove(i)}>✕</Button>
      </div>
    {/each}
  </div>

  {#if err}<p class="mt-2 text-s text-dida-danger">{err}</p>{/if}
  <div class="mt-2"><Button size="small" onclick={add}>+ {t("media.addPreset")}</Button></div>
</div>
