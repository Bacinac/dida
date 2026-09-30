<script lang="ts">
  import { onMount } from "svelte";
  import { api, type Schedule } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Picks, SaveButton, dialog, i18n } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { isoDate } from "$lib/dt";
  import { auth } from "$lib/auth.svelte";

  let beats = $state<Schedule[]>([]);
  let msg = $state<string | null>(null);
  let name = $state("");
  let editingId = $state<number | null>(null);
  let saving = $state(false);
  let showForm = $state(false); // form stays hidden until "Add schedule" (or Edit) opens it

  let recurType = $state("weekly"); // once | daily | weekly | monthly | yearly
  let interval = $state(1);
  let weekdays = $state<number[]>([0, 1, 2, 3, 4]);
  let monthlyMode = $state<"day" | "weekday">("weekday");
  let dayOffset = $state(0);
  let startDate = $state(todayISO()); // "from" (anchor; drives monthly pattern + window start)
  let endDate = $state("");           // "to" (optional; "" = ∞, else a yearly window end)

  function todayISO() { return isoDate(new Date()); }
  function monthOf(s: string) { const d = new Date(s + "T00:00:00"); return { y: d.getFullYear(), m: d.getMonth() }; }

  let calFrom = $state(monthOf(todayISO()));
  let calTo = $state(monthOf(todayISO()));

  interface Cell { date: string; day: number; inMonth: boolean; weekend: boolean; today: boolean; }
  function monthGrid(y: number, m: number): Cell[] {
    const first = new Date(y, m, 1);
    const off = (first.getDay() + 6) % 7;
    const start = new Date(y, m, 1 - off);
    const today = todayISO();
    const out: Cell[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      out.push({ date: isoDate(d), day: d.getDate(), inMonth: d.getMonth() === m, weekend: (d.getDay() + 6) % 7 >= 5, today: isoDate(d) === today });
    }
    return out;
  }
  const gridFrom = $derived(monthGrid(calFrom.y, calFrom.m));
  const gridTo = $derived(monthGrid(calTo.y, calTo.m));
  function shift(which: "from" | "to", delta: number) {
    const c = which === "from" ? calFrom : calTo;
    const d = new Date(c.y, c.m + delta, 1);
    if (which === "from") calFrom = { y: d.getFullYear(), m: d.getMonth() };
    else calTo = { y: d.getFullYear(), m: d.getMonth() };
  }

  const monthName = (m: number) => t(`schedule.month.${m}` as MessageKey);
  const wdNames = () => Array.from({ length: 7 }, (_, i) => t(`schedule.wd.${i}` as MessageKey));
  const recurLabel = (r: string) => t(`schedule.recur.${r}` as MessageKey) || r;
  // "Every [N] <unit>" dropdown — unit nouns (once has no interval).
  const unitLabel = (r: string) => t(`schedule.unit.${r}` as MessageKey) || r;

  // The pattern phrasings need a weekday in the form the sentence takes: Croatian
  // wants the genitive ("svakog 2. utorka"), English the plain name with an
  // ordinal. Both live in the catalogs; only the ordinal is computed.
  const wdGen = (i: number) => t(`schedule.wdGen.${i}` as MessageKey);
  function _ord(n: number) {
    if (i18n.locale !== "en") return `${n}.`;
    const s = ["th", "st", "nd", "rd"], v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }
  const selDate = $derived(new Date(startDate + "T00:00:00"));
  const selDay = $derived(selDate.getDate());
  const selWd = $derived((selDate.getDay() + 6) % 7);
  const selOcc = $derived(Math.ceil(selDay / 7));
  const monthlyDayLabel = $derived(t("schedule.monthlyDay", { day: selDay }));
  const monthlyWeekdayLabel = $derived(t("schedule.monthlyWeekday", { occ: _ord(selOcc), wd: wdGen(selWd) }));

  function toggleWd(i: number) {
    weekdays = weekdays.includes(i) ? weekdays.filter((x) => x !== i) : [...weekdays, i].sort((a, b) => a - b);
  }
  function pickFrom(d: string) { startDate = d; }
  function pickTo(d: string) { endDate = d === endDate ? "" : d; }

  const hasPattern = $derived(recurType === "weekly" || recurType === "monthly");

  // --- calendar overlay: colour per schedule + "is active on date X" (mirrors
  // the calendar adapter's day-level logic) so the grid shows dots on match days.
  const COLORS = ["bg-emerald-500", "bg-amber-500", "bg-rose-500", "bg-violet-500", "bg-sky-500", "bg-orange-500", "bg-pink-500", "bg-lime-500"];
  const colorFor = (b: Schedule) => COLORS[Math.max(0, beats.findIndex((x) => x.id === b.id)) % COLORS.length];
  function _pd(s: unknown): Date | null { if (!s) return null; const d = new Date(String(s) + "T00:00:00"); return isNaN(d.getTime()) ? null : d; }
  const _cmpMD = (a: Date, b: Date) => (a.getMonth() !== b.getMonth() ? a.getMonth() - b.getMonth() : a.getDate() - b.getDate());
  const _inWindow = (d: Date, sd: Date, ed: Date) => (_cmpMD(sd, ed) <= 0 ? _cmpMD(sd, d) <= 0 && _cmpMD(d, ed) <= 0 : _cmpMD(d, sd) >= 0 || _cmpMD(d, ed) <= 0);
  function _recur(d: Date, p: Record<string, unknown>, sd: Date): boolean {
    const rt = String(p.recurrence_type ?? ""); const wd = (d.getDay() + 6) % 7;
    if (rt === "daily") return true;
    if (rt === "once") return d.getTime() === sd.getTime();
    if (rt === "weekly") return (Array.isArray(p.weekdays) ? (p.weekdays as number[]) : []).includes(wd);
    if (rt === "monthly") return p.monthly_mode === "weekday" ? wd === Number(p.monthly_weekday) && Math.ceil(d.getDate() / 7) === Number(p.week_occurrence) : d.getDate() === Number(p.monthly_day);
    if (rt === "yearly") return d.getMonth() === sd.getMonth() && d.getDate() === sd.getDate();
    return false;
  }
  function isActive(b: Schedule, dateStr: string): boolean {
    const p = (b.params ?? {}) as Record<string, unknown>;
    const d = new Date(dateStr + "T00:00:00");
    const offset = Number(p.day_offset ?? 0);
    const sd = _pd(p.start_date) ?? d;
    const anchor = new Date(d.getFullYear(), d.getMonth(), d.getDate() - offset);
    if (anchor < sd) return false;
    const ed = _pd(p.end_date);
    if (ed && !_inWindow(d, sd, ed)) return false;
    return _recur(anchor, p, sd);
  }
  const dotColors = (dateStr: string) => beats.filter((b) => b.enabled && isActive(b, dateStr)).map(colorFor);

  function buildParams(): Record<string, unknown> {
    const p: Record<string, unknown> = { recurrence_type: recurType, start_date: startDate };
    if (recurType !== "once" && endDate) p.end_date = endDate;
    if (recurType === "monthly" && monthlyMode === "weekday" && dayOffset) p.day_offset = Number(dayOffset);
    if (recurType !== "once") p.interval = interval;
    if (recurType === "weekly") p.weekdays = weekdays;
    if (recurType === "monthly") {
      p.monthly_mode = monthlyMode;
      if (monthlyMode === "day") p.monthly_day = selDay;
      else { p.monthly_weekday = selWd; p.week_occurrence = selOcc; }
    }
    return p;
  }

  const wdFull = (i: number) => t(`schedule.wdFull.${i}` as MessageKey);
  const monthDay = (s: unknown) => { const d = _pd(s); return d ? `${monthName(d.getMonth())} ${d.getDate()}` : ""; };
  function summary(b: Schedule): string {
    const p = (b.params ?? {}) as Record<string, unknown>;
    const rt = String(p.recurrence_type ?? "");
    // season / window: a daily beat with an end date → "May 1 – September 5"
    if (rt === "daily" && p.end_date) return `${monthDay(p.start_date)} – ${monthDay(p.end_date)}`;
    const bits = [recurLabel(rt)];
    if (rt === "weekly" && Array.isArray(p.weekdays)) bits.push((p.weekdays as number[]).map((i) => wdFull(i)).join(", "));
    if (rt === "monthly") bits.push(p.monthly_mode === "weekday"
      ? t("schedule.patternWeekday", { occ: _ord(Number(p.week_occurrence)), wd: wdGen(Number(p.monthly_weekday)) })
      : t("schedule.patternDay", { day: String(p.monthly_day) }));
    if (rt === "once") bits.push(String(p.start_date ?? ""));
    const off = Number(p.day_offset ?? 0);
    if (off) bits.push(`(${off > 0 ? "+" : ""}${off}d)`);
    if (p.end_date) bits.push(`· ${monthDay(p.start_date)} – ${monthDay(p.end_date)}`);
    return bits.join(" ");
  }

  async function load() { try { beats = await api.listSchedules(); } catch (e) { msg = errMsg(e); } }
  function resetForm() { editingId = null; name = ""; savedForm = ""; }
  const current = () => JSON.stringify({ name: name.trim(), params: buildParams() });
  let savedForm = $state("");
  const dirty = $derived(editingId == null || current() !== savedForm);
  function openAdd() { resetForm(); showForm = true; }
  function closeForm() { resetForm(); showForm = false; }
  function startEdit(b: Schedule) {
    const p = (b.params ?? {}) as Record<string, unknown>;
    editingId = b.id;
    name = b.name;
    recurType = String(p.recurrence_type ?? "weekly");
    interval = Number(p.interval ?? 1);
    weekdays = Array.isArray(p.weekdays) ? (p.weekdays as number[]).slice() : [0, 1, 2, 3, 4];
    monthlyMode = (p.monthly_mode as "day" | "weekday") ?? "weekday";
    dayOffset = Number(p.day_offset ?? 0);
    startDate = String(p.start_date ?? todayISO());
    endDate = p.end_date ? String(p.end_date) : "";
    calFrom = monthOf(startDate);
    calTo = endDate ? monthOf(endDate) : monthOf(todayISO());
    msg = null;
    savedForm = current();
    showForm = true;
  }
  async function save() {
    msg = null;
    if (!name.trim()) return;
    saving = true;
    try {
      if (editingId != null) await api.patchSchedule(editingId, { name: name.trim(), params: buildParams() });
      else await api.createSchedule(name.trim(), "calendar", buildParams());
      closeForm();
      await load();
    } catch (e) { msg = errMsg(e); }
    finally { saving = false; }
  }
  async function toggle(b: Schedule) { try { await api.patchSchedule(b.id, { enabled: !b.enabled }); await load(); } catch (e) { msg = errMsg(e); } }
  async function remove(b: Schedule) {
    const ok = await dialog.confirm({ title: t("common.delete"), message: t("schedules.confirmDelete", { name: b.name }), confirmLabel: t("common.delete"), danger: true });
    if (!ok) return;
    try { await api.deleteSchedule(b.id); await load(); } catch (e) { msg = errMsg(e); }
  }
  onMount(load);

  const gl = "text-xs text-dida-text-muted";
</script>

{#snippet miniCal(label: string, cells: Cell[], selected: string, onPrev: () => void, onNext: () => void, onPick: (d: string) => void)}
  <div class="w-full max-w-[13rem]">
    <div class="mb-1.5 flex items-center justify-between">
      <Button size="small" onclick={onPrev} label={t("schedule.prevMonth")}>‹</Button>
      <span class="text-s font-semibold">{label}</span>
      <Button size="small" onclick={onNext} label={t("schedule.nextMonth")}>›</Button>
    </div>
    <div class="grid grid-cols-7 gap-0.5 text-center text-xs text-dida-text-muted">
      {#each wdNames() as wd (wd)}<div class="py-0.5">{wd}</div>{/each}
      {#each cells as c (c.date)}
        <button type="button" onclick={() => onPick(c.date)}
          class="relative aspect-square rounded text-xs transition-colors
            {c.date === selected ? 'bg-dida-accent-strong text-dida-on-accent font-semibold' : c.inMonth ? 'hover:bg-dida-panel-2' : 'text-dida-text-faint/40'}
            {c.weekend && c.date !== selected ? 'text-dida-text-faint' : ''}
            {c.today && c.date !== selected ? 'ring-1 ring-dida-accent/60' : ''}">
          {c.day}
          {#if c.inMonth}
            {@const dc = dotColors(c.date)}
            {#if dc.length}
              <span class="pointer-events-none absolute inset-x-0 bottom-0.5 flex justify-center gap-[1px]">
                {#each dc.slice(0, 4) as col, ci (ci)}<span class="h-1 w-1 rounded-full {col}"></span>{/each}
              </span>
            {/if}
          {/if}
        </button>
      {/each}
    </div>
  </div>
{/snippet}

<svelte:head><title>{t("schedules.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  {#if showForm}
  <Card>
    <div class="grid grid-cols-1 items-start gap-6 md:grid-cols-3">
      <!-- selection rows (first of three equal columns) -->
      <div class="flex flex-col gap-3">
        <div class="flex flex-wrap items-center gap-2">
          {#if recurType !== "once"}
            <span class={gl}>{t("schedule.every")}</span>
            <input type="number" min="1" bind:value={interval} class="w-14" />
          {/if}
          <select bind:value={recurType}>
            {#each ["once", "daily", "weekly", "monthly", "yearly"] as r (r)}<option value={r}>{unitLabel(r)}</option>{/each}
          </select>
        </div>
        {#if hasPattern}
          <div class="flex flex-col items-start gap-1.5">
            <span class={gl}>{recurType === "weekly" ? t("schedule.days") : t("schedule.pattern")}</span>
            {#if recurType === "weekly"}
              <Picks
                many
                picks={wdNames().map((wd, i) => ({ key: String(i), label: wd }))}
                chosen={weekdays.map(String)}
                onpick={(k) => toggleWd(Number(k))}
              />
            {:else}
              <label class="flex cursor-pointer items-center gap-2 rounded border px-2 py-1 text-s {monthlyMode === 'day' ? 'border-dida-accent bg-dida-accent/10' : 'border-dida-border bg-dida-panel-2'}">
                <input type="radio" value="day" bind:group={monthlyMode} /> {monthlyDayLabel}
              </label>
              <label class="flex cursor-pointer items-center gap-2 rounded border px-2 py-1 text-s {monthlyMode === 'weekday' ? 'border-dida-accent bg-dida-accent/10' : 'border-dida-border bg-dida-panel-2'}">
                <input type="radio" value="weekday" bind:group={monthlyMode} /> {monthlyWeekdayLabel}
              </label>
              {#if monthlyMode === "weekday"}
                <div class="flex items-center gap-1.5">
                  <span class={gl}>{t("schedule.offset")}</span>
                  <input type="number" bind:value={dayOffset} class="w-11" />
                  <span class={gl}>{t("schedule.daysUnit")}</span>
                </div>
              {/if}
            {/if}
          </div>
        {/if}
      </div>

      <!-- calendar(s) — same horizontal row as the selection -->
      {#if recurType === "once"}
        <div>
          <p class="mb-1 {gl}">{t("schedule.date")}: <b>{startDate}</b></p>
          {@render miniCal(monthName(calFrom.m) + " " + calFrom.y, gridFrom, startDate, () => shift("from", -1), () => shift("from", 1), pickFrom)}
        </div>
      {:else}
        <div>
          <p class="mb-1 {gl}">{recurType === "monthly" ? t("schedule.fromPattern") : t("schedule.from")}: <b>{startDate}</b></p>
          {@render miniCal(monthName(calFrom.m) + " " + calFrom.y, gridFrom, startDate, () => shift("from", -1), () => shift("from", 1), pickFrom)}
        </div>
        <div>
          <p class="mb-1 {gl}">{t("schedule.to")}: <b>{endDate || "∞"}</b>{#if endDate}<span class="ml-2"><Button size="small" onclick={() => (endDate = "")}>{t("schedule.noEnd")}</Button></span>{/if}</p>
          {@render miniCal(monthName(calTo.m) + " " + calTo.y, gridTo, endDate, () => shift("to", -1), () => shift("to", 1), pickTo)}
        </div>
      {/if}
    </div>

    <div class="mt-5 flex flex-wrap items-end gap-2 border-t border-dida-border/60 pt-4">
      <input bind:value={name} placeholder={t("schedules.name")} class="min-w-56 flex-1" />
      <SaveButton size="small" {dirty} {saving} blocked={!name.trim()} onclick={save} label={editingId != null ? undefined : t("schedules.add")} />
      <Button onclick={closeForm}>{t("common.cancel")}</Button>
      {#if msg}<span class="text-s text-dida-danger">{msg}</span>{/if}
    </div>
  </Card>
  {:else}
    <div class="mb-4">
      <Button tone="primary" onclick={openAdd}>＋ {t("schedules.new")}</Button>
    </div>
  {/if}

  <div class="mt-4"><Card>
    <table class="w-full text-m">
      <thead class="text-left text-s text-dida-text-muted">
        <tr><th class="py-1 font-medium">{t("schedules.name")}</th><th class="py-1 font-medium">{t("schedules.when")}</th><th></th><th></th><th></th></tr>
      </thead>
      <tbody>
        {#each beats as b (b.id)}
          <tr class="border-t border-dida-border/60" class:opacity-50={!b.enabled}>
            <td class="py-1.5 font-medium"><span class="mr-2 inline-block h-2.5 w-2.5 rounded-full align-middle {colorFor(b)} {b.enabled ? '' : 'opacity-30'}"></span>{b.name}</td>
            <td class="py-1.5 text-s text-dida-text-muted">{summary(b)}</td>
            <td class="py-1.5 text-right"><Button size="small" onclick={() => startEdit(b)}>{t("common.edit")}</Button></td>
            <td class="py-1.5 text-right"><Button size="small" onclick={() => toggle(b)}>{b.enabled ? t("schedules.disable") : t("schedules.enable")}</Button></td>
            <td class="py-1.5 text-right"><Button tone="danger" size="small" onclick={() => remove(b)}>{t("common.delete")}</Button></td>
          </tr>
        {/each}
        {#if beats.length === 0}<tr><td class="py-2 text-s text-dida-text-faint" colspan="5">{t("schedules.empty")}</td></tr>{/if}
      </tbody>
    </table>
  </Card></div>
{/if}
