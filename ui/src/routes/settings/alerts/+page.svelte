<script lang="ts">
  import { onMount } from "svelte";
  import { api, type AlertHistoryRow, type AlertRecipients, type AlertRule } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { Card, Notice, SaveButton, Tag, formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { SECTION_TITLE_CLASS } from "$lib/ui";

  let rules = $state<AlertRule[]>([]);
  let history = $state<AlertHistoryRow[]>([]);
  let snapshot = $state("");
  let saving = $state(false);
  let saveMsg = $state<string | null>(null);
  let err = $state<string | null>(null);

  let targets = $state<AlertRecipients["targets"]>([]);
  let routes = $state<string[] | null>(null);
  let chosen = $state<string[]>([]);
  let chosenSnapshot = $state("");
  let savingRecipients = $state(false);
  let recipientsMsg = $state<string | null>(null);

  const snap = () => JSON.stringify(rules);
  const dirty = $derived(snapshot !== "" && snap() !== snapshot);
  const chosenKey = () => JSON.stringify([...chosen].sort());
  const recipientsDirty = $derived(chosenSnapshot !== "" && chosenKey() !== chosenSnapshot);
  const sortedTargets = $derived(
    [...targets].sort((a, b) => a.name.localeCompare(b.name, "hr", { sensitivity: "base" })),
  );

  function toggleRecipient(id: string, on: boolean) {
    chosen = on ? [...chosen, id] : chosen.filter((c) => c !== id);
  }

  async function saveRecipients() {
    savingRecipients = true;
    recipientsMsg = null;
    try {
      await api.setAlertRecipients(chosen);
      chosenSnapshot = chosenKey();
      recipientsMsg = t("alerts.recipientsSaved");
    } catch (e) {
      recipientsMsg = errMsg(e);
    } finally {
      savingRecipients = false;
    }
  }

  function ruleLabel(key: string): string {
    const k = `alerts.rule.${key}` as Parameters<typeof t>[0];
    const label = t(k);
    return label === k ? key : label;
  }

  async function load() {
    try {
      const [r, a, rc] = await Promise.all([api.alertRules(), api.systemAlerts(), api.alertRecipients()]);
      rules = r;
      history = a.history;
      snapshot = snap();
      targets = rc.targets;
      routes = rc.routes;
      chosen = rc.recipients;
      chosenSnapshot = chosenKey();
      err = null;
    } catch (e) {
      err = errMsg(e);
    }
  }

  onMount(load);

  async function save() {
    saving = true;
    saveMsg = null;
    try {
      const original: AlertRule[] = JSON.parse(snapshot);
      const byKey = new Map(original.map((o) => [o.key, o]));
      for (const rule of rules) {
        const o = byKey.get(rule.key);
        if (!o) continue;
        if (o.threshold === rule.threshold && o.hold_s === rule.hold_s && o.enabled === rule.enabled) continue;
        await api.updateAlertRule(rule.key, {
          threshold: rule.threshold,
          hold_s: rule.hold_s,
          enabled: rule.enabled,
        });
      }
      snapshot = snap();
      saveMsg = t("alerts.saved");
    } catch (e) {
      saveMsg = errMsg(e);
    } finally {
      saving = false;
    }
  }

  function fmtTs(iso: string): string {
    return dateTime(iso) || iso;
  }
</script>

<svelte:head><title>{t("alerts.title")} · DIDA</title></svelte:head>

{#if err}
  <Notice tone="err">{err}</Notice>
{/if}

<Card>
  <div class="hidden grid-cols-[1fr_auto_auto_auto] gap-3 px-1 text-s uppercase tracking-wide text-dida-text-muted sm:grid">
    <span></span>
    <span class="w-24 text-right">{t("alerts.threshold")}</span>
    <span class="w-20 text-right">{t("alerts.hold")}</span>
    <span class="w-16 text-right">{t("alerts.enabled")}</span>
  </div>
  {#each rules as rule (rule.key)}
    <div class="grid grid-cols-[1fr_auto_auto_auto] items-center gap-3 border-t border-dida-border pt-3 first:border-0 first:pt-0">
      <div class="flex min-w-0 flex-col">
        <span class="truncate text-m font-medium">{ruleLabel(rule.key)}</span>
        <span class="mt-0.5"><Tag tone={rule.severity === "critical" ? "err" : "warn"}>
          {rule.severity === "critical" ? t("alerts.critical") : t("alerts.warning")}
        </Tag></span>
      </div>
      <div class="w-24 text-right">
        {#if rule.threshold !== null}
          <input
            type="number"
            min="0"
            class="w-24 text-right tabular-nums"
            bind:value={rule.threshold}
          />
        {:else}
          <span class="text-m text-dida-text-faint">{t("alerts.noThreshold")}</span>
        {/if}
      </div>
      <input
        type="number"
        min="0"
        class="w-20 text-right tabular-nums"
        bind:value={rule.hold_s}
      />
      <label class="flex w-16 justify-end">
        <input type="checkbox" class="size-4 accent-dida-accent" bind:checked={rule.enabled} />
      </label>
    </div>
  {/each}

  <div class="flex items-center gap-3 pt-1">
    <SaveButton size="small" {dirty} {saving} onclick={save} />
    {#if saveMsg}<span class="text-s text-dida-text-muted">{saveMsg}</span>{/if}
  </div>
</Card>

<h2 class={SECTION_TITLE_CLASS}>{t("alerts.recipients")}</h2>
<Card>
  {#if routes === null}
    <p class="text-s text-dida-danger">{t("alerts.notifyDown")}</p>
  {/if}
  {#each sortedTargets as target (target.entity_id)}
    <label class="flex items-center gap-3 text-m">
      <input
        type="checkbox"
        class="size-4 accent-dida-accent"
        checked={chosen.includes(target.entity_id)}
        onchange={(e) => toggleRecipient(target.entity_id, e.currentTarget.checked)}
      />
      <span class="min-w-0 flex-1 truncate">{target.name}</span>
      {#if routes !== null && !routes.includes(target.entity_id)}
        <span class="shrink-0 text-s text-dida-text-faint">{t("alerts.noDevice")}</span>
      {/if}
    </label>
  {:else}
    <p class="text-m text-dida-text-muted">{t("alerts.noTargets")}</p>
  {/each}
  <div class="flex items-center gap-3 pt-1">
    <SaveButton size="small" dirty={recipientsDirty} saving={savingRecipients} onclick={saveRecipients} />
    {#if recipientsMsg}<span class="text-s text-dida-text-muted">{recipientsMsg}</span>{/if}
  </div>
</Card>

{#if history.length}
  <h2 class={SECTION_TITLE_CLASS}>{t("alerts.history")}</h2>
  <Card>
    <div class="divide-y divide-dida-border">
      {#each history as h (h.ts + h.key + h.scope + h.event)}
        <div class="flex items-center gap-3 py-2 text-m first:pt-0 last:pb-0">
          <Tag tone={h.event === "resolved" ? "ok" : h.severity === "critical" ? "err" : "warn"}>
            {h.event === "resolved" ? t("alerts.resolved") : t("alerts.fired")}
          </Tag>
          <span class="min-w-0 flex-1 truncate">{h.message}</span>
          <span class="shrink-0 text-s tabular-nums text-dida-text-muted">{fmtTs(h.ts)}</span>
        </div>
      {/each}
    </div>
  </Card>
{/if}
