<script lang="ts">
  // One device, all of it — the UI half of GET /entities/{id}/detail.
  //
  // Before this, answering "what IS this thing and why did it just do that" meant
  // four surfaces: Devices for the registry row, the live card for values, nothing
  // at all for the other entities on the same physical device, Automations for the
  // rules and Settings → Commands for the audit. This is that, in one panel.
  import { api, type EntityDetail, type EntityStats } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { Dialog, Tabs, Tag, formatNumber, type TagTone } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dayTime } from "$lib/dt";
  import { capLabel, OPTION_CAPS } from "$lib/capabilities";
  import DeviceIcon from "$lib/DeviceIcon.svelte";
  import FloorHistory from "$lib/FloorHistory.svelte";
  import { capMeta } from "$lib/capabilities";
  import { devices } from "$lib/store.svelte";

  let { entityId, onclose }: { entityId: string | null; onclose: () => void } = $props();

  let detail = $state<EntityDetail | null>(null);
  let stats = $state<EntityStats | null>(null);
  let err = $state<string | null>(null);
  let tab = $state("overview");
  // ONE chart at a time, opened by clicking its tile — never a stack of them
  // below the values (that shape was rejected on the floor plan in July, and
  // it is no better here).
  let chartFor = $state<{ entityId: string; cap: string; name: string; key: string } | null>(null);
  let seq = 0;

  // Reload whenever the drawer is pointed at a different device. A stale response
  // from the previously-opened one must not land in the panel, so the fetch is
  // sequence-guarded (same latest-wins shape used elsewhere in the app).
  $effect(() => {
    const id = entityId;
    if (!id) { detail = null; err = null; return; }
    const mine = ++seq;
    detail = null; stats = null; err = null; tab = "overview"; chartFor = null;
    api.entityDetail(id)
      .then((d) => { if (mine === seq) detail = d; })
      .catch((e) => { if (mine === seq) err = errMsg(e); });
    // Cumulatives are a SECOND call on purpose: they scan a month of history and
    // must not hold up the panel, and a device with nothing to total (most of
    // them) simply never shows the row. A failure here is silence, not an error —
    // the panel's job is what the device IS, and it already has that.
    api.entityStats(id)
      .then((s_) => { if (mine === seq) stats = s_; })
      .catch(() => {});
  });

  const TABS = $derived([
    { key: "overview", label: t("detail.overview") },
    { key: "device", label: t("detail.device") },
    { key: "timeline", label: t("detail.timeline") },
  ]);

  // ONE past for the device, not two lists side by side: what happened to it
  // (the journal) and what was asked of it (the command audit, admin-only) are
  // the same story told from either end, and reading them apart is what made the
  // couch-light case take an evening. Merged, newest first.
  type Entry =
    | { t: "event"; ms: number; kind: string; severity: string; source: string; message: string; key: string }
    | { t: "command"; ms: number; source: string; command: string; capability: string; key: string };

  const entries = $derived.by<Entry[]>(() => {
    if (!detail) return [];
    // Events that arrived AFTER the panel opened, from the live socket. Scoped
    // the same way the server scopes the fetch — this entity, its siblings, and
    // its adapter's own lifecycle — so a panel left open still answers "why did
    // it just go quiet" without a reload.
    const mine = new Set([detail.entity_id, ...detail.siblings.map((s_) => s_.entity_id)]);
    const adapterSource = `adapter:${detail.adapter}`;
    const oldest = detail.events[0]?.ms ?? 0;
    const live = devices.liveEvents.filter(
      (e) => e.ms > oldest
        && (mine.has(e.entity_id)
          || (e.device_key && e.device_key === detail!.device_key)
          || (!e.entity_id && !e.device_key && e.source === adapterSource)),
    );
    const out: Entry[] = [
      ...live.map((e, i) => ({
        t: "event" as const, ms: e.ms, kind: e.kind, severity: e.severity,
        source: e.source, message: e.message, key: `l${e.ms}:${i}`,
      })),
      ...detail.events.map((e, i) => ({
        t: "event" as const, ms: e.ms, kind: e.kind, severity: e.severity,
        source: e.source, message: e.message, key: `e${e.ms}:${i}`,
      })),
      ...(detail.commands ?? []).map((c, i) => ({
        t: "command" as const, ms: c.ms, source: c.source,
        command: c.command, capability: c.capability, key: `c${c.ms}:${i}`,
      })),
    ];
    return out.sort((a, b) => b.ms - a.ms);
  });

  // A rejected reading and an adapter dropping off are the two things worth
  // spotting at a glance; everything else is background.
  const SEVERITY_TONE: Record<string, TagTone> = {
    error: "err",
    warning: "warn",
    notice: "busy",
  };
  // The kind is JournalKind in dida_core.journal; the words check holds every one to a word.
  const kindLabel = (kind: string) => t(`detail.event.${kind}` as MessageKey);

  // A reading that stopped arriving is a fact about the device, not a number to
  // show as if it were current — so it is stated in words, not silently stale.
  const STALE_S = 6 * 3600;
  function ageLabel(s: number): string {
    if (s < 90) return t("detail.now");
    if (s < 3600) return t("detail.minAgo", { n: Math.round(s / 60) });
    if (s < 86400) return t("detail.hAgo", { n: Math.round(s / 3600) });
    return t("detail.dAgo", { n: Math.round(s / 86400) });
  }

  // Every value the DEVICE reports, primary entity first, then its siblings. When
  // the device is several entities the tile is labelled "<entity> · <capability>",
  // because "Power" three times over says nothing about which leg it is.
  const tiles = $derived.by(() => {
    if (!detail) return [];
    const multi = detail.siblings.length > 0;
    const short = (id: string, name: string | null, label: string | null) =>
      label ?? name ?? id.split(":").pop() ?? id;
    const rows = [
      ...detail.state.filter((v) => !OPTION_CAPS.has(v.capability)).map((v) => ({
        key: detail!.entity_id + "|" + v.capability,
        entityId: detail!.entity_id, cap: v.capability,
        label: multi
          ? `${short(detail!.entity_id, detail!.name, detail!.label)} · ${capLabel(v.capability)}`
          : capLabel(v.capability),
        value: v.value, unit: v.unit, age_s: v.age_s,
      })),
      ...detail.siblings.flatMap((sib) =>
        (sib.state ?? []).filter((v) => !OPTION_CAPS.has(v.capability)).map((v) => ({
          key: sib.entity_id + "|" + v.capability,
          entityId: sib.entity_id, cap: v.capability,
          label: `${short(sib.entity_id, sib.name, sib.label)} · ${capLabel(v.capability)}`,
          value: v.value, unit: v.unit, age_s: v.age_s,
        }))),
    ];
    return rows;
  });

  // "How much" and "how often", when there is such a thing. A device with nothing
  // to total shows no row at all rather than a line of zeros.
  const statTiles = $derived.by(() => {
    if (!stats) return [];
    const unitOf = (cap: string) =>
      detail?.state.find((v) => v.capability === cap)?.unit
      ?? detail?.siblings.flatMap((s_) => s_.state ?? []).find((v) => v.capability === cap)?.unit
      ?? null;
    return [
      ...stats.counters.map((c) => ({
        key: `c${c.capability}`, label: capLabel(c.capability),
        value: formatNumber(c.total, { maximumFractionDigits: 1 }), unit: unitOf(c.capability),
      })),
      ...stats.runtime.flatMap((r) => [
        { key: `h${r.capability}`, label: t("detail.hoursOn"),
          value: formatNumber(r.hours_on, { maximumFractionDigits: 1 }), unit: t("detail.hoursUnit") },
        { key: `y${r.capability}`, label: t("detail.cycles"),
          value: formatNumber(r.cycles, { maximumFractionDigits: 1 }), unit: null },
        { key: `d${r.capability}`, label: t("detail.duty"),
          value: formatNumber(Math.round(r.duty * 1000) / 10, { maximumFractionDigits: 1 }), unit: "%" },
      ]),
    ];
  });

  // Hours actually covered, when that is materially less than the 30 days asked
  // for. Null when the answer spans the whole window.
  const shortWindow = $derived.by(() => {
    const w = stats?.runtime?.[0]?.window_h;
    return w !== undefined && w < 30 * 24 * 0.9 ? Math.round(w) : null;
  });

  const CHARTABLE = new Set(["sensor", "number", "slider", "toggle", "binary", "mower"]);
  const chartable = (cap: string) => CHARTABLE.has(capMeta(cap).control);

  function fmt(v: unknown): string {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? t("detail.on") : t("detail.off");
    if (typeof v === "number") return formatNumber(v, { maximumFractionDigits: 1 });
    return String(v);
  }

  // The audit's `source` is "user:marko" | "automation:12:Evening" | "matter" | a bus
  // client name. Split it so the WHO reads as a word and the kind can be coloured —
  // "who did this" is the question the trail exists to answer.
  function who(source: string): { kind: string; name: string } {
    const [kind, ...rest] = source.split(":");
    const name = rest.length ? rest[rest.length - 1] : kind;
    return { kind, name: name || source };
  }
  const SOURCE_KINDS = new Set(["user", "automation", "assistant", "matter", "heating"]);
</script>

{#if entityId !== null}
<Dialog size={tiles.length > 2 ? "normal" : "narrow"} label={detail?.name ?? "device"} {onclose}>
  {#if err}
    <p class="text-m text-dida-danger">{err}</p>
  {:else if !detail}
    <p class="text-m text-dida-text-muted">{t("common.loading")}</p>
  {:else}
    <header class="mb-4 flex items-start gap-3 border-b border-dida-border pb-3 pr-10">
      <DeviceIcon type={detail.fp_style?.icon || detail.device_type || "sensor"}
        class="mt-0.5 size-5 shrink-0 text-dida-text-muted" />
      <div class="min-w-0 flex-1">
        <h2 class="truncate text-l font-semibold leading-tight">
          {detail.label ?? detail.name ?? detail.entity_id}
        </h2>
        <p class="mt-0.5 truncate font-mono text-xs text-dida-text-faint">{detail.entity_id}</p>
      </div>
      <Tag tone="quiet">{detail.adapter}</Tag>
    </header>

    <div class="mb-4"><Tabs tabs={TABS} active={tab} onpick={(k) => (tab = k)} /></div>

    {#if tab === "overview"}
      {#if tiles.length === 0}
        <p class="text-m text-dida-text-muted">{t("detail.noReadings")}</p>
      {:else}
        <!-- The WHOLE device, not just the clicked entity: a meter is five entities
             (power, voltage, current, import, export), so showing one read as
             half-empty. Each tile names its own entity when the device has several. -->
        <div class="grid gap-2 {tiles.length > 2 ? 'sm:grid-cols-2' : ''}">
          {#each tiles as x (x.key)}
            {@const canChart = chartable(x.cap)}
            <svelte:element
              this={canChart ? "button" : "div"}
              type={canChart ? "button" : undefined}
              role={canChart ? "button" : undefined}
              onclick={canChart
                ? () => (chartFor = chartFor?.key === x.key
                    ? null
                    : { entityId: x.entityId, cap: x.cap, name: x.label, key: x.key })
                : undefined}
              class="rounded-lg border bg-dida-panel-2 px-3 py-2 text-left
                {chartFor?.key === x.key ? 'border-dida-accent' : 'border-dida-border'}
                {canChart ? 'hover:border-dida-accent' : ''}">
              <p class="truncate text-xs uppercase tracking-wide text-dida-text-faint">
                {x.label}
              </p>
              <p class="mt-0.5 truncate text-xl font-semibold leading-snug tabular-nums">
                {fmt(x.value)}{#if x.unit}<span class="ml-1 text-s font-normal text-dida-text-muted">{x.unit}</span>{/if}
              </p>
              <p class="mt-0.5 text-xs {x.age_s > STALE_S ? 'text-dida-warn' : 'text-dida-text-faint'}">
                {ageLabel(x.age_s)}
              </p>
            </svelte:element>
          {/each}
        </div>
        {#if chartFor}
          <!-- Reuses the floor plan's own history card — same ranges, same chart,
               no second implementation. No ⓘ passed: we are already in the panel. -->
          <div class="mt-3">
            <FloorHistory readings={[{ entityId: chartFor.entityId, cap: chartFor.cap }]}
              cap={chartFor.cap} name={chartFor.name} />
          </div>
        {/if}
      {/if}

      {#if statTiles.length}
        <h3 class="mt-5 mb-2 text-s font-semibold uppercase tracking-wide text-dida-text-muted">
          {t("detail.last30")}
          <!-- A device younger than the window (or a truncated answer) covers less
               than the heading claims. Say the real span rather than let the
               heading make a promise the numbers don't keep. -->
          {#if shortWindow}<span class="ml-1 normal-case font-normal text-dida-text-faint">
            · {t("detail.onlySince", { h: formatNumber(shortWindow, { maximumFractionDigits: 1 }) })}
          </span>{/if}
        </h3>
        <div class="grid gap-2 {statTiles.length > 2 ? 'sm:grid-cols-3' : 'sm:grid-cols-2'}">
          {#each statTiles as x (x.key)}
            <div class="rounded-lg border border-dida-border bg-dida-panel-2 px-3 py-2">
              <p class="truncate text-xs uppercase tracking-wide text-dida-text-faint">{x.label}</p>
              <p class="mt-0.5 truncate text-xl font-semibold leading-snug tabular-nums">
                {x.value}{#if x.unit}<span class="ml-1 text-s font-normal text-dida-text-muted">{x.unit}</span>{/if}
              </p>
            </div>
          {/each}
        </div>
      {/if}

      {#if detail.automations.length}
        <h3 class="mt-5 mb-2 text-s font-semibold uppercase tracking-wide text-dida-text-muted">
          {t("detail.usedBy")}
        </h3>
        <ul class="space-y-1">
          {#each detail.automations as a (a.id)}
            <li class="flex items-center justify-between gap-3 rounded-lg border border-dida-border bg-dida-panel-2 px-3 py-2">
              <a class="min-w-0 flex-1 truncate text-m text-dida-accent hover:underline"
                href="/automations?id={a.id}">{a.name}</a>
              <span class="shrink-0 text-xs {a.enabled ? 'text-dida-text-faint' : 'text-dida-warn'}">
                {#if !a.enabled}{t("detail.disabled")}
                {:else if a.last_triggered_at}{dayTime((a.last_triggered_at))}
                {:else}{t("detail.neverFired")}{/if}
              </span>
            </li>
          {/each}
        </ul>
      {/if}

    {:else if tab === "device"}
      <dl class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-m">
        <dt class="text-dida-text-muted">{t("detail.adapter")}</dt><dd class="font-medium">{detail.adapter}</dd>
        <dt class="text-dida-text-muted">{t("detail.type")}</dt>
        <dd class="font-medium">{detail.device_type ?? "—"}</dd>
        <dt class="text-dida-text-muted">{t("detail.lastSeen")}</dt>
        <dd class="font-medium">{detail.last_seen ? dayTime((detail.last_seen)) : "—"}</dd>
      </dl>
      {#if detail.siblings.length}
        <h3 class="mt-5 mb-2 text-s font-semibold uppercase tracking-wide text-dida-text-muted">
          {t("detail.sameDevice")}
        </h3>
        <ul class="space-y-1">
          {#each detail.siblings as s (s.entity_id)}
            <li class="rounded-lg border border-dida-border bg-dida-panel-2 px-3 py-2">
              <p class="truncate text-m">{s.label ?? s.name ?? s.entity_id}</p>
              <p class="truncate font-mono text-xs text-dida-text-faint">{s.entity_id}</p>
            </li>
          {/each}
        </ul>
      {/if}

    {:else if tab === "timeline"}
      {#if entries.length === 0}
        <p class="text-m text-dida-text-muted">{t("detail.noEvents")}</p>
      {:else}
        <ul class="space-y-1">
          {#each entries as x (x.key)}
            <li class="flex items-center gap-3 rounded-lg border border-dida-border bg-dida-panel-2 px-3 py-2">
              {#if x.t === "command"}
                {@const w = who(x.source)}
                <Tag tone="quiet" kind={SOURCE_KINDS.has(w.kind) ? w.kind : undefined}>{w.name}</Tag>
                <span class="min-w-0 flex-1 truncate text-m">{x.command}</span>
              {:else}
                <Tag tone={SEVERITY_TONE[x.severity] ?? "quiet"}>{kindLabel(x.kind)}</Tag>
                <span class="min-w-0 flex-1 truncate text-m {x.message ? '' : 'text-dida-text-muted'}">
                  {x.message || x.source}
                </span>
              {/if}
              <span class="shrink-0 text-xs text-dida-text-faint">{dayTime((x.ms))}</span>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/if}
</Dialog>
{/if}
