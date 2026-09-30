<script lang="ts">
  // House-energy dashboard (History → Energija) for ONE day, hour by hour:
  // consumption drawn UP, solar production DOWN, on an hourly axis, with a date
  // picker. Per-device series live on the device itself (floor-plan click), never
  // here — this is the whole-house view. Data: GET /history/energy/hourly.
  import { api, type EnergyHourly, type EnergyConfig } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { auth } from "$lib/auth.svelte";
  import { Button, SaveButton, Tag, formatNumber } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { isoDate } from "$lib/dt";
  import { SECTION_TITLE_CLASS } from "$lib/ui";

  const todayISO = (): string => isoDate(new Date());

  let dateStr = $state(todayISO());
  // Local-day bounds as unix seconds — the frontend owns the timezone; the backend
  // just buckets [frm, to) and the returned hour timestamps map back to local hours.
  const bounds = $derived.by(() => {
    const frm = Math.floor(new Date(dateStr + "T00:00:00").getTime() / 1000);
    return { frm, to: frm + 86400 };
  });
  const isToday = $derived(dateStr >= todayISO());
  function shiftDay(delta: number) {
    const d = new Date(dateStr + "T00:00:00");
    d.setDate(d.getDate() + delta);
    dateStr = isoDate(d);
  }

  let data = $state<EnergyHourly | null>(null);
  let loading = $state(false);
  let err = $state<string | null>(null);

  async function load() {
    loading = true;
    err = null;
    try {
      data = await api.energyHourly(bounds.frm, bounds.to);
    } catch (e) {
      err = errMsg(e);
      data = null;
    } finally {
      loading = false;
    }
  }
  $effect(() => {
    void dateStr;
    load();
  });

  const totals = $derived(data?.totals);
  const hours = $derived(data?.hours ?? []);
  const kwh = (v: number | null | undefined): string =>
    v == null ? "—" : formatNumber(Math.round(v * 10) / 10, { maximumFractionDigits: 1 }) + " kWh";

  // ── hourly mirror chart: consumption up, production down, 24h axis ──
  const W = 760;
  const H = 250;
  const PAD = { l: 26, r: 8, t: 12, b: 18 };
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const midY = PAD.t + plotH / 2;
  const half = plotH / 2 - 2;
  const slot = plotW / 24;
  const barW = Math.min(18, slot * 0.72);
  const xC = (i: number): number => PAD.l + slot * (i + 0.5);
  const byHour = $derived.by(() => {
    const arr: (EnergyHourly["hours"][number] | null)[] = Array(24).fill(null);
    for (const h of hours) arr[new Date(h.ts).getHours()] = h;
    return arr;
  });
  const maxV = $derived(
    Math.max(0.2, ...hours.map((h) => Math.max(h.consumption ?? 0, h.export ?? 0))),
  );
  const hh = (v: number): number => (half * v) / maxV;

  // Second chart: own production, hour by hour (its own scale).
  const H2 = 150;
  const plotH2 = H2 - PAD.t - PAD.b;
  const pbase = PAD.t + plotH2;
  const prodMax = $derived(Math.max(0.2, ...hours.map((h) => h.production ?? 0)));
  const phh = (v: number): number => (plotH2 * v) / prodMax;

  // Distinct palette for the per-device consumption breakdown, kept off the
  // amber/rose/sky energy-flow colours.
  const DEV_COLORS = ["#3b82f6", "#a855f7", "#ec4899", "#14b8a6", "#84cc16", "#f97316", "#06b6d4", "#eab308"];
  // Friendly device name for a device prefix. The prefix is the entity id minus its
  // last segment (e.g. "esphome:203_0_113_71"), but the store keys devices by their
  // registry device_key (e.g. "203_0_113_71"), so resolve via a matching entity.
  const devLabel = (dev: string): string => {
    const e = devices.list.find((d) => d.entityId.startsWith(dev + ":") || d.entityId === dev);
    const key = e?.deviceKey ?? null;
    return devices.deviceLabel(key) || devices.deviceRawName(key) || dev.split(":").pop() || dev;
  };
  // A meter row wears the entity's name like everywhere else; the raw id lives
  // in the tooltip. The id tail only shows for a meter the store no longer has.
  const meterName = (id: string): string => devices.byId[id]?.name ?? id.split(":").pop() ?? id;

  // ── meter role config (admin) — the source of truth for which sensor is the grid
  //    meter / solar inverter / a sub-meter; the heuristic is only a default. ──
  let cfgOpen = $state(false);
  let cfg = $state<EnergyConfig | null>(null);
  let roles = $state<Record<string, string>>({});
  let rolesInit = $state("");
  let cfgBusy = $state(false);
  let cfgMsg = $state<string | null>(null);
  const allRoles = $derived(cfg?.roles ?? []);
  const suggested = $derived(
    Object.fromEntries((cfg?.meters ?? []).map((m) => [m.entity_id, m.suggested])),
  );
  const cfgDirty = $derived(rolesInit !== "" && JSON.stringify(roles) !== rolesInit);
  const cfgGroups = $derived.by(() => {
    const g = new Map<string, EnergyConfig["meters"]>();
    for (const m of cfg?.meters ?? []) {
      const parts = m.entity_id.split(":");
      const dev = parts.length > 2 ? parts.slice(0, -1).join(":") : m.entity_id;
      const arr = g.get(dev);
      if (arr) arr.push(m);
      else g.set(dev, [m]);
    }
    return [...g.entries()];
  });
  const roleLabel = (r: string): string => t(`energy.role.${r}` as MessageKey);

  $effect(() => {
    if (cfgOpen && !cfg) loadCfg();
  });
  async function loadCfg() {
    try {
      cfg = await api.energyConfig();
      roles = Object.fromEntries(cfg.meters.map((m) => [m.entity_id, m.role]));
      rolesInit = JSON.stringify(roles);
    } catch (e) {
      cfgMsg = errMsg(e);
    }
  }
  async function saveCfg() {
    cfgBusy = true;
    cfgMsg = null;
    try {
      // Persist only true overrides (role ≠ the auto suggestion), so devices left
      // on "auto" keep following future heuristic improvements.
      const overrides: Record<string, string> = {};
      for (const [e, r] of Object.entries(roles)) if (r !== suggested[e]) overrides[e] = r;
      await api.saveEnergyConfig(overrides);
      rolesInit = JSON.stringify(roles);
      cfgMsg = t("energy.configSaved");
      await load(); // recompute with the new roles
    } catch (e) {
      cfgMsg = errMsg(e);
    } finally {
      cfgBusy = false;
    }
  }
</script>

<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
  <div class="flex items-center gap-1">
    <Button onclick={() => shiftDay(-1)} label={t("energy.prevDay")}>‹</Button>
    <input type="date" bind:value={dateStr} max={todayISO()} />
    <Button onclick={() => shiftDay(1)} disabled={isToday} label={t("energy.nextDay")}>›</Button>
    {#if !isToday}
      <Button onclick={() => (dateStr = todayISO())}>{t("energy.today")}</Button>
    {/if}
  </div>
  {#if auth.isAdmin}
    <Button size="small" onclick={() => (cfgOpen = !cfgOpen)}>{cfgOpen ? t("common.done") : t("common.edit")}</Button>
  {/if}
</div>

{#if err}
  <p class="py-6 text-center text-m text-dida-danger">{err}</p>
{:else if !data || (loading && hours.length === 0)}
  <p class="py-8 text-center text-m text-dida-text-faint">{t("common.loading")}</p>
{:else if hours.length === 0 || !totals}
  <p class="py-8 text-center text-m text-dida-text-muted">{t("energy.noDayData")}</p>
{:else}
  <!-- KPI cards (this day) -->
  {@const bal = totals.production - totals.consumption}
  <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
    <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
      <div class="text-s text-dida-text-muted">{t("energy.consumption")}</div>
      <div class="mt-1 text-xl font-semibold">{kwh(totals.consumption)}</div>
    </div>
    <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
      <div class="text-s text-dida-text-muted">{t("energy.production")}</div>
      <div class="mt-1 text-xl font-semibold text-dida-production">{kwh(totals.production)}</div>
    </div>
    <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
      <div class="text-s text-dida-text-muted">{t("energy.balance")}</div>
      <div class="mt-1 text-xl font-semibold {bal >= 0 ? 'text-dida-ok' : 'text-dida-import'}">{bal >= 0 ? '+' : '−'}{kwh(Math.abs(bal))}</div>
    </div>
    <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
      <div class="text-s text-dida-text-muted">{t("energy.selfSufficiency")}</div>
      <div class="mt-1 text-xl font-semibold text-dida-accent">
        {totals.self_sufficiency == null ? "—" : formatNumber(totals.self_sufficiency, { maximumFractionDigits: 1 }) + " %"}
      </div>
    </div>
    <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
      <div class="text-s text-dida-text-muted">{t("energy.import")} / {t("energy.export")}</div>
      <div class="mt-1 text-m font-semibold">
        <span class="text-dida-import">↓ {kwh(totals.import)}</span>
        <span class="mx-1 text-dida-text-faint">·</span>
        <span class="text-dida-warn">↑ {kwh(totals.export)}</span>
      </div>
    </div>
  </div>

  <!-- Hourly mirror chart: consumption up (self-use + import), production down
       (self-use + export). The green self-use segment mirrors around the zero line. -->
  <div class="mt-3 rounded-lg border border-dida-border bg-dida-panel p-3">
    <div class="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-s text-dida-text-muted">
      <span class="flex items-center gap-1"><span class="inline-block h-2 w-3 rounded-sm bg-dida-production"></span>{t("energy.selfConsumed")}</span>
      <span class="flex items-center gap-1"><span class="inline-block h-2 w-3 rounded-sm bg-dida-import"></span>{t("energy.import")}</span>
      <span class="flex items-center gap-1"><span class="inline-block h-2 w-3 rounded-sm bg-dida-export"></span>{t("energy.export")}</span>
      <span class="ml-auto text-dida-text-faint">{t("energy.upDown")}</span>
    </div>
    <svg viewBox="0 0 {W} {H}" class="w-full" preserveAspectRatio="none" role="img" aria-label={t("energy.hourly")}>
      <!-- top / mid / bottom guides + kWh scale -->
      <line x1={PAD.l} y1={midY - half} x2={W - PAD.r} y2={midY - half} class="stroke-dida-border" stroke-width="0.4" stroke-dasharray="3 3" />
      <line x1={PAD.l} y1={midY} x2={W - PAD.r} y2={midY} class="stroke-dida-border" stroke-width="0.8" />
      <line x1={PAD.l} y1={midY + half} x2={W - PAD.r} y2={midY + half} class="stroke-dida-border" stroke-width="0.4" stroke-dasharray="3 3" />
      <text x="2" y={midY - half + 3} class="fill-dida-text-faint text-2xs">{formatNumber(Math.round(maxV * 10) / 10, { maximumFractionDigits: 1 })}</text>
      <text x="2" y={midY + 3} class="fill-dida-text-faint text-2xs">0</text>
      <text x="2" y={midY + half} class="fill-dida-text-faint text-2xs">{formatNumber(Math.round(maxV * 10) / 10, { maximumFractionDigits: 1 })}</text>
      {#each byHour as h, i (i)}
        {#if h && h.consumption != null}
          {@const s = h.self_consumed ?? 0}
          <!-- UP (used at home): solar self-use (amber) + grid import (rose) -->
          <rect x={xC(i) - barW / 2} y={midY - hh(s)} width={barW} height={hh(s)} class="fill-dida-production" />
          <rect x={xC(i) - barW / 2} y={midY - hh(s) - hh(h.import)} width={barW} height={hh(h.import)} class="fill-dida-import" />
          <!-- DOWN (to the grid): surplus solar exported (sky) -->
          <rect x={xC(i) - barW / 2} y={midY} width={barW} height={hh(h.export)} class="fill-dida-export" />
        {/if}
      {/each}
      {#each [0, 3, 6, 9, 12, 15, 18, 21] as hr (hr)}
        <text x={xC(hr)} y={H - 5} text-anchor="middle" class="fill-dida-text-faint text-2xs">{hr}h</text>
      {/each}
    </svg>
  </div>

  <!-- Second chart: own production, hour by hour -->
  <div class="mt-3 rounded-lg border border-dida-border bg-dida-panel p-3">
    <div class="mb-2 flex items-center gap-1 text-s text-dida-text-muted">
      <span class="inline-block h-2 w-3 rounded-sm bg-dida-production"></span>{t("energy.production")}
    </div>
    <svg viewBox="0 0 {W} {H2}" class="w-full" preserveAspectRatio="none" role="img" aria-label={t("energy.production")}>
      <line x1={PAD.l} y1={PAD.t} x2={W - PAD.r} y2={PAD.t} class="stroke-dida-border" stroke-width="0.4" stroke-dasharray="3 3" />
      <text x="2" y={PAD.t + 3} class="fill-dida-text-faint text-2xs">{formatNumber(Math.round(prodMax * 10) / 10, { maximumFractionDigits: 1 })}</text>
      <line x1={PAD.l} y1={pbase} x2={W - PAD.r} y2={pbase} class="stroke-dida-border" stroke-width="0.7" />
      {#each byHour as h, i (i)}
        {#if h && h.production > 0}
          <rect x={xC(i) - barW / 2} y={pbase - phh(h.production)} width={barW} height={phh(h.production)} class="fill-dida-production" />
        {/if}
      {/each}
      {#each [0, 3, 6, 9, 12, 15, 18, 21] as hr (hr)}
        <text x={xC(hr)} y={H2 - 5} text-anchor="middle" class="fill-dida-text-faint text-2xs">{hr}h</text>
      {/each}
    </svg>
  </div>

  <!-- Third bar: each device's share of consumption (one stacked bar, per-device colours) -->
  {#if data.submeters.length && totals.consumption > 0}
    {@const cons = totals.consumption}
    {@const metered = data.submeters.reduce((a, s) => a + s.kwh, 0)}
    {@const other = Math.max(0, Math.round((cons - metered) * 100) / 100)}
    <div class="mt-3 rounded-lg border border-dida-border bg-dida-panel p-3">
      <h3 class="{SECTION_TITLE_CLASS} mb-2">{t("energy.byConsumer")}</h3>
      <div class="mb-3 flex h-6 overflow-hidden rounded bg-dida-panel-2">
        {#each data.submeters as s, i (s.device)}
          <div title="{devLabel(s.device)}: {kwh(s.kwh)}" style="width:{(100 * s.kwh) / cons}%;background:{DEV_COLORS[i % DEV_COLORS.length]}"></div>
        {/each}
        <div class="bg-dida-text-faint/30" style="width:{(100 * other) / cons}%"></div>
      </div>
      <div class="flex flex-col gap-1">
        {#each data.submeters as s, i (s.device)}
          <div class="flex items-center gap-2 text-m">
            <span class="size-2.5 shrink-0 rounded-sm" style="background:{DEV_COLORS[i % DEV_COLORS.length]}"></span>
            <span class="min-w-0 flex-1 truncate" title={s.device}>{devLabel(s.device)}</span>
            <span class="shrink-0 text-s text-dida-text-muted">{kwh(s.kwh)} · {Math.round((100 * s.kwh) / cons)}%</span>
          </div>
        {/each}
        <div class="flex items-center gap-2 text-m">
          <span class="size-2.5 shrink-0 rounded-sm bg-dida-text-faint/40"></span>
          <span class="min-w-0 flex-1 truncate text-dida-text-muted">{t("energy.other")}</span>
          <span class="shrink-0 text-s text-dida-text-muted">{kwh(other)} · {Math.round((100 * other) / cons)}%</span>
        </div>
      </div>
    </div>
  {/if}

  {#if auth.isAdmin && cfgOpen}
    <div class="mt-3 rounded-lg border border-dida-border bg-dida-panel p-4">
      <h3 class="{SECTION_TITLE_CLASS} mb-2">{t("energy.config")}</h3>
      {#if !cfg}
        <p class="text-s text-dida-text-faint">{t("common.loading")}</p>
      {:else}
        <div class="flex flex-col gap-3">
          {#each cfgGroups as [dev, ms] (dev)}
            <div>
              <div class="mb-1 text-s font-medium text-dida-text-muted">{devLabel(dev)}</div>
              <div class="flex flex-col gap-1">
                {#each ms as m (m.entity_id)}
                  <div class="flex items-center gap-2 rounded border border-dida-border/60 bg-dida-panel-2 px-2 py-1.5 text-m">
                    <span class="min-w-0 flex-1 truncate text-s text-dida-text-muted" title={m.entity_id}>{meterName(m.entity_id)}</span>
                    {#if !m.exposed}
                      <Tag tone="warn" title={t("energy.meterOffHint")}>{t("energy.meterOff")}</Tag>
                    {/if}
                    <select bind:value={roles[m.entity_id]} class="shrink-0">
                      {#each allRoles as r (r)}<option value={r}>{roleLabel(r)}</option>{/each}
                    </select>
                  </div>
                {/each}
              </div>
            </div>
          {/each}
        </div>
        <div class="mt-3 flex items-center gap-3">
          <SaveButton size="small" dirty={cfgDirty} saving={cfgBusy} onclick={saveCfg} />
          {#if cfgMsg}<span class="text-s text-dida-text-muted">{cfgMsg}</span>{/if}
        </div>
      {/if}
    </div>
  {/if}
{/if}
