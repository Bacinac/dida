<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { api, type ActiveAlert, type OrphansData, type SystemStats } from "$lib/api";
  import { auth } from "$lib/auth.svelte";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Notice, Stats, Tag, formatNumber, type TagTone } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { shortDateTime } from "$lib/dt";
  import { Page, Section } from "$lib/ui";

  let stats = $state<SystemStats | null>(null);
  let alerts = $state<ActiveAlert[]>([]);
  let orphans = $state<OrphansData | null>(null);
  let error = $state("");
  let silencing = $state<string | null>(null);
  let silenceErr = $state<string | null>(null);
  let timer: ReturnType<typeof setInterval> | undefined;

  const SILENCE_FOR: { hours: number | null; label: () => string }[] = [
    { hours: 1, label: () => t("alerts.for1h") },
    { hours: 8, label: () => t("alerts.for8h") },
    { hours: 24, label: () => t("alerts.for1d") },
    { hours: 168, label: () => t("alerts.for7d") },
    { hours: null, label: () => t("alerts.untilResolved") },
  ];

  const alertId = (a: ActiveAlert) => `${a.key}|${a.scope}`;

  function silencedLabel(a: ActiveAlert): string {
    if (!a.silenced_until) return t("alerts.silencedUntilResolved");
    return t("alerts.silencedUntil", { time: shortDateTime(a.silenced_until) });
  }

  async function silence(a: ActiveAlert, hours: number | null) {
    silenceErr = null;
    try {
      await api.silenceAlert(a.key, a.scope, hours);
      silencing = null;
      await load();
    } catch (e) {
      silenceErr = errMsg(e);
    }
  }

  async function unsilence(a: ActiveAlert) {
    silenceErr = null;
    try {
      await api.unsilenceAlert(a.key, a.scope);
      await load();
    } catch (e) {
      silenceErr = errMsg(e);
    }
  }

  function ruleLabel(key: string): string {
    const k = `alerts.rule.${key}` as Parameters<typeof t>[0];
    const label = t(k);
    return label === k ? key : label;
  }

  async function load() {
    try {
      const [s, a, o] = await Promise.all([api.systemStats(), api.systemAlerts(), api.systemOrphans()]);
      stats = s;
      alerts = a.active;
      orphans = o;
      error = "";
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    load();
    timer = setInterval(load, 3000); // live poll — the engine counters move in real time
  });
  onDestroy(() => clearInterval(timer));

  // Compact, single-line duration — the stat cards are narrow (6-up on wide), so
  // "1 h 40 m" wrapped. h:mm (1:40) reads as a duration and never breaks.
  function uptime(s: number): string {
    if (s < 60) return `${Math.round(s)} s`;
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const hm = `${h}:${String(m).padStart(2, "0")}`;
    if (d > 0) return `${d}d ${hm}`;
    if (h > 0) return hm;
    return `${m} m`;
  }

  const STATE_TONE: Record<string, TagTone> = {
    ok: "ok",
    connecting: "warn",
    idle: "quiet",
    error: "err",
    offline: "quiet",
  };
</script>

<svelte:head><title>{t("system.title")} · DIDA</title></svelte:head>

<Page wide>

  {#if alerts.length}
    <div class="flex flex-col">
      {#each alerts as a (alertId(a))}
        <div class="text-m {a.silenced ? 'opacity-70' : ''}"><Notice tone={a.severity === "critical" ? "err" : "warn"}>
          <div class="flex flex-wrap items-center gap-3">
            <span class="shrink-0">{a.severity === "critical" ? "⚠️" : "⚡"}</span>
            <span class="min-w-0 flex-1">{a.message}</span>
            <span class="shrink-0 text-s uppercase tracking-wide opacity-70">{ruleLabel(a.key)}</span>
            {#if a.silenced}
              <span class="shrink-0 text-s opacity-80">{silencedLabel(a)}</span>
            {/if}
            {#if auth.isAdmin}
              {#if a.silenced}
                <Button size="small" onclick={() => unsilence(a)}>{t("alerts.unsilence")}</Button>
              {:else}
                <Button size="small"
                  onclick={() => (silencing = silencing === alertId(a) ? null : alertId(a))}
                >{t("alerts.silence")}</Button>
              {/if}
            {/if}
          </div>
          {#if silencing === alertId(a) && !a.silenced}
            <div class="flex flex-wrap gap-2">
              {#each SILENCE_FOR as opt (opt.hours ?? "resolved")}
                <Button size="small" onclick={() => silence(a, opt.hours)}>{opt.label()}</Button>
              {/each}
            </div>
          {/if}
        </Notice></div>
      {/each}
      {#if silenceErr}<p class="text-s text-dida-danger">{silenceErr}</p>{/if}
    </div>
  {/if}

  {#if error}
    <Notice tone="err">{error}</Notice>
  {:else if !stats}
    <p class="text-m text-dida-text-muted">…</p>
  {:else}
    <Section title={t("system.engine")}>
      {#if stats.engine}
        {@const e = stats.engine}
        <Stats stats={[
          { label: t("system.accepted"), value: e.accepted, tone: "ok" },
          { label: t("system.rejected"), value: e.rejected, tone: e.rejected ? "err" : undefined },
          { label: t("system.stale"), value: e.stale },
          { label: t("system.backlog"), value: e.backlog < 0 ? "?" : e.backlog, tone: e.backlog > 0 ? "warn" : undefined },
          { label: t("system.uptime"), value: uptime(e.uptime_s) },
          {
            label: t("system.historyBuf"),
            value: `${formatNumber(e.history.buffered)} · ↯${formatNumber(e.history.dropped)}`,
            tone: e.history.dropped ? "err" : e.history.connected ? "ok" : "warn",
          },
        ]} />
      {:else}
        <p class="text-m text-dida-danger">{t("system.engineOffline")}</p>
      {/if}
    </Section>

    {#if orphans && orphans.count > 0}
      <Section title={t("system.orphans")}>
        <p class="mb-3 text-m text-dida-text-muted">{t("system.orphansHint")}</p>
        <div class="flex flex-col gap-2">
          {#each orphans.orphans as o (o.kind + o.id)}
            <Card>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span class="text-s uppercase tracking-wide text-dida-text-muted">{t(`system.orphanKind.${o.kind}` as MessageKey)}</span>
                <span class="min-w-0 flex-1 truncate text-m">{o.name}</span>
                <span class="shrink-0 font-mono text-s text-dida-danger">{o.missing.join(", ")}</span>
              </div>
            </Card>
          {/each}
        </div>
      </Section>
    {/if}

    <Section title={t("system.datastores")}>
      <div class="flex flex-wrap gap-2 text-m">
        {#each Object.entries(stats.db) as [name, up] (name)}
          <span class="rounded-full px-3 py-1 font-medium {up ? 'bg-dida-ok/15 text-dida-ok' : 'bg-dida-danger/15 text-dida-danger'}">
            {name} · {up ? t("system.up") : t("system.down")}
          </span>
        {/each}
      </div>
    </Section>

    <Section title={t("system.adapters")}>
      <div class="mb-3 flex flex-wrap gap-2 text-m">
        {#each Object.entries(stats.adapter_counts).filter(([, n]) => n > 0) as [state, n] (state)}
          <Tag tone={STATE_TONE[state] ?? "fact"}>{state} · {n}</Tag>
        {/each}
      </div>
      <div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {#each stats.adapters as a (a.name)}
          <Card>
            <div class="flex items-center justify-between gap-2">
              <span class="truncate text-m">{a.name}</span>
              <Tag tone={STATE_TONE[a.state] ?? "fact"} title={a.detail}>{a.state}</Tag>
            </div>
          </Card>
        {/each}
      </div>
    </Section>
  {/if}
</Page>
