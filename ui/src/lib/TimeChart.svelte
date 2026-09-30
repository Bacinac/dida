<script lang="ts">
  // One capability's history as a time-series line. Composed from layerchart
  // primitives (not the simplified LineChart) so we control the axis formatting:
  // layerchart's built-in axes default to en/US ("6/29", "12 PM", "1,000"), and
  // ALL numbers in this app must go through the Croatian-aware formatNumber. So we
  // pass explicit `format` fns — Croatian dates/times on x, formatNumber on y.
  import { Chart, Svg, Axis, Spline, Highlight } from "layerchart";
  import { curveStepAfter } from "d3-shape";
  import type { HistorySeries, HistoryNumPoint } from "$lib/api";
  import { formatNumber, i18n } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { clock, dayDate, shortDate } from "$lib/dt";

  let {
    series,
    color = "var(--color-dida-accent)",
    height = 240,
    step = false,
    states,
    keys,
  }: {
    series: HistorySeries; color?: string; height?: number; step?: boolean;
    // Categorical timeline (mower docked/mowing/…): the caller maps each state to
    // its index in `states` and sends an index-valued numeric series. Drawn as a
    // BAND, not a line: the distance between "idle" and "charging" means nothing,
    // and a state that lasted a minute has to be visible at all.
    states?: string[];
    keys?: string[];   // the raw state names behind those labels, for colour
  } = $props();

  const cat = $derived(states != null && states.length > 0);
  const stepped = $derived(step || cat);

  // Booleans (motion/contact/on_off…) are logged as 0/1. Per bucket we take `max`
  // (was it EVER on in this window → 1, else 0), not the average — otherwise a
  // half-active hour reads as a meaningless "0.5". Numeric caps use the average.
  const data = $derived(
    series.numeric
      ? (series.points as HistoryNumPoint[]).map((p) => ({ date: new Date(p.ts), v: stepped && !cat ? p.max : p.v }))
      : [],
  );

  // y-axis: locale numbers (never a raw d3 "1,000").
  const fmtNum = (v: unknown): string => (typeof v === "number" ? formatNumber(v, { maximumFractionDigits: 1 }) : String(v ?? ""));
  // Booleans: label the two states, not 0 / 0.5 / 1.
  const fmtBool = (v: unknown): string => (typeof v === "number" && v >= 0.5 ? t("history.on") : t("history.off"));
  // Categorical lanes: the state name at its index, nothing between lanes.
  const fmtState = (v: unknown): string =>
    typeof v === "number" && states ? (states[Math.round(v)] ?? "") : "";

  // x-axis: short intraday ranges → time (12:00), multi-day → date (29.6.).
  const spanDays = $derived(
    data.length > 1 ? (data.at(-1)!.date.getTime() - data[0].date.getTime()) / 86_400_000 : 0,
  );
  function fmtDate(value: unknown): string {
    const d = value instanceof Date ? value : new Date(value as number);
    return spanDays <= 2 ? clock(d) : shortDate(d);
  }
  // One coloured run per state, from its first report to the next change. The span
  // comes from the DATA, so a month-long window over two days of history shows those
  // two days rather than twenty-eight empty ones.
  interface Band { x: number; w: number; label: string; key: string; from: Date; to: Date }
  const bands = $derived.by((): Band[] => {
    if (!cat || data.length < 2) return [];
    const t0 = data[0].date.getTime();
    const t1 = data.at(-1)!.date.getTime();
    const span = Math.max(1, t1 - t0);
    const out: Band[] = [];
    for (let i = 0; i < data.length - 1; i++) {
      const from = data[i].date, to = data[i + 1].date;
      const w = ((to.getTime() - from.getTime()) / span) * 100;
      if (w <= 0) continue;
      const idx = Math.round(data[i].v);
      out.push({ x: ((from.getTime() - t0) / span) * 100, w,
                 label: states?.[idx] ?? "", key: keys?.[idx] ?? String(idx), from, to });
    }
    return out;
  });

  // Colour carries meaning, not variety: working is the accent, on its way is amber,
  // resting is muted, broken is red.
  const BAND_COLOR: Record<string, string> = {
    cleaning: "var(--color-dida-accent)", mowing: "var(--color-dida-accent)",
    starting: "#22d3ee", returning: "#f59e0b", paused: "#fb923c",
    charging: "#38bdf8", docked: "#475569", idle: "#334155", leaving: "#22d3ee",
    error: "#ef4444",
    parked: "var(--color-dida-accent)", unidentified: "#f59e0b", vacant: "#334155",
  };
  const bandColor = (key: string): string => BAND_COLOR[key] ?? BAND_COLOR[key.split(":")[0]] ?? "#64748b";
  const seenStates = $derived([...new Map(bands.map((b) => [b.key, b.label])).entries()]);

  // Over more than a couple of days, one long strip hides the thing worth seeing:
  // that it happens every other morning at four. Stacking a day per row, each 00–24,
  // puts those runs under each other.
  const byDay = $derived(spanDays > 2);
  interface DayRow { key: number; label: string; runs: Band[] }
  const dayRows = $derived.by((): DayRow[] => {
    if (!byDay || bands.length === 0) return [];
    const rows = new Map<number, DayRow>();
    const loc = i18n.locale === "en" ? "en-GB" : "hr-HR";
    for (const b of bands) {
      // A run that crosses midnight belongs to both days, cut at the boundary.
      let from = b.from;
      while (from < b.to) {
        const midnight = new Date(from);
        midnight.setHours(24, 0, 0, 0);
        const to = midnight < b.to ? midnight : b.to;
        const day = new Date(from);
        day.setHours(0, 0, 0, 0);
        const key = day.getTime();
        if (!rows.has(key)) {
          rows.set(key, { key, runs: [],
            label: dayDate(day) });
        }
        const x = ((from.getTime() - key) / 86_400_000) * 100;
        rows.get(key)!.runs.push({ ...b, x, w: ((to.getTime() - from.getTime()) / 86_400_000) * 100,
                                   from, to });
        from = to;
      }
    }
    return [...rows.values()].sort((a, b2) => b2.key - a.key);
  });
  const HOUR_MARKS = [0, 6, 12, 18];

  const timeOf = (d: Date): string => clock(d);
  const minutesOf = (b: Band): number => Math.round((b.to.getTime() - b.from.getTime()) / 60000);

  // A handful of marks along the band, dense enough to place a run and sparse enough
  // to read.
  const ticks = $derived.by(() => {
    if (bands.length === 0) return [];
    const t0 = data[0].date.getTime(), t1 = data.at(-1)!.date.getTime();
    return Array.from({ length: 5 }, (_, i) => {
      const at = new Date(t0 + ((t1 - t0) * i) / 4);
      return { x: (i / 4) * 100, label: fmtDate(at) };
    });
  });

</script>

{#if data.length === 0}
  <p class="py-8 text-center text-m text-dida-text-faint">{t("history.noData")}</p>
{:else if cat && byDay}
  <div>
    <div class="mb-1 flex items-center gap-2 text-xs text-dida-text-faint">
      <span class="w-14 shrink-0"></span>
      <span class="relative h-3 flex-1">
        {#each HOUR_MARKS as h (h)}
          <span class="absolute" style="left:{(h / 24) * 100}%">{String(h).padStart(2, "0")}</span>
        {/each}
        <span class="absolute right-0">24</span>
      </span>
    </div>
    <div class="flex max-h-56 flex-col gap-0.5 overflow-y-auto">
      {#each dayRows as row (row.key)}
        <div class="flex items-center gap-2">
          <span class="w-14 shrink-0 text-xs text-dida-text-muted">{row.label}</span>
          <div class="relative h-4 flex-1 overflow-hidden rounded-sm border border-dida-border bg-dida-panel-2">
            {#each HOUR_MARKS.slice(1) as h (h)}
              <span class="absolute inset-y-0 w-px bg-dida-border/60" style="left:{(h / 24) * 100}%"></span>
            {/each}
            {#each row.runs as b (b.from.getTime())}
              <div class="absolute inset-y-0" style="left:{b.x}%;width:{b.w}%;background:{bandColor(b.key)}"
                title="{b.label} · {timeOf(b.from)}–{timeOf(b.to)} ({minutesOf(b)} min)"></div>
            {/each}
          </div>
        </div>
      {/each}
    </div>
    <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-dida-text-muted">
      {#each seenStates as [key, label] (key)}
        <span class="flex items-center gap-1">
          <span class="size-2 rounded-sm" style="background:{bandColor(key)}"></span>{label}
        </span>
      {/each}
    </div>
  </div>
{:else if cat}
  <div>
    <div class="relative h-4 w-full overflow-hidden rounded-sm border border-dida-border bg-dida-panel-2">
      {#each bands as b (b.from.getTime())}
        <div class="absolute inset-y-0" style="left:{b.x}%;width:{b.w}%;background:{bandColor(b.key)}"
          title="{b.label} · {timeOf(b.from)}–{timeOf(b.to)} ({minutesOf(b)} min)"></div>
      {/each}
    </div>
    <div class="relative mt-1 h-4 w-full text-xs text-dida-text-faint">
      {#each ticks as tick, i (tick.x)}
        <span class="absolute whitespace-nowrap"
          style="left:{tick.x}%;transform:translateX({i === 0 ? '0' : i === ticks.length - 1 ? '-100%' : '-50%'})">{tick.label}</span>
      {/each}
    </div>
    <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-dida-text-muted">
      {#each seenStates as [key, label] (key)}
        <span class="flex items-center gap-1">
          <span class="size-2 rounded-sm" style="background:{bandColor(key)}"></span>{label}
        </span>
      {/each}
    </div>
  </div>
{:else}
  <div style="height:{height}px">
    <Chart
      {data}
      x="date"
      y="v"
      yNice={!stepped}
      yDomain={cat ? [-0.3, states!.length - 1 + 0.3] : stepped ? [-0.08, 1.08] : undefined}
      padding={{ left: cat ? 74 : stepped ? 44 : 52, bottom: 24, top: 8, right: 14 }}
      tooltipContext
    >
      <Svg>
        <Axis placement="left" grid rule
          format={cat ? fmtState : stepped ? fmtBool : fmtNum}
          ticks={cat ? states!.map((_, i) => i) : stepped ? [0, 1] : undefined} />
        <Axis placement="bottom" rule format={fmtDate} />
        <Spline stroke={color} width={1.75} curve={stepped ? curveStepAfter : undefined} />
        <Highlight points lines />
      </Svg>
    </Chart>
  </div>
{/if}
