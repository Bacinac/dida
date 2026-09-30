<script lang="ts">
  // Settings → Data → Commands. Admin-only (guarded by the layout nav): the
  // command audit trail — who told which entity to do what (user / automation /
  // assistant / Matter). States say what happened; this says who asked for it.
  import { onMount } from "svelte";
  import { Button, Card, Tag } from "$lib/kit";
  import { api, type CommandLogEntry } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";

  const LIMIT = 500;
  const WINDOWS = [
    { hours: 1, key: "cmdlog.h1" as const },
    { hours: 6, key: "cmdlog.h6" as const },
    { hours: 24, key: "cmdlog.h24" as const },
    { hours: 72, key: "cmdlog.h72" as const },
    { hours: 168, key: "cmdlog.h168" as const },
  ];

  let rows = $state<CommandLogEntry[]>([]);
  let entityId = $state("");
  let source = $state("");
  let hours = $state(24);
  let loading = $state(false);
  let err = $state<string | null>(null);

  async function load() {
    loading = true;
    err = null;
    try {
      const r = await api.commandHistory({
        entityId: entityId.trim() || undefined,
        source: source.trim() || undefined,
        hours,
        limit: LIMIT,
      });
      rows = r.commands;
    } catch (e) {
      err = errMsg(e);
    } finally {
      loading = false;
    }
  }

  const fmtTs = (ms: number) => dateTime(ms, true);

  // "user:alex" → chip-friendly [kind, rest]; keeps raw source visible in title.
  const srcKind = (s: string) => s.split(":", 1)[0];
  const srcRest = (s: string) => (s.includes(":") ? s.slice(s.indexOf(":") + 1) : "");
  const SOURCE_KINDS = new Set(["user", "automation", "assistant", "matter", "heating"]);

  onMount(load);
</script>

<svelte:head><title>{t("nav.commands")}</title></svelte:head>

<p class="mb-3 text-m text-dida-text-faint">{t("cmdlog.help")}</p>

<Card>
  <div class="mb-3 flex flex-wrap items-end gap-2">
    <input class="w-full min-w-[14rem] flex-1" placeholder={t("cmdlog.filterEntity")}
           bind:value={entityId} onkeydown={(e) => e.key === "Enter" && load()} />
    <input class="w-44" placeholder={t("cmdlog.filterSource")}
           bind:value={source} onkeydown={(e) => e.key === "Enter" && load()} />
    <label class="flex items-center gap-1.5 text-m text-dida-text-faint">
      {t("cmdlog.window")}
      <select class="w-full" bind:value={hours} onchange={load}>
        {#each WINDOWS as w (w.hours)}<option value={w.hours}>{t(w.key)}</option>{/each}
      </select>
    </label>
    <Button disabled={loading} onclick={load}>{t("cmdlog.refresh")}</Button>
  </div>

  {#if err}
    <p class="text-m text-dida-danger">{err}</p>
  {:else if !rows.length && !loading}
    <p class="text-m text-dida-text-faint">{t("cmdlog.empty")}</p>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-m">
        <thead>
          <tr class="border-b border-dida-border text-left text-s text-dida-text-faint">
            <th class="py-1.5 pr-3 font-normal">{t("cmdlog.time")}</th>
            <th class="py-1.5 pr-3 font-normal">{t("cmdlog.source")}</th>
            <th class="py-1.5 pr-3 font-normal">{t("cmdlog.entity")}</th>
            <th class="py-1.5 pr-3 font-normal">{t("cmdlog.commandCol")}</th>
            <th class="py-1.5 font-normal">{t("cmdlog.args")}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as r (r.ts + r.entity_id + r.command + r.source)}
            <tr class="border-b border-dida-border/40 align-top">
              <td class="whitespace-nowrap py-1.5 pr-3 tabular-nums text-dida-text-faint">{fmtTs(r.ts)}</td>
              <td class="py-1.5 pr-3">
                <Tag tone="quiet" kind={SOURCE_KINDS.has(srcKind(r.source)) ? srcKind(r.source) : undefined} title={r.source}>
                  {srcKind(r.source)}{#if srcRest(r.source)}<span class="text-dida-text opacity-80">{srcRest(r.source)}</span>{/if}
                </Tag>
              </td>
              <td class="py-1.5 pr-3 font-mono text-s">{r.entity_id}</td>
              <td class="whitespace-nowrap py-1.5 pr-3 font-mono text-s">{r.capability} / {r.command}</td>
              <td class="max-w-[18rem] truncate py-1.5 font-mono text-s text-dida-text-faint" title={r.args}>{r.args}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if rows.length >= LIMIT}
      <p class="mt-2 text-s text-dida-text-faint">{t("cmdlog.truncated", { n: LIMIT })}</p>
    {/if}
  {/if}
</Card>
