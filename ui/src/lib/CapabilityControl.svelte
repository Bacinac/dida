<script lang="ts">
  import { onDestroy } from "svelte";
  import { api } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { capBadge, capLabel, capMeta, formatDuration, formatValue, mowerErrorLabel, mowerStateLabel, optionLabel, type ParkedVehicle, parkedFor, parkedSince, parkedVehicle, sceneLit } from "$lib/capabilities";
  import { clock } from "$lib/clock.svelte";
  import { tr } from "$lib/translations.svelte";
  import { errMsg } from "$lib/errors";
  import { Button, Dialog, Tag, Toggle, formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import LiveVideo from "$lib/LiveVideo.svelte";
  import type { CapState } from "$lib/store.svelte";

  let { entityId, capability, cs, options = [], range, compact = false, name, sinceIso }: {
    entityId: string;
    capability: string;
    cs: CapState;
    options?: string[];   // enum: the selectable values (from the entity's enum_options)
    range?: { min?: number; max?: number; step?: number }; // device range (number_options)
    compact?: boolean;    // hide the label (caller already shows the field name)
    name?: string;        // label override — for a generic cap (number/enum/…) whose
                          // capability name says nothing, the caller passes the entity's.
    sinceIso?: string;    // identity_presence only: the sibling identity_since value,
                          // rendered as the same since-footnote a parked vehicle gets.
  } = $props();

  const meta = $derived(capMeta(capability));
  // Device-provided range wins over the static CAP_META one (a generic `number`
  // has no static range — it comes from the entity's number_options).
  const lo = $derived(range?.min ?? meta.min);
  const hi = $derived(range?.max ?? meta.max);
  const step = $derived(range?.step ?? meta.step);
  // A bounded number (min AND max known, e.g. a virtual helper's number_options range or
  // a device's) is naturally a slider; an unbounded number stays a typed box.
  const isSlider = $derived(meta.control === "slider" || (capability === "number" && lo != null && hi != null));
  const label = $derived(name ?? capLabel(capability));
  const unit = $derived(cs.unit ?? meta.unit ?? "");

  // `camera` value is a stream descriptor {stream, snapshot, mp4, hls, webrtc}.
  // We only parse it to know "this IS a camera" and get its stream name — the
  // pixels ride DIDA's OWN proxy (/api/camera/<entity_id>/…), NEVER the raw URLs
  // in the descriptor: those point at a private camera subnet (BABA go2rtc) or an
  // authenticated NVR (Frigate) the browser can't reach, so an <img> at them just
  // shows a black box. Same proxy the camera wall uses.
  const cam = $derived.by(() => {
    if (meta.control !== "camera" || typeof cs.value !== "string") return null;
    try {
      const d = JSON.parse(cs.value);
      return d && typeof d.snapshot === "string" ? d : null;
    } catch {
      return null;
    }
  });
  const camSnap = $derived(`/api/camera/${encodeURIComponent(entityId)}/snapshot?w=480`);
  let camLive = $state(false);

  // Localised object-class value (person/vehicle/animal/other/none → Croatian).
  const trClass = (c: string) => {
    const key = `objclass.${c.trim()}`;
    const tr = t(key as Parameters<typeof t>[0]);
    return tr !== key ? tr : c.trim();
  };
  const objectClass = $derived(
    capability === "object_class" && typeof cs.value === "string" ? trClass(cs.value) : String(cs.value ?? ""),
  );

  // Localised identity-presence value (absent/body/face → Croatian).
  const trIdentity = (v: string) => {
    const key = `identity.${v.trim()}`;
    const tr = t(key as Parameters<typeof t>[0]);
    return tr !== key ? tr : v.trim();
  };
  const identityPresence = $derived(
    capability === "identity_presence" && typeof cs.value === "string" ? trIdentity(cs.value) : String(cs.value ?? ""),
  );
  // "since 16:19 · 42 min" under a present person — the same footnote shape a
  // parked vehicle carries, from the same helpers, so the two read alike.
  const identityNote = $derived.by(() => {
    if (capability !== "identity_presence" || !sinceIso) return "";
    if (typeof cs.value !== "string" || cs.value === "absent") return "";
    const held = parkedFor(sinceIso, clock.now), at = parkedSince(sinceIso);
    return [held, at].filter(Boolean).join(" · ");
  });
  // The parked vehicle rides as a JSON descriptor: the name is the reading, how long
  // and since when are its footnote — one line each, so neither wraps into the other.
  const parked = $derived(capability === "parked_vehicle" ? parkedVehicle(cs.value) : null);
  const parkedNote = $derived.by(() => {
    if (!parked) return "";
    const held = parkedFor(parked.since, clock.now), at = parkedSince(parked.since);
    return [held, at].filter(Boolean).join(" · ");
  });
  // An empty place says nothing about itself, but its last occupancy still does —
  // so instead of a dash it remembers out loud: who stood here and how long ago
  // they left, greyed, because a memory must never read like a live reading. Asked
  // for once per open card, from history; a place empty since before the firehose
  // began simply has no memory and keeps the dash.
  let lastParked = $state<ParkedVehicle | null>(null);
  let lastParkedUntil = $state<number | null>(null);
  $effect(() => {
    if (capability !== "parked_vehicle" || parked) { lastParked = null; return; }
    let live = true;
    void api.lastNonEmpty(entityId, capability)
      .then((r) => {
        if (!live) return;
        lastParked = parkedVehicle(r.value);
        lastParkedUntil = r.until;
      })
      .catch(() => { /* no history store, no memory — the dash stands */ });
    return () => { live = false; };
  });
  const lastParkedNote = $derived(
    lastParkedUntil ? t("cap.parkedVehicle.ago", { t: formatDuration((clock.now - lastParkedUntil) / 1000) }) : "",
  );
  // A scene region's state is BABA's own word (present / open / …), localised like any
  // adapter descriptor and worn as a badge — the same shape a binary state gets, in
  // the amber the plan marker and the camera wall already use for a live region.
  const sceneState = $derived(capability === "scene_state" ? String(cs.value ?? "") : "");
  const sceneOn = $derived(sceneState !== "" && sceneLit(sceneState));
  let busy = $state(false);
  let err = $state<string | null>(null);

  // Local slider position follows the real value unless the user is dragging.
  let dragging = $state(false);
  let local = $state(0);
  $effect(() => {
    if (!dragging && typeof cs.value === "number") local = cs.value;
  });

  async function send(command: string, args?: Record<string, number | string>) {
    if (readOnly) return;
    busy = true;
    err = null;
    try {
      await api.sendCommand({ entity_id: entityId, capability, command, args });
    } catch (e) {
      err = errMsg(e);
    } finally {
      busy = false;
    }
  }

  function toggle() {
    const on = cs.value === true;
    if (capability === "lock") send(on ? "unlock" : "lock");
    else send(on ? "turn_off" : "turn_on");
  }

  // Live colour while dragging the picker: update the state optimistically (so the
  // swatch AND the device's marker show the picked colour at once) and stream the
  // change throttled to ~8/s so the light follows in real time without flooding.
  let colorTimer: ReturnType<typeof setTimeout> | null = null;
  let colorLatest = "";
  function liveColor(v: string) {
    if (readOnly) return;
    cs.value = v; // optimistic — swatch + FloorMarker glow reflect it instantly
    colorLatest = v;
    if (colorTimer) return;
    void api.sendCommand({ entity_id: entityId, capability, command: "set_color", args: { value: v } }).catch(() => {});
    colorTimer = setTimeout(() => {
      colorTimer = null;
      if (colorLatest !== v) void api.sendCommand({ entity_id: entityId, capability, command: "set_color", args: { value: colorLatest } }).catch(() => {});
    }, 120);
  }
  onDestroy(() => {
    if (colorTimer) clearTimeout(colorTimer); // don't fire a trailing send after unmount
  });

  function commitSlider() {
    dragging = false;
    if (meta.cmd) send(meta.cmd, { value: local });
  }

  // Number input: clamp to the device range before sending, so an out-of-range
  // value never leaves the UI.
  function submitNumber(el: HTMLInputElement) {
    if (!meta.cmd || el.value === "") return;
    let v = Number(el.value);
    if (lo != null) v = Math.max(lo, v);
    if (hi != null) v = Math.min(hi, v);
    el.value = String(v);
    send(meta.cmd, { value: v });
  }

  const on = $derived(cs.value === true);
  // View-only user (or per-user control restriction baseline): interactive
  // controls are disabled. The API enforces the real boundary; this hides the
  // affordance so a view-only user isn't offered a control that would 403.
  const readOnly = $derived(!auth.canControl);
</script>

<div class="flex min-w-0 flex-col gap-1 py-1.5" class:opacity-60={busy}>
  <div class="flex min-w-0 items-center gap-3" class:justify-between={!compact} class:justify-end={compact}
       class:flex-wrap={meta.control === "camera" && !compact}>
    {#if !compact && !(meta.control === "press" && name)}<span class="text-s font-medium text-dida-text-muted">{label}</span>{/if}

    {#if meta.control === "toggle"}
      <Toggle checked={on} onclick={toggle} disabled={busy || readOnly} label={label} />

    {:else if meta.control === "camera"}
      {#if cam}
        <!-- Static snapshot (one JPEG, not N live streams hammering the source when
             a list is open); click opens the live stream. Both go through DIDA's
             proxy, gated by DIDA login — pixels ride our tunnel. -->
        <button type="button" onclick={() => (camLive = true)}
           class="group relative block shrink-0 overflow-hidden rounded border border-dida-border bg-black
                  {compact ? '' : 'w-full'}" title={cam.stream}>
          <img src={camSnap} alt={cam.stream} loading="lazy"
               class="object-cover {compact ? 'h-11 w-20' : 'aspect-video w-full'}" />
          <span class="pointer-events-none absolute bottom-1 right-1 rounded bg-black/60 px-1 text-2xs
                       text-white opacity-0 transition-opacity group-hover:opacity-100">▶ {t("cameras.live")}</span>
        </button>
        {#if camLive}
          <Dialog {label} size="wide" onclose={() => (camLive = false)}>
            <LiveVideo entityId={entityId} alt={label} class="aspect-video w-full rounded" />
          </Dialog>
        {/if}
      {:else}
        <span class="text-s text-dida-text-faint">—</span>
      {/if}

    {:else if capability === "scene_state"}
      <Tag tone="quiet" kind={sceneOn ? "lit" : undefined}>{sceneState ? tr(sceneState) : "—"}</Tag>

    {:else if capability === "parked_vehicle"}
      {#if parked}
        <!-- an empty name is "occupied, not yet identified" — BABA holds that
             from the moment a car parks until its plate is read. Muted, so it
             never reads as a recognised vehicle, but present, so the previous
             car's memory cannot stand in for whatever is parked now. -->
        <span class="flex min-w-0 flex-col items-end gap-0.5 text-right">
          <span class="min-w-0 truncate text-m {parked.name ? '' : 'text-dida-text-muted'}">
            {parked.name || t("cap.parkedVehicle.unknown")}
          </span>
          {#if parkedNote}<span class="text-s tabular-nums text-dida-text-faint">{parkedNote}</span>{/if}
        </span>
      {:else if lastParked}
        <!-- a memory, not a reading: the whole block is muted so nobody mistakes
             the last car for the one standing there now -->
        <span class="flex min-w-0 flex-col items-end gap-0.5 text-right text-dida-text-faint">
          <span class="min-w-0 truncate text-m">{lastParked.name || t("cap.parkedVehicle.unknown")}</span>
          {#if lastParkedNote}<span class="text-s tabular-nums">{lastParkedNote}</span>{/if}
        </span>
      {:else}
        <span class="text-s text-dida-text-faint">—</span>
      {/if}

    {:else if meta.control === "sensor"}
      <span class="min-w-0 flex-1 [overflow-wrap:anywhere] text-right font-mono text-m tabular-nums">
        {#if capability === "object_class"}{objectClass}
        {:else if capability === "mower_error"}{mowerErrorLabel(String(cs.value))}
        {:else if capability === "identity_presence"}<span class="flex min-w-0 flex-col items-end gap-0.5 text-right"><span>{identityPresence}</span>{#if identityNote}<span class="text-s font-sans tabular-nums text-dida-text-faint">{identityNote}</span>{/if}</span>
        {:else}{typeof cs.value === "number" ? formatValue(capability, cs.value) : tr(String(cs.value))}{/if}
        <span class="text-dida-text-faint">{unit}</span>
      </span>

    {:else if meta.control === "binary"}
      <span
        class="rounded-full px-2 py-0.5 text-s font-semibold
               {on ? 'bg-dida-accent/20 text-dida-accent' : 'bg-dida-border/50 text-dida-text-muted'}"
      >
        {capBadge(capability, on)}
      </span>

    {:else if meta.control === "event"}
      <span class="min-w-0 flex-1 [overflow-wrap:anywhere] text-right font-mono text-m text-dida-text">{cs.value}</span>

    {:else if meta.control === "enum"}
      <select
        value={typeof cs.value === "string" ? cs.value : ""}
        onchange={(e) => send(meta.cmd ?? "set_option", { value: e.currentTarget.value })}
        disabled={busy || readOnly}
        aria-label={label}
       
      >
        {#each options as opt (opt)}<option value={opt}>{optionLabel(opt)}</option>{/each}
      </select>

    {:else if meta.control === "color"}
      <input
        type="color"
        value={typeof cs.value === "string" && /^#[0-9a-fA-F]{6}$/.test(cs.value) ? cs.value : "#ffffff"}
        oninput={(e) => liveColor(e.currentTarget.value)}
        onchange={(e) => send("set_color", { value: e.currentTarget.value })}
        disabled={readOnly}
        aria-label={label}
        class="h-7 w-10 cursor-pointer rounded border border-dida-border bg-dida-panel-2 disabled:cursor-default disabled:opacity-50"
      />

    {:else if meta.control === "mower"}
      <Tag tone="quiet">{mowerStateLabel(String(cs.value))}</Tag>

    {:else if isSlider}
      <span class="font-mono text-m tabular-nums">
        {formatNumber(local, { maximumFractionDigits: 1 })}<span class="text-dida-text-faint">{unit}</span>
      </span>

    {:else if meta.control === "number"}
      <span class="flex shrink-0 items-center gap-1">
        <input type="number" value={typeof cs.value === "number" ? cs.value : ""}
          min={lo} max={hi} step={step ?? "any"} disabled={busy || readOnly} aria-label={label}
          onchange={(e) => submitNumber(e.currentTarget)}
          class="w-20 tabular-nums" />
        {#if unit}<span class="text-s text-dida-text-faint">{unit}</span>{/if}
        {#if lo != null && hi != null}
          <span class="text-s text-dida-text-faint/70">{formatNumber(lo, { maximumFractionDigits: 1 })}–{formatNumber(hi, { maximumFractionDigits: 1 })}</span>
        {/if}
      </span>

    {:else if meta.control === "press"}
      <!-- A named press (a laser's Auto/Music mode) labels the button itself → a clean
           mode menu; a generic press keeps the "Press" label with its row label. -->
      <Button size="small" onclick={() => send("press")} disabled={busy || readOnly}>{name ?? t("cmd.press")}</Button>
    {/if}
  </div>

  {#if meta.control === "mower"}
    <div class="mt-1 flex gap-1 *:flex-1">
      <Button size="small" onclick={() => send("start")} disabled={busy || readOnly}>{t("cmd.start")}</Button>
      <Button size="small" onclick={() => send("pause")} disabled={busy || readOnly}>{t("cmd.pause")}</Button>
      <Button size="small" onclick={() => send("dock")} disabled={busy || readOnly}>{t("cmd.dock")}</Button>
    </div>
  {/if}

  {#if isSlider}
    <input
      type="range"
      min={lo}
      max={hi}
      step={step}
      bind:value={local}
      oninput={() => (dragging = true)}
      onchange={commitSlider}
      disabled={readOnly}
      class="w-full disabled:opacity-50"
      aria-label={label}
    />
    {#if capability === "open_close"}
      <div class="mt-0.5 flex gap-1 *:flex-1">
        <Button size="small" onclick={() => send("open")} disabled={busy || readOnly}>{t("cmd.open")}</Button>
        <Button size="small" onclick={() => send("stop")} disabled={busy || readOnly}>{t("cmd.stop")}</Button>
        <Button size="small" onclick={() => send("close")} disabled={busy || readOnly}>{t("cmd.close")}</Button>
      </div>
    {/if}
  {/if}

  {#if err}
    <span class="text-s text-dida-danger">{err}</span>
  {/if}
</div>
