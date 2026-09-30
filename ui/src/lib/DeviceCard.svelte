<script lang="ts">
  // Device-centric card: ONE physical device (all entities sharing a device_key,
  // or the derived adapter:node group). Its facets are split by role into clear
  // sections — HERO controls (actuators / climate / media), a compact READINGS
  // strip (live sensors), and a collapsed DIAGNOSTICS block (technical fields the
  // user opted back in). One shared shell; the hero adapts to the device type.
  import CapabilityControl from "$lib/CapabilityControl.svelte";
  import MediaPlayer from "$lib/MediaPlayer.svelte";
  import ClimateCard from "$lib/ClimateCard.svelte";
  import Remote from "$lib/Remote.svelte";
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import Sparkline from "$lib/Sparkline.svelte";
  import { capRank, capLabel, capMeta, isActuator, deviceType, formatValue, OPTION_CAPS, type DeviceType } from "$lib/capabilities";
  import { isMediaPlayer, MEDIA_CAPS } from "$lib/media";
  import { isRemote, remoteButtonIds } from "$lib/remote";
  import { isClimate, CLIMATE_CAPS } from "$lib/climate";
  import { t } from "$lib/i18n";
  import { Button, Tag } from "$lib/kit";
  import { clock } from "$lib/dt";
  import { devices, type CapState, type Device } from "$lib/store.svelte";
  import { areaExempt } from "$lib/devices";
  import { auth } from "$lib/auth.svelte";
  import type { Area } from "$lib/api";

  let { members, name, deviceKey, areas = [], onassign, ondetail, oncollapse, spark, type: unitType }: {
    members: Device[];
    name: string;
    deviceKey: string;   // group key (device_key, or the entity_id for ungrouped) — the rename target
    areas?: Area[];
    onassign?: (entityId: string, areaId: number | null) => void;
    ondetail?: (entityId: string) => void;   // open the per-device detail panel
    oncollapse?: () => void; // the card was opened from a folded row; its heading folds it back
    spark?: number[];    // shape of this device's main reading over the last day
    type?: DeviceType;   // resolved device type (respects the user's type override); falls back to caps-derived
  } = $props();

  // The option blobs (OPTION_CAPS) plus what the media/AVR hero renders itself and
  // the identity_presence footnote, which is never its own field.
  const META_CAPS = new Set([...OPTION_CAPS, "source", "channel", "identity_since"]);

  // A composed hero exists for climate/media devices — those caps render as one
  // block, so they're pulled out of the generic field list (only for that member).
  const climateMember = $derived(members.find(isClimate) ?? null);
  const mediaMember = $derived(members.find(isMediaPlayer) ?? null);
  // A universal-remote device: its own hero (source picker + D-pad). The head's
  // source is already hidden (META_CAPS); its press-button siblings are pulled
  // into the Remote widget, so they're excluded from the generic field list.
  const remoteMode = $derived(mediaMember ? false : isRemote(members));
  const remoteBtnIds = $derived(remoteMode ? remoteButtonIds(members) : new Set<string>());

  interface Field { entity: Device; cap: string; cs: CapState; }
  const fields = $derived.by<Field[]>(() => {
    const out: Field[] = [];
    for (const m of members) {
      if (remoteBtnIds.has(m.entityId)) continue; // a remote key → shown in the Remote hero
      for (const cap of Object.keys(m.caps)) {
        if (META_CAPS.has(cap)) continue;
        if (m.hiddenCaps.includes(cap)) continue; // user hid this capability
        if (m === climateMember && CLIMATE_CAPS.has(cap)) continue;
        if (m === mediaMember && MEDIA_CAPS.has(cap)) continue;
        out.push({ entity: m, cap, cs: m.caps[cap] });
      }
    }
    return out;
  });

  const byRank = (a: Field, b: Field) => capRank(a.cap) - capRank(b.cap);
  // Each facet's display zone. The adapter tags a field "control" (primary,
  // shown), "config" (a setting) or "diagnostic" (technical); the legacy
  // `diagnostic` flag still folds an untagged technical entity into diagnostics.
  function zoneOf(f: Field): "control" | "config" | "diagnostic" {
    const c = f.entity.category;
    if (c === "config") return "config";
    if (c === "diagnostic" || f.entity.diagnostic) return "diagnostic";
    return "control";
  }
  // Control-zone facets split by role: actuators → hero, numeric sensors →
  // readings chips, other read-only fields → their own rows. Config and
  // diagnostic zones each get a collapsed section of their own.
  const controlFields = $derived(fields.filter((f) => zoneOf(f) === "control"));
  const controls = $derived(controlFields.filter((f) => isActuator(f.cap)).sort(byRank));
  const metrics = $derived(
    controlFields.filter((f) => !isActuator(f.cap) && capMeta(f.cap).control === "sensor").sort(byRank),
  );
  const otherReadings = $derived(
    controlFields.filter((f) => !isActuator(f.cap) && capMeta(f.cap).control !== "sensor").sort(byRank),
  );
  const settings = $derived(fields.filter((f) => zoneOf(f) === "config").sort(byRank));
  const diagnostics = $derived(fields.filter((f) => zoneOf(f) === "diagnostic").sort(byRank));

  const allCaps = $derived(Object.assign({}, ...members.map((m) => m.caps)) as Record<string, CapState>);
  const type = $derived(unitType ?? deviceType(allCaps));
  const adapters = $derived([...new Set(members.map((m) => m.adapter))]);
  // Non-spatial devices (presence/calendar/notify/astro) get no room picker.
  const roomExempt = $derived(areaExempt({ type, adapters, site: devices.deviceSite(deviceKey) }));
  const areaId = $derived(members.find((m) => m.areaId != null)?.areaId ?? null);
  const lastSeen = $derived(Math.max(0, ...members.map((m) => m.lastSeen)));

  // Reachability is device-level, so every member carries the same verdict — read
  // the first. `unreachable` is what the card dims and badges: the adapter has said
  // the hardware is not there, which is a different fact from a stale reading and
  // the one that stops a person chasing a light that is simply unplugged.
  const unreachable = $derived(members.length > 0 && members.some((m) => !m.reachable));
  const downSince = $derived(Math.max(0, ...members.map((m) => (m.reachable ? 0 : m.reachableSince))));
  const downLabel = $derived(
    downSince ? clock(downSince) : "",
  );
  const heldSince = $derived(Math.max(0, ...members.map((m) => m.manualSince)));

  // When a card groups several entities that each carry their OWN name — a heating
  // controller's per-room facets, an ESPHome node's many sensors — the capability
  // label alone repeats ("Target temperature" eight times, one per room, with
  // nothing to say which). Prefix the row with the entity's name so the rooms are
  // distinguishable. The card is already titled `name`, so strip a redundant
  // leading copy: "Heating living" under "Heating" reads as "living". Returns null
  // for an entity with no name of its own (a one-relay Shelly) or whose name IS the
  // card title (the system entity), where the plain capability label is right.
  function facetPrefix(entity: Device): string | null {
    if (!entity.hasOwnName) return null;
    const n = entity.name.trim();
    if (!n || n === name) return null;
    const pre = name + " ";
    return n.toLowerCase().startsWith(pre.toLowerCase()) ? n.slice(pre.length) : n;
  }
  function facetLabel(entity: Device, cap: string): string {
    const p = facetPrefix(entity);
    return p ? `${p} · ${capLabel(cap)}` : capLabel(cap);
  }

  // Selectable values / device range for an actuator field, from the *_options
  // metadata published on that same entity.
  function jsonList(entity: Device, cap: string): string[] {
    const v = entity.caps[cap]?.value;
    if (typeof v !== "string") return [];
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a.map(String) : [];
    } catch {
      return [];
    }
  }
  function optionsFor(f: Field): string[] {
    if (f.cap === "enum") return jsonList(f.entity, "enum_options");
    if (f.cap === "vacuum_mode") return jsonList(f.entity, "vacuum_mode_options");
    if (f.cap === "effect") return jsonList(f.entity, "effect_options");
    return [];
  }
  function rangeFor(f: Field): { min?: number; max?: number; step?: number } | undefined {
    if (f.cap !== "number") return undefined;
    const v = f.entity.caps["number_options"]?.value;
    if (typeof v !== "string") return undefined;
    try { const o = JSON.parse(v); return { min: o.min, max: o.max, step: o.step }; } catch { return undefined; }
  }
  function unitOf(f: Field): string {
    return f.cs.unit ?? capMeta(f.cap).unit ?? "";
  }

  function ago(ms: number): string {
    if (!ms) return t("ago.never");
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return t("ago.s", { n: s });
    const m = Math.round(s / 60);
    if (m < 60) return t("ago.min", { n: m });
    const h = Math.round(m / 60);
    if (h < 24) return t("ago.h", { n: h });
    return t("ago.d", { n: Math.round(h / 24) });
  }

  let showDiag = $state(false);
  let showSettings = $state(false);
  const assignAll = (a: number | null) => { for (const m of members) onassign?.(m.entityId, a); };

  // Inline device rename (sets the user label; blank clears → auto name). Works
  // for any device, incl. ungrouped cast/dlna where the entity_id IS the key.
  let renaming = $state(false);
  let renameVal = $state("");
  function focusEl(node: HTMLInputElement) { node.focus(); node.select(); }
  function startRename() { renameVal = name; renaming = true; }
  async function commitRename() {
    if (!renaming) return;      // guard: Enter then blur must not fire twice
    renaming = false;
    const v = renameVal.trim();
    if (v !== name) await devices.renameDevice(deviceKey, v || null);
  }
</script>

<div
  class="flex flex-col rounded-lg border bg-dida-panel p-3 transition-opacity {unreachable
    ? 'border-dida-warn/40'
    : 'border-dida-border'}">
  <div class="mb-2 flex items-start justify-between gap-2">
    <div class="flex min-w-0 items-center gap-2">
      {#if oncollapse && !renaming}
        <button type="button" aria-expanded="true" title={t("common.collapse")} onclick={oncollapse}
          class="flex min-w-0 items-center gap-2 text-left">
          <span class="rotate-90 text-xl leading-none text-dida-text-muted">›</span>
          <DeviceIcon {type} class="size-4 shrink-0 text-dida-text-muted" />
          <h3 class="line-clamp-2 min-w-0 break-words font-semibold leading-tight">{name}</h3>
        </button>
      {:else}
        <DeviceIcon {type} class="size-4 shrink-0 text-dida-text-muted" />
      {/if}
      {#if renaming}
        <input
          value={renameVal}
          oninput={(e) => (renameVal = e.currentTarget.value)}
          onkeydown={(e) => { if (e.key === "Enter") commitRename(); if (e.key === "Escape") renaming = false; }}
          onblur={commitRename}
          use:focusEl
          aria-label={t("common.rename")}
          class="min-w-0 flex-1 font-semibold"
        />
      {:else}
        {#if !oncollapse}
          <h3 class="line-clamp-2 min-w-0 break-words font-semibold leading-tight" title={name}>{name}</h3>
        {/if}
        {#if auth.isAdmin}
          <Button size="small" label={t("common.rename")} title={t("common.rename")} onclick={startRename}>✎</Button>
        {/if}
        {#if spark && spark.length > 1}
          <Sparkline values={spark} />
        {/if}
        {#if ondetail}
          <Button size="small" label={t("detail.title")} title={t("detail.title")} onclick={() => ondetail?.(members[0].entityId)}>ⓘ</Button>
        {/if}
      {/if}
    </div>
    <div class="flex shrink-0 items-center gap-1">
      {#if heldSince}
        <Tag tone="busy" title={t("card.manualSince", { t: clock(heldSince) })}>{t("card.manual")}</Tag>
      {/if}
      {#if unreachable}
        <Tag tone="warn" title={downLabel ? t("card.unreachableSince", { t: downLabel }) : t("card.unreachable")}>
          {t("card.unreachable")}{downLabel ? ` · ${downLabel}` : ""}
        </Tag>
      {/if}
    </div>
  </div>

  <!-- The whole value/control body dims when the device is unreachable: every
       reading below is last-known, not live. The header badge and the footer's
       "seen" age stay full-opacity so the signal and its timestamp read clearly. -->
  <div class={unreachable ? "opacity-45 transition-opacity" : "contents"}>
  <!-- HERO: primary controls -->
  {#if climateMember}<ClimateCard device={climateMember} />{/if}
  {#if mediaMember}<div class="mb-2"><MediaPlayer device={mediaMember} compact /></div>{/if}
  {#if remoteMode}<div class="mb-2"><Remote {members} /></div>{/if}
  {#if controls.length}
    <div class="divide-y divide-dida-border/60">
      {#each controls as f (f.entity.entityId + f.cap)}
        <CapabilityControl
          entityId={f.entity.entityId}
          capability={f.cap}
          cs={f.cs}
          name={facetPrefix(f.entity) ? facetLabel(f.entity, f.cap) : undefined}
          options={optionsFor(f)}
          range={rangeFor(f)}
          sinceIso={f.cap === "identity_presence"
            ? String(f.entity.caps["identity_since"]?.value ?? "")
            : undefined}
        />
      {/each}
    </div>
  {/if}

  <!-- READINGS: live sensor values as a compact chip strip -->
  {#if metrics.length}
    <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 border-t border-dida-border/60 pt-2">
      {#each metrics as f (f.entity.entityId + f.cap)}
        <span class="flex items-baseline gap-1">
          <span class="text-xs text-dida-text-muted">{facetLabel(f.entity, f.cap)}</span>
          <span class="font-mono text-m tabular-nums">
            {typeof f.cs.value === "number" ? formatValue(f.cap, f.cs.value) : f.cs.value}<span class="text-dida-text-faint">{unitOf(f)}</span>
          </span>
        </span>
      {/each}
    </div>
  {/if}

  {#if otherReadings.length}
    <div class="mt-1 divide-y divide-dida-border/60">
      {#each otherReadings as f (f.entity.entityId + f.cap)}
        <CapabilityControl entityId={f.entity.entityId} capability={f.cap} cs={f.cs}
          name={facetPrefix(f.entity) ? facetLabel(f.entity, f.cap) : undefined} />
      {/each}
    </div>
  {/if}

  {#if !controls.length && !metrics.length && !otherReadings.length && !settings.length && !diagnostics.length && !climateMember && !mediaMember && !remoteMode}
    <p class="py-2 text-s text-dida-text-faint">{t("card.noReadings")}</p>
  {/if}

  <!-- SETTINGS: writable configuration knobs, collapsed -->
  {#if settings.length}
    <button
      onclick={() => (showSettings = !showSettings)}
      class="mt-2 flex items-center gap-1 border-t border-dida-border/60 pt-2 text-left text-s font-medium text-dida-text-muted hover:text-dida-text"
    >
      <span class="inline-block w-3">{showSettings ? "▾" : "▸"}</span>
      {t("devices.settings")} <span class="text-dida-text-faint">· {settings.length}</span>
    </button>
    {#if showSettings}
      <div class="divide-y divide-dida-border/60">
        {#each settings as f (f.entity.entityId + f.cap)}
          <CapabilityControl
            entityId={f.entity.entityId}
            capability={f.cap}
            cs={f.cs}
            name={facetPrefix(f.entity) ? facetLabel(f.entity, f.cap) : undefined}
            options={optionsFor(f)}
            range={rangeFor(f)}
          />
        {/each}
      </div>
    {/if}
  {/if}

  <!-- DIAGNOSTICS: technical fields, collapsed -->
  {#if diagnostics.length}
    <button
      onclick={() => (showDiag = !showDiag)}
      class="mt-2 flex items-center gap-1 border-t border-dida-border/60 pt-2 text-left text-s font-medium text-dida-text-muted hover:text-dida-text"
    >
      <span class="inline-block w-3">{showDiag ? "▾" : "▸"}</span>
      {t("devices.diagnostics")} <span class="text-dida-text-faint">· {diagnostics.length}</span>
    </button>
    {#if showDiag}
      <div class="divide-y divide-dida-border/60">
        {#each diagnostics as f (f.entity.entityId + f.cap)}
          <CapabilityControl entityId={f.entity.entityId} capability={f.cap} cs={f.cs}
            name={facetPrefix(f.entity) ? facetLabel(f.entity, f.cap) : undefined} />
        {/each}
      </div>
    {/if}
  {/if}

  </div>

  <div class="mt-2 flex items-center justify-between gap-2">
    {#if auth.isAdmin && !roomExempt}
      <select
        value={areaId ?? ""}
        onchange={(e) => assignAll(e.currentTarget.value ? Number(e.currentTarget.value) : null)}
        class="text-dida-text-muted"
        aria-label={t("card.room")}
      >
        <option value="">{t("card.room")}</option>
        {#each areas as a (a.id)}<option value={a.id}>{devices.roomLabel(a)}</option>{/each}
      </select>
    {/if}
    <span class="ml-auto text-right text-xs text-dida-text-faint">{adapters.join(", ")} · {t("card.seen", { ago: ago(lastSeen) })}</span>
  </div>
</div>
