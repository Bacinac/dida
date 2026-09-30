<script lang="ts">
  // A room's weekly schedule as a list of steps: from this time, on these days,
  // run this profile — until the next step. A list rather than a 7×24 grid because
  // that is how heating is actually reasoned about ("warm at 06:30, back down at
  // 09:00"), and because it stays usable on a phone.
  import type { HeatingSlot } from "$lib/api";
  import { Button, Field, Picks } from "$lib/kit";
  import { SCHEDULE_PROFILES } from "$lib/heating";
  import { t, type MessageKey } from "$lib/i18n";

  // `inheritLabel` marks a schedule that may be left empty because something else
  // supplies it (a room falls back to the house schedule). Absent = this IS the
  // schedule, and empty means comfort all day.
  let { slots = $bindable(), inheritLabel }: { slots: HeatingSlot[]; inheritLabel?: string } = $props();

  const WORKDAYS = [0, 1, 2, 3, 4];
  // Abbreviated weekday labels — the full `backup.day.*` names don't fit a chip.
  const DAYS = [0, 1, 2, 3, 4, 5, 6];

  function add() {
    slots = [...slots, { days: [...WORKDAYS], at: "06:30", profile: "comfort" }];
  }
  function remove(i: number) {
    slots = slots.filter((_, n) => n !== i);
  }
  function toggleDay(i: number, day: number) {
    const days = slots[i].days.includes(day)
      ? slots[i].days.filter((d) => d !== day)
      : [...slots[i].days, day].sort((a, b) => a - b);
    slots = slots.map((s, n) => (n === i ? { ...s, days } : s));
  }
  function patch(i: number, change: Partial<HeatingSlot>) {
    slots = slots.map((s, n) => (n === i ? { ...s, ...change } : s));
  }
  // Chronological within the week — the order the day is actually lived.
  const ordered = $derived(
    slots.map((s, i) => ({ s, i })).sort((a, b) => a.s.at.localeCompare(b.s.at)),
  );
</script>

<Field label={t("heating.schedule")}
  hint={slots.length ? undefined : (inheritLabel ?? t("heating.scheduleEmpty"))}>
  <div class="space-y-2">
    {#each ordered as { s, i } (i)}
      <div class="flex flex-wrap items-center gap-1.5 rounded border border-dida-border bg-dida-panel-2 p-2">
        <input type="time" value={s.at} onchange={(e) => patch(i, { at: e.currentTarget.value })}
          class="tabular-nums" />
        <select value={s.profile} onchange={(e) => patch(i, { profile: e.currentTarget.value })}
         >
          {#each SCHEDULE_PROFILES as p (p)}
            <option value={p}>{t(`heating.profile.${p}` as MessageKey)}</option>
          {/each}
        </select>
        <div>
          <Picks
            many
            picks={DAYS.map((day) => ({ key: String(day), label: t(`day.short.${day}` as MessageKey) }))}
            chosen={s.days.map(String)}
            onpick={(k) => toggleDay(i, Number(k))}
          />
        </div>
        <Button size="small" onclick={() => remove(i)}>{t("common.delete")}</Button>
      </div>
    {/each}
    <Button size="small" onclick={add}>{t("heating.addSlot")}</Button>
  </div>
</Field>
