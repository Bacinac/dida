<script lang="ts">
  // The address book itself, one row per person, every field editable.
  //
  // It began as a list of proposed changes, which meant somebody whose name was
  // already right never appeared — and there was nowhere to give them a birth
  // date. So the page is the book, and a suggestion is a value filled in ahead of
  // time: accepting it is doing nothing, rejecting it is typing over it.
  //
  // Only what DIFFERS from the book is sent. A row nobody touched costs nothing.
  import { onMount } from "svelte";
  import { Button, Card, Notice, Picks, Tag, dialog } from "$lib/kit";
  import { api } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";

  type Loaded = Awaited<ReturnType<typeof api.upkeepProposals>>;
  type Row = Loaded["people"][number];
  type Edit = { given: string; family: string; born: string; label: string; remove: boolean };

  const LABELS = ["Work", "Private"] as const;

  let survey = $state<Awaited<ReturnType<typeof api.upkeepSurvey>> | null>(null);
  let people = $state<Row[]>([]);
  let duplicates = $state<Loaded["duplicates"]>([]);
  let assistant = $state(true);
  let suggested = $state(0);
  // What each row says now, and what Google holds. "Changed" is the difference
  // between the two — and because a suggestion is seeded into the fields but not
  // into the baseline, accepting one counts as a change and gets written.
  let edit = $state<Record<string, Edit>>({});
  let base = $state<Record<string, Edit>>({});
  let filter = $state("");
  let only = $state<"all" | "suggested" | "noBirthday" | "changed">("all");
  let busy = $state<"" | "load" | "apply">("");
  let msg = $state("");
  let failures = $state<{ id: string; error: string }[]>([]);

  const flat = (s: string) =>
    s.normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[đĐ]/g, "d").toLowerCase();

  function differs(id: string): boolean {
    const a = edit[id], b = base[id];
    if (!a || !b) return false;
    return (
      a.remove || a.given !== b.given || a.family !== b.family ||
      a.born !== b.born || a.label !== b.label
    );
  }

  const changed = $derived(people.filter((p) => differs(p.id)));
  const shown = $derived(
    people.filter((p) => {
      if (filter.trim() && !flat(`${p.name} ${p.context}`).includes(flat(filter))) return false;
      if (only === "suggested") return !!p.suggest;
      if (only === "noBirthday") return !edit[p.id]?.born;
      if (only === "changed") return differs(p.id);
      return true;
    }),
  );

  async function loadSurvey() {
    try {
      survey = await api.upkeepSurvey();
    } catch (e) {
      msg = errMsg(e);
    }
  }

  async function load() {
    busy = "load";
    msg = "";
    failures = [];
    try {
      const r = await api.upkeepProposals();
      people = r.people;
      duplicates = r.duplicates;
      assistant = r.assistant;
      suggested = r.suggested;
      const now: Record<string, Edit> = {};
      const was: Record<string, Edit> = {};
      for (const p of r.people) {
        const label = p.labels.find((l) => (LABELS as readonly string[]).includes(l)) ?? "";
        // The fields start on the suggestion where there is one; the baseline is
        // what the book actually holds, which is the name still shown in `name`.
        now[p.id] = { given: p.given, family: p.family, born: p.born, label, remove: false };
        was[p.id] = p.suggest
          ? { given: bookGiven(p), family: bookFamily(p), born: p.born, label, remove: false }
          : { ...now[p.id] };
      }
      edit = now;
      base = was;
    } catch (e) {
      msg = errMsg(e);
    } finally {
      busy = "";
    }
  }

  // What Google holds for the name. `name` is the display name as read, while the
  // editable fields may already carry a suggestion.
  const bookGiven = (p: Row) => {
    const parts = p.name.split(" ");
    return parts.length > 1 ? parts.slice(0, -1).join(" ") : p.name;
  };
  const bookFamily = (p: Row) => {
    const parts = p.name.split(" ");
    return parts.length > 1 ? parts[parts.length - 1] : "";
  };

  function setLabel(id: string, l: string) {
    edit[id] = { ...edit[id], label: edit[id].label === l ? "" : l };
  }
  function setRemove(id: string) {
    edit[id] = { ...edit[id], remove: !edit[id].remove };
  }
  function revert(id: string) {
    edit[id] = { ...base[id] };
  }

  async function apply() {
    const items = changed.map((p) => ({
      id: p.id,
      given: edit[p.id].given,
      family: edit[p.id].family,
      born: edit[p.id].born,
      target: edit[p.id].label,
      remove: edit[p.id].remove,
    }));
    const removals = items.filter((i) => i.remove).length;
    if (
      !(await dialog.confirm({
        title: t("upkeep.confirmTitle"),
        message: removals
          ? t("upkeep.confirmWithRemovals", { count: items.length, removals })
          : t("upkeep.confirmBody", { count: items.length }),
        confirmLabel: t("upkeep.apply"),
        danger: removals > 0,
      }))
    )
      return;
    busy = "apply";
    msg = "";
    try {
      const r = await api.upkeepApply(items);
      failures = r.failed;
      msg = t("upkeep.applied", { count: r.applied });
      await load();
      await loadSurvey();
    } catch (e) {
      msg = errMsg(e);
    } finally {
      busy = "";
    }
  }

  onMount(loadSurvey);
</script>

<Card>
  <p class="text-m text-dida-text-muted">{t("upkeep.intro")}</p>

  {#if survey}
    <div class="mt-4 grid grid-cols-2 gap-x-8 gap-y-1 text-m sm:grid-cols-3">
      <div class="flex justify-between"><span class="text-dida-text-faint">{t("upkeep.contacts")}</span><span>{survey.contacts}</span></div>
      <div class="flex justify-between"><span class="text-dida-text-faint">{t("upkeep.noDiacritics")}</span><span>{survey.without_diacritics}</span></div>
      <div class="flex justify-between"><span class="text-dida-text-faint">{t("upkeep.noBirthday")}</span><span>{survey.without_birthday}</span></div>
      <div class="flex justify-between"><span class="text-dida-text-faint">{t("upkeep.unlabelled")}</span><span>{survey.unlabelled}</span></div>
      <div class="flex justify-between"><span class="text-dida-text-faint">{t("upkeep.duplicateCount")}</span><span>{survey.duplicates}</span></div>
      {#each Object.entries(survey.labels) as [name, n] (name)}
        <div class="flex justify-between"><span class="text-dida-text-faint">{name}</span><span>{n}</span></div>
      {/each}
    </div>
  {/if}

  <div class="mt-5 flex flex-wrap items-center gap-3">
    <Button tone="primary" onclick={load} disabled={busy !== ""}>
      {busy === "load" ? t("upkeep.proposing") : people.length ? t("upkeep.reload") : t("upkeep.propose")}
    </Button>
    {#if people.length}
      <Button onclick={apply} disabled={busy !== "" || changed.length === 0}>
        {busy === "apply" ? t("upkeep.applying") : t("upkeep.applyN", { count: changed.length })}
      </Button>
    {/if}
    {#if msg}<span class="text-s text-dida-text-muted">{msg}</span>{/if}
  </div>

  {#if busy === "load"}
    <p class="mt-3 text-s text-dida-text-faint">{t("upkeep.proposingHint")}</p>
  {/if}
  {#if people.length && !assistant}
    <p class="mt-3 text-s text-dida-warn">{t("upkeep.noAssistant")}</p>
  {/if}
  {#if failures.length}
    <div class="mt-3"><Notice tone="err">
      <p class="text-s">{t("upkeep.someFailed", { count: failures.length })}</p>
      {#each failures as f (f.id)}
        <p class="text-s text-dida-text-faint">{f.error}</p>
      {/each}
    </Notice></div>
  {/if}
</Card>

{#if people.length}
  <Card>
    <div class="flex flex-wrap items-center gap-3">
      <input bind:value={filter} placeholder={t("contacts.search")} class="w-56" />
      <Picks picks={[
        { key: "all", label: `${t("upkeep.only.all")} ${people.length}` },
        { key: "suggested", label: `${t("upkeep.only.suggested")} ${suggested}` },
        { key: "noBirthday", label: t("upkeep.only.noBirthday") },
        { key: "changed", label: `${t("upkeep.only.changed")} ${changed.length}` },
      ]} chosen={[only]} onpick={(k) => (only = k as typeof only)} />
      <span class="ml-auto text-s text-dida-text-faint">{shown.length}</span>
    </div>

    <div class="mt-3 flex flex-col gap-1">
      {#each shown as p (p.id)}
        <div class="rounded border px-3 py-2 transition {edit[p.id]?.remove
          ? 'border-dida-danger/50 bg-dida-danger/5'
          : differs(p.id)
            ? 'border-dida-accent/50 bg-dida-panel-2'
            : 'border-dida-border bg-dida-panel-2'}">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span class="text-m font-medium {edit[p.id]?.remove ? 'text-dida-danger line-through' : ''}">{p.name}</span>
            {#if p.context}<span class="font-mono text-s text-dida-text-faint">{p.context}</span>{/if}
            {#if p.extra}
              <!-- Kept on write and shown here: hiding a middle name is how one
                   would quietly disappear during a spelling correction. -->
              <Tag tone="quiet">{p.extra}</Tag>
            {/if}
            {#if p.suggest && !p.suggest.sure}
              <Tag tone="warn">{t("upkeep.unsure")}</Tag>
            {/if}
            {#if p.born_raw}
              <!-- A birthday with no year: something to complete, not a date. -->
              <Tag tone="warn">{p.born_raw}</Tag>
            {/if}
            <span class="ml-auto flex shrink-0 items-center gap-1">
              {#if differs(p.id)}
                <Button size="small" tone="quiet" onclick={() => revert(p.id)}>{t("upkeep.revert")}</Button>
              {/if}
              {#each LABELS as l (l)}
                <Button size="small" selected={edit[p.id]?.label === l} onclick={() => setLabel(p.id, l)}>{l}</Button>
              {/each}
              <Button size="small" tone="danger" selected={!!edit[p.id]?.remove} onclick={() => setRemove(p.id)}>{t("upkeep.delete")}</Button>
            </span>
          </div>

          {#if !edit[p.id]?.remove}
            <div class="mt-1 flex flex-wrap items-center gap-2">
              <input bind:value={edit[p.id].given} aria-label={t("upkeep.given")}
                placeholder={t("upkeep.given")} class="w-36" />
              <input bind:value={edit[p.id].family} aria-label={t("upkeep.family")}
                placeholder={t("upkeep.family")} class="w-44" />
              <input type="date" bind:value={edit[p.id].born} aria-label={t("upkeep.born")}
                title={t("upkeep.born")} class="w-40" />
            </div>
            {#if p.suggest?.why}
              <p class="mt-1 text-s text-dida-text-faint">{p.suggest.why}</p>
            {/if}
          {/if}
        </div>
      {/each}
    </div>
  </Card>

  {#if duplicates.length}
    <Card>
      <h3 class="text-m font-medium">{t("upkeep.kind.duplicate")}</h3>
      <p class="mt-1 text-s text-dida-text-faint">{t("upkeep.why.duplicate")}</p>
      <div class="mt-3 flex flex-col gap-1">
        {#each duplicates as d (d.id)}
          <div class="flex items-start gap-3 rounded border border-dida-border bg-dida-panel-2 px-3 py-2">
            <div class="min-w-0 flex-1">
              <div class="text-m">{d.who}</div>
              <p class="mt-0.5 text-s text-dida-text-faint">{d.detail}</p>
            </div>
            <a href="https://contacts.google.com" target="_blank" rel="noreferrer"
              class="shrink-0 text-s text-dida-accent hover:underline">{t("upkeep.openGoogle")}</a>
          </div>
        {/each}
      </div>
    </Card>
  {/if}
{/if}
