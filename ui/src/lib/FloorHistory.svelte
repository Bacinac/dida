<script lang="ts">
  // Inline history popover for a room reading, opened by tapping a value in the
  // expanded room label. Keeps the user on the plan — a focused chart + range
  // picker, reusing the same TimeChart as /history. A reading can be backed by
  // several sensors (a two-thermometer room): we plot the room MEAN over time,
  // bucket-aligned across those sensors (one sensor → its own series, exactly).
  import { Button, Picks } from "$lib/kit";
  import { api, type HistorySeries, type HistoryNumPoint, type HistoryStrPoint } from "$lib/api";
  import {
    MOWER_STATE_ORDER, capLabel, capMeta, mowerStateLabel, parkedVehicleLane, parkedVehicleLaneLabel,
  } from "$lib/capabilities";
  import { errMsg } from "$lib/errors";
  import { t, type MessageKey } from "$lib/i18n";
  import TimeChart from "$lib/TimeChart.svelte";

  let { readings, cap, name, ondetail }: {
  readings: { entityId: string; cap: string }[]; cap: string; name: string;
  ondetail?: (entityId: string) => void;   // open the device-detail panel
} = $props();

  const RANGES: { key: MessageKey; hours: number }[] = [
    { key: "history.range6h", hours: 6 },
    { key: "history.range24h", hours: 24 },
    { key: "history.range7d", hours: 168 },
    { key: "history.range30d", hours: 720 },
  ];

  let hours = $state(24);
  // Boolean channels (on_off, occupancy, contact …) are stored as 0/1 — render
  // them as an on/off step timeline, not a floating-point line.
  const step = $derived(["toggle", "binary"].includes(capMeta(cap).control));
  let series = $state<HistorySeries | null>(null);
  // Categorical lane labels (mower docked/mowing/…) when the channel is a string
  // state: the series carries lane indices, this carries their display names.
  let states = $state<string[] | null>(null);
  let stateKeys = $state<string[] | null>(null);   // the raw names behind the labels
  let loading = $state(false);
  let err = $state<string | null>(null);
  let reqSeq = 0; // guards against a slow earlier range overwriting a newer one

  const stateLabel = (s: string): string =>
    cap === "mower" || cap === "vacuum" ? mowerStateLabel(s)
    : cap === "parked_vehicle" ? parkedVehicleLaneLabel(s)
    : s;

  // A string-state series (raw change points) → index-valued step series + lane
  // labels. States sit in canonical order when we know one (mower), else in
  // first-seen order; the last state is extended to "now" so the step reaches
  // the right edge of the chart instead of dying at the last transition.
  function toLanes(pts: HistoryStrPoint[], bucket: number): { s: HistorySeries; labels: string[]; order: string[] } {
    const seen: string[] = [];
    for (const p of pts) if (!seen.includes(p.s)) seen.push(p.s);
    const canon = MOWER_STATE_ORDER.filter((x) => seen.includes(x));
    const order = cap === "mower" || cap === "vacuum" ? [...canon, ...seen.filter((x) => !canon.includes(x))] : seen;
    const num: HistoryNumPoint[] = pts.map((p) => {
      const v = order.indexOf(p.s);
      return { ts: p.ts, v, min: v, max: v, last: v };
    });
    const lastV = num[num.length - 1].v;
    num.push({ ts: Date.now(), v: lastV, min: lastV, max: lastV, last: lastV });
    return { s: { capability: cap, numeric: true, bucket_seconds: bucket, points: num },
             labels: order.map(stateLabel), order };
  }

  // A reading mirrored from another installation is LIVE here and archived THERE,
  // so this house has no series to draw. Say that instead of asking for one and
  // rendering "no data", which reads as a broken sensor.
  const remote = $derived(readings.length > 0 && readings.every((r) => r.entityId.startsWith("peer:")));

  $effect(() => { if (!remote) void load($state.snapshot(readings), hours); });

  async function load(rs: { entityId: string; cap: string }[], h: number) {
    const seq = ++reqSeq;
    loading = true;
    err = null;
    try {
      const all = (await Promise.all(
        rs.map((r) => api.history(r.entityId, [r.cap], h).then((res) => res.series[0] ?? null).catch(() => null)),
      )).filter((s): s is HistorySeries => s != null);
      if (seq !== reqSeq) return; // a newer range was requested — drop this stale result
      const got = all.filter((s) => s.numeric);
      const strs = all.find((s) => !s.numeric && s.points.length);
      if (!got.some((s) => s.points.length) && strs) {
        // A string-state channel (mower run-state, enum): categorical timeline.
        const pts = strs.points as HistoryStrPoint[];
        const lanes = toLanes(
          cap === "parked_vehicle" ? pts.map((p) => ({ ...p, s: parkedVehicleLane(p.s) })) : pts,
          strs.bucket_seconds,
        );
        series = lanes.s;
        states = lanes.labels;
        stateKeys = lanes.order;
      } else if (got.length <= 1) {
        states = null;
      stateKeys = null;
        stateKeys = null;
        series = got[0] ?? null;
      } else {
        // Mean across sensors, aligned by bucket timestamp.
        states = null;
      stateKeys = null;
        stateKeys = null;
        const byTs = new Map<number, number[]>();
        for (const s of got) for (const p of s.points as HistoryNumPoint[]) {
          const arr = byTs.get(p.ts) ?? []; arr.push(p.v); byTs.set(p.ts, arr);
        }
        const points: HistoryNumPoint[] = [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([ts, vs]) => {
          const v = vs.reduce((s, x) => s + x, 0) / vs.length;
          return { ts, v, min: Math.min(...vs), max: Math.max(...vs), last: v };
        });
        series = { capability: cap, numeric: true, bucket_seconds: got[0].bucket_seconds, points };
      }
    } catch (e) {
      if (seq !== reqSeq) return;
      err = errMsg(e);
      series = null;
      states = null;
      stateKeys = null;
    } finally {
      if (seq === reqSeq) loading = false;
    }
  }
</script>

<div class="rounded-lg border border-dida-border bg-dida-panel p-3 shadow-xl">
  <div class="mb-2 flex items-baseline justify-between gap-2 pr-7">
    <div class="flex min-w-0 items-baseline gap-1.5">
      <h3 class="truncate text-m font-semibold leading-tight">{name}</h3>
      {#if ondetail && readings.length}
        <Button size="small" label={t("detail.title")} title={t("detail.title")} onclick={() => ondetail?.(readings[0].entityId)}>ⓘ</Button>
      {/if}
    </div>
    <span class="shrink-0 text-s text-dida-text-muted">{capLabel(cap)}{#if readings.length > 1} · ⌀ {readings.length}{/if}</span>
  </div>

  <div class="mb-2 flex gap-1" class:hidden={remote}>
    <Picks picks={RANGES.map((r) => ({ key: String(r.hours), label: t(r.key) }))} chosen={[String(hours)]} onpick={(k) => (hours = Number(k))} />
  </div>

  {#if remote}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("history.remote")}</p>
  {:else if err}
    <p class="py-6 text-center text-m text-dida-danger">{err}</p>
  {:else if loading && !series}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("common.loading")}</p>
  {:else if series}
    <TimeChart {series} height={150} {step} states={states ?? undefined} keys={stateKeys ?? undefined} />
  {:else}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("history.noData")}</p>
  {/if}
</div>
