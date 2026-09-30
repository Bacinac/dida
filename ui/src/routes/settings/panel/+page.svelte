<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Picks, SaveButton } from "$lib/kit";
  import { api, type PanelConfig } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t, type MessageKey } from "$lib/i18n";
  import { SECTION_TITLE_CLASS } from "$lib/ui";
  import { auth } from "$lib/auth.svelte";

  // The wall panel is headless — no one edits it in front of the Hub. Its display
  // behaviour is configured here and read by /panel at boot (and on self-reload).
  let cfg = $state<PanelConfig | null>(null);
  let opusConfigured = $state(true);
  // Presence entities (occupancy/motion/person_count) that can gate the display.
  let presenceOpts = $state<{ id: string; label: string; areaId: number | null }[]>([]);
  let snapshot = $state("");
  let saving = $state(false);
  let saveMsg = $state<string | null>(null);

  const dirty = $derived(!!cfg && snapshot !== "" && JSON.stringify(cfg) !== snapshot);

  async function load() {
    cfg = await api.panelConfig();
    snapshot = JSON.stringify(cfg);
    try {
      const s = await api.getSettings();
      opusConfigured = s.opus_configured;
    } catch { opusConfigured = false; }
    // Presence-gating source: any entity that reports occupancy/motion/people.
    try {
      const PRESENCE = ["occupancy", "motion", "person_count"];
      const [ents, areas] = await Promise.all([api.listEntities(), api.listAreas()]);
      const areaName = new Map(areas.map((a) => [a.id, a.name]));
      presenceOpts = ents
        .filter((e) => e.exposed && !e.diagnostic && e.capabilities.some((c) => PRESENCE.includes(c)))
        .map((e) => {
          const nm = (e.label || e.name || e.entity_id).trim();
          const area = e.area_id != null ? areaName.get(e.area_id) : null;
          return { id: e.entity_id, label: area ? `${area} · ${nm}` : nm, areaId: e.area_id };
        })
        .sort((a, b) => a.label.localeCompare(b.label, "hr", { sensitivity: "base" }));
    } catch { presenceOpts = []; }
  }

  // Scope the presence picker to the panel's OWN room (its cast device's area,
  // room_area_id from the server) — a much shorter, relevant list. Fall back to
  // every sensor when the room is unknown or has none, so it's never empty.
  const presenceInRoom = $derived.by(() => {
    const room = cfg?.room_area_id;
    if (room == null) return presenceOpts;
    const scoped = presenceOpts.filter((o) => o.areaId === room);
    return scoped.length ? scoped : presenceOpts;
  });
  const presenceScoped = $derived(
    cfg?.room_area_id != null && presenceOpts.some((o) => o.areaId === cfg!.room_area_id),
  );

  async function save() {
    if (!cfg) return;
    saving = true; saveMsg = null;
    try {
      const { room_area_id, ...body } = cfg; // room_area_id is a read-only hint
      await api.updatePanelConfig(body);
      snapshot = JSON.stringify(cfg);
      saveMsg = t("settings.saved");
    } catch (e) {
      saveMsg = errMsg(e);
    } finally {
      saving = false;
    }
  }
  onMount(load);

  const TRANSITIONS: { value: PanelConfig["transition"]; key: MessageKey }[] = [
    { value: "kenburns", key: "panel.transitionKenburns" },
    { value: "fade", key: "panel.transitionFade" },
    { value: "none", key: "panel.transitionNone" },
  ];
  const FITS: { value: PanelConfig["photo_fit"]; key: MessageKey }[] = [
    { value: "blur", key: "panel.fitBlur" },
    { value: "contain", key: "panel.fitContain" },
    { value: "cover", key: "panel.fitCover" },
  ];
  // A minute-friendly label for the idle field (stored in seconds).
  const idleMin = $derived(cfg ? Math.round(cfg.idle_s / 30) / 2 : 0);
</script>

<svelte:head><title>{t("nav.panel")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else if cfg}
  <div class="max-w-xl space-y-6 rounded-lg border border-dida-border bg-dida-panel p-4">
    <p class="text-s text-dida-text-faint">{t("settings.panelIntro")}</p>

    <!-- Idle → screensaver -->
    <div>
      <div class="mb-1 flex items-baseline justify-between">
        <span class="{SECTION_TITLE_CLASS}">{t("panel.idle")}</span>
        <span class="text-m font-semibold tabular-nums text-dida-text">{idleMin} min</span>
      </div>
      <input type="range" min="30" max="600" step="30" bind:value={cfg.idle_s} class="w-full" />
      <p class="mt-1 text-s text-dida-text-faint">{t("panel.idleHint")}</p>
    </div>

    <!-- Slideshow -->
    <div>
      <p class="{SECTION_TITLE_CLASS} mb-2">{t("panel.slideshow")}</p>

      <div class="mb-3">
        <div class="mb-1 flex items-baseline justify-between">
          <span class="text-m text-dida-text-muted">{t("panel.photoInterval")}</span>
          <span class="text-m font-semibold tabular-nums text-dida-text">{cfg.photo_interval_s} s</span>
        </div>
        <input type="range" min="5" max="120" step="5" bind:value={cfg.photo_interval_s} class="w-full" />
      </div>

      <div class="mb-3">
        <span class="mb-1 block text-m text-dida-text-muted">{t("panel.transition")}</span>
        <div>
          <Picks
            picks={TRANSITIONS.map((tr) => ({ key: tr.value, label: t(tr.key) }))}
            chosen={[cfg.transition]}
            onpick={(k) => cfg && (cfg.transition = k as PanelConfig["transition"])}
          />
        </div>
      </div>

      <div class="mb-3">
        <span class="mb-1 block text-m text-dida-text-muted">{t("panel.fit")}</span>
        <div>
          <Picks
            picks={FITS.map((f) => ({ key: f.value, label: t(f.key) }))}
            chosen={[cfg.photo_fit]}
            onpick={(k) => cfg && (cfg.photo_fit = k as PanelConfig["photo_fit"])}
          />
        </div>
        <p class="mt-1 text-s text-dida-text-faint">{t("panel.fitHint")}</p>
      </div>

      {#if !opusConfigured}
        <p class="text-s text-dida-text-faint">{t("panel.photosNoOpus")}</p>
      {/if}
    </div>

    <!-- Presence gating: sleep the display when a chosen room sensor is empty -->
    <div>
      <span class="{SECTION_TITLE_CLASS} block">{t("panel.presence")}</span>
      <p class="mt-0.5 mb-2 text-s text-dida-text-faint">{t("panel.presenceHint")}</p>
      <select bind:value={cfg.presence_entity} class="w-full">
        <option value="">{t("panel.presenceAlways")}</option>
        {#each presenceInRoom as o (o.id)}
          <option value={o.id}>{o.label}</option>
        {/each}
      </select>
      {#if presenceScoped}
        <p class="mt-1 text-s text-dida-text-faint">{t("panel.presenceScoped")}</p>
      {/if}
    </div>

    <div class="flex items-center gap-3">
      <SaveButton size="small" {dirty} {saving} onclick={save} />
      {#if saveMsg}<span class="text-s text-dida-text-muted">{saveMsg}</span>{/if}
    </div>
  </div>
{/if}
