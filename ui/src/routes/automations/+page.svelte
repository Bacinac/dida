<script lang="ts">
  import { onMount } from "svelte";
  import { api, type Automation, type AutomationDef, type AutomationRun, type Condition, type Scalar, type Trigger } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { capBadge, capLabel, capMeta, capRank, cmdLabel, commandsFor, entityType, isActuator, typeLabel, valueKind, optionLabel } from "$lib/capabilities";
  import { Button, Notice, PageHead, Picks, SaveButton, Toggle, dialog, formatNumber } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { SECTION_TITLE_CLASS, SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import EntityPicker, { pickerItem, type PickerItem } from "$lib/EntityPicker.svelte";

  const OPERATORS = ["==", "!=", "<", "<=", ">", ">="];

  type FLeaf = { entity_id: string; capability: string; op: string; value: string };
  // A condition row: a LEAF (kind "") uses the leaf fields; a GROUP (or/not) uses
  // `sub` (a list of leaves). The top-level list is AND-ed; one level of nesting.
  type FCond = FLeaf & { kind: "" | "or" | "not"; sub: FLeaf[] };
  type FAction = { entity_id: string; capability: string; command: string; value: string; delay: string };
  type FTrigger = { entity_id: string; capability: string; to: string; for_seconds: string; id: string };
  interface Form {
    id: number | null;
    name: string;
    enabled: boolean;
    mode: "typed" | "starlark";
    triggers: FTrigger[];   // one or more; any fires the rule
    conditions: FCond[];
    actions: FAction[];
    script: string;
    cooldown: string;
  }
  const blankTrigger = (entityId: string): FTrigger =>
    ({ entity_id: entityId, capability: capsOf(entityId)[0] ?? "", to: "", for_seconds: "", id: "" });
  const blankLeaf = (entityId: string): FLeaf =>
    ({ entity_id: entityId, capability: capsOf(entityId)[0] ?? "", op: "==", value: "" });

  const STARLARK_HELP =
    "# Pokreće se na okidač. Dostupno:\n" +
    "#   event            — {entity_id, capability, value, trigger_id}\n" +
    "#   state(eid, cap)  — trenutna vrijednost ili None\n" +
    "#   command(eid, cap, cmd, value=None)   value može biti dict (multi-arg)\n" +
    "#   turn_on(eid)  turn_off(eid)  toggle(eid)  set_color(eid, \"#RRGGBB\")\n" +
    "#   notify(cilj, naslov, poruka)   set_brightness(eid, 0..100)\n" +
    "# Više okidača? Granaj po event[\"trigger_id\"]. Bez I/O, bez while.\n\n" +
    'if event["trigger_id"] == "on":\n    turn_on("esphome:...")\nelif event["trigger_id"] == "off":\n    turn_off("esphome:...")\n';

  let items = $state<Automation[]>([]);
  let loadErr = $state<string | null>(null);
  // List filters: by name (also matches referenced device names) + by device type.
  let filterName = $state("");
  let filterType = $state("");
  // Run history (append-only log of every firing / error across all rules).
  let showRuns = $state(false);
  let runs = $state<AutomationRun[]>([]);
  const RUN_BADGE: Record<string, [string, MessageKey]> = {
    fired: ["bg-dida-ok/15 text-dida-ok", "auto.runFired"],
    error: ["bg-dida-danger/15 text-dida-danger", "auto.runError"],
    stale: ["bg-dida-warn/15 text-dida-warn", "auto.runStale"],
    held: ["bg-dida-accent/15 text-dida-accent", "auto.runHeld"],
  };
  let runsErr = $state<string | null>(null);
  async function toggleRuns() {
    showRuns = !showRuns;
    if (showRuns) {
      try { runs = await api.automationRuns(); runsErr = null; }
      catch (e) { runsErr = errMsg(e); }
    }
  }
  let form = $state<Form | null>(null);
  let saveErr = $state<string | null>(null);
  let busy = $state(false);
  // The editor's state as last saved; null for an AI draft, which nothing has saved yet.
  let formSaved = $state<string | null>(null);
  const formDirty = $derived(form !== null && (formSaved === null || JSON.stringify(form) !== formSaved));
  // Starlark pre-flight check (compile + dry-run) result.
  let checking = $state(false);
  let checkResult = $state<{ ok: boolean; error?: string; commands?: string[] } | null>(null);
  // Notification targets (notify:* entities) — write-only, never in /state, so
  // the builder gets their directory from the API instead of the device store.
  let notifyTargets = $state<{ entity_id: string; label: string }[]>([]);

  async function refresh() {
    try {
      // The server omits empty conditions/actions arrays (msgspec omit_defaults),
      // so a rule with no conditions arrives without the key. Normalize them to
      // arrays — otherwise reads like `definition.conditions.length` throw and
      // crash the whole list render (leaving it stuck on the empty state).
      const raw = await api.listAutomations();
      items = raw.map((a) => ({
        ...a,
        definition: {
          ...a.definition,
          conditions: a.definition.conditions ?? [],
          actions: a.definition.actions ?? [],
        },
      }));
      loadErr = null;
    } catch (e) {
      loadErr = errMsg(e);
    }
    try {
      notifyTargets = (await api.notifyTargets()).map((tg) => ({
        entity_id: tg.entity_id,
        label: tg.entity_id === "notify:all" ? t("auto.notifyAll") : tg.label,
      }));
    } catch {
      notifyTargets = []; // no targets ≠ a broken rule list
    }
  }
  onMount(() => {
    refresh();
    // Deep-link from a helper: ?q=<name> pre-filters the list to the automations
    // that helper drives (filterName also matches referenced device names).
    const q = new URLSearchParams(location.search).get("q");
    if (q) filterName = q;
  });

  // Self-heal: the list is fetched once on mount, so an API bounce (e.g. a
  // redeploy) would otherwise leave a stale/empty list until you re-navigate.
  // Re-fetch whenever the live connection comes back.
  let lastConn = devices.conn;
  $effect(() => {
    const c = devices.conn;
    if (c === "live" && lastConn !== "live") refresh();
    lastConn = c;
  });

  // --- option sources from the live device list ---
  const entityIds = $derived(devices.list.map((d) => d.entityId));
  function capsOf(entityId: string): string[] {
    const d = devices.byId[entityId];
    return d ? Object.keys(d.caps).sort((a, b) => capRank(a) - capRank(b)) : [];
  }
  function actuatorCapsOf(entityId: string): string[] {
    return capsOf(entityId).filter(isActuator);
  }
  // Action targets = device actuators + notify targets (stateless, store-less).
  function actionCapsOf(entityId: string): string[] {
    return entityId.startsWith("notify:") ? ["notify"] : actuatorCapsOf(entityId);
  }

  // Friendly labels: dropdowns + summaries show device names grouped by room,
  // not raw entity ids like "esphome:gate-switches-kitchen-light:gate".
  function entLabel(entityId: string): string {
    if (entityId.startsWith("notify:")) {
      return notifyTargets.find((tg) => tg.entity_id === entityId)?.label ?? entityId;
    }
    return devices.byId[entityId]?.name ?? entityId;
  }
  // Candidate lists for the searchable EntityPicker (area/type filters + search).
  // Actions target only actuators (+notify); triggers/conditions observe entities
  // that actually CARRY a state — never command-only ones (announce/notify = "event",
  // a remote key = "press"), which have nothing to fire on.
  const OBSERVABLE = (entityId: string): boolean =>
    capsOf(entityId).some((c) => capMeta(c).control !== "event" && capMeta(c).control !== "press");
  // Trigger/condition candidates: only entities with an observable state.
  const triggerItems = $derived(devices.list.filter((d) => OBSERVABLE(d.entityId)).map(pickerItem));
  const actionItems = $derived([
    ...devices.list.filter((d) => actuatorCapsOf(d.entityId).length > 0).map(pickerItem),
    ...notifyTargets.map((tg): PickerItem => ({
      id: tg.entity_id, name: tg.label, area: t("auto.notifyGroup"), type: "notify", category: "control",
    })),
  ]);
  function fmtDelay(ms: number): string {
    return ms >= 1000 ? `${formatNumber(ms / 1000, { maximumFractionDigits: 1 })}s` : `${ms}ms`;
  }
  // Options for an "option"-valued action, read from the entity's <cap>_options
  // list (effect_options, enum_options, source_options, hvac_mode_options, fan_mode_options,
  // vacuum_mode_options).
  function actionOptions(entityId: string, capability: string): string[] {
    const oc = ["effect", "enum", "source", "hvac_mode", "fan_mode", "vacuum_mode"].includes(capability)
      ? `${capability}_options` : null;
    const v = oc ? devices.byId[entityId]?.caps[oc]?.value : undefined;
    if (typeof v !== "string") return [];
    try { const a = JSON.parse(v); return Array.isArray(a) ? a.map(String) : []; } catch { return []; }
  }
  function actionValueKind(a: FAction): string | undefined {
    return commandsFor(a.capability).find((o) => o.command === a.command)?.value;
  }

  function blankForm(): Form {
    const first = entityIds[0] ?? "";
    return {
      id: null, name: "", enabled: true, mode: "typed",
      triggers: [blankTrigger(first)],
      conditions: [],
      actions: [],
      script: STARLARK_HELP,
      cooldown: "",
    };
  }

  // Map a stored/AI-drafted definition into the editable form. Shared by Edit
  // and the AI draft so both round-trip the same fields (incl. for_seconds/delay).
  function formFromDef(id: number | null, name: string, enabled: boolean, d: AutomationDef): Form {
    const isStarlark = !!(d.script && d.script.trim());
    return {
      id, name, enabled,
      mode: isStarlark ? "starlark" : "typed",
      cooldown: d.cooldown_seconds ? String(d.cooldown_seconds) : "",
      triggers: (d.triggers ?? []).map((tr) => ({
        entity_id: tr.entity_id,
        capability: tr.capability,
        to: tr.to === undefined || tr.to === null ? "" : String(tr.to),
        for_seconds: tr.for_seconds ? String(tr.for_seconds) : "",
        id: tr.id ?? "",
      })),
      conditions: (d.conditions ?? []).map((c): FCond => {
        const leaf = (l: Condition): FLeaf => ({ entity_id: l.entity_id, capability: l.capability, op: l.op, value: String(l.value) });
        if (c.kind === "or" || c.kind === "not") {
          return { ...blankLeaf(""), kind: c.kind, sub: (c.conditions ?? []).map(leaf) };
        }
        return { ...leaf(c), kind: "", sub: [] };
      }),
      actions: (d.actions ?? []).map((ac) => {
        // The single-value form field maps to the command's arg key (value /
        // text / message) — read whichever the stored rule carries, so older
        // {value}-shaped say/notify actions round-trip too.
        const av = ac.args?.value ?? ac.args?.text ?? ac.args?.message;
        return {
          entity_id: ac.entity_id, capability: ac.capability, command: ac.command,
          value: av !== undefined ? String(av) : "",
          delay: ac.delay_ms ? String(ac.delay_ms) : "",
        };
      }),
      script: d.script ?? STARLARK_HELP,
    };
  }

  function startCreate() {
    saveErr = null;
    form = blankForm();
    formSaved = JSON.stringify(form);
  }

  async function startEdit(a: Automation) {
    saveErr = null;
    // The editor handles ONE level of grouping (AND-list of leaves + OR/NOT groups
    // of leaves). A group nested inside a group is beyond it — refuse rather than
    // silently flatten (and lose) it.
    const tooDeep = (a.definition.conditions ?? []).some((c) =>
      (c.kind === "or" || c.kind === "not") && (c.conditions ?? []).some((sc) => !!(sc.kind && sc.kind.trim())));
    if (tooDeep) {
      await dialog.alert({ title: t("auto.groupCondTitle"), message: t("auto.groupCondBody") });
      return;
    }
    form = formFromDef(a.id, a.name, a.enabled, a.definition);
    formSaved = JSON.stringify(form);
  }

  // --- AI draft: describe in plain language → Claude synthesises a validated
  // definition (NOT saved) → opens in the editor for review before you save.
  let aiText = $state("");
  let aiBusy = $state(false);
  let aiErr = $state<string | null>(null);
  let aiMode = $state<"typed" | "starlark">("typed");
  const modeTabs = $derived([{ key: "typed", label: t("auto.typed") }, { key: "starlark", label: t("auto.starlark") }]);
  async function aiGenerate() {
    const desc = aiText.trim();
    if (!desc || aiBusy) return;
    aiBusy = true; aiErr = null;
    try {
      const { definition } = await api.synthesizeAutomation(desc, aiMode);
      // Suggest a short name from the description; the user can rename before save.
      const name = desc.length > 48 ? desc.slice(0, 47).trimEnd() + "…" : desc;
      form = formFromDef(null, name, true, definition);
      formSaved = null;
      saveErr = null;
      aiText = "";
    } catch (e) {
      aiErr = errMsg(e);
    } finally {
      aiBusy = false;
    }
  }

  // Accordion: only one rule is expanded at a time; selecting one collapses the rest.
  let selectedId = $state<number | null>(null);

  // --- AI explain: plain-language description + "would it fire now / why not".
  let explaining = $state<Record<number, { loading: boolean; text?: string; err?: string }>>({});
  async function explain(a: Automation) {
    const cur = explaining[a.id];
    if (cur && (cur.text || cur.err)) { explaining = { ...explaining, [a.id]: { loading: false } }; return; } // toggle off
    explaining = { ...explaining, [a.id]: { loading: true } };
    try {
      const { explanation } = await api.explainAutomation(a.id);
      explaining = { ...explaining, [a.id]: { loading: false, text: explanation } };
    } catch (e) {
      explaining = { ...explaining, [a.id]: { loading: false, err: errMsg(e) } };
    }
  }

  // Force-run a rule's actions now. Drives real devices (running the gate rule
  // opens the gate), so confirm first; the actual firing happens in the engine.
  let running = $state<number | null>(null);
  async function runNow(a: Automation) {
    const ok = await dialog.confirm({
      title: t("ai.runConfirmTitle"),
      message: t("ai.runConfirmBody", { name: a.name }),
      confirmLabel: t("ai.run"),
    });
    if (!ok) return;
    running = a.id;
    try {
      await api.runAutomation(a.id);
    } catch (e) {
      await dialog.alert({ title: t("ai.run"), message: errMsg(e) });
    } finally {
      running = null;
    }
  }

  function conv(capability: string, raw: string): Scalar {
    const k = valueKind(capability);
    if (k === "bool") return raw === "true";
    if (k === "number") return Number(raw);
    return raw;
  }

  function buildDef(f: Form): AutomationDef {
    const triggers: Trigger[] = f.triggers.map((ft) => {
      const trig: Trigger = { entity_id: ft.entity_id, capability: ft.capability };
      if (ft.to !== "") trig.to = conv(ft.capability, ft.to);
      const hold = Number(ft.for_seconds);
      if (ft.for_seconds !== "" && hold > 0) trig.for_seconds = hold;
      if (ft.id.trim()) trig.id = ft.id.trim();
      return trig;
    });
    const cd = Number(f.cooldown);
    const cooldown = f.cooldown !== "" && cd > 0 ? cd : undefined;
    if (f.mode === "starlark") {
      return { triggers, conditions: [], actions: [], script: f.script, cooldown_seconds: cooldown };
    }
    return {
      triggers,
      cooldown_seconds: cooldown,
      conditions: f.conditions.map((c): Condition =>
        c.kind === ""
          ? { entity_id: c.entity_id, capability: c.capability, op: c.op, value: conv(c.capability, c.value) }
          : { entity_id: "", capability: "", op: "", value: "" as Scalar, kind: c.kind,
              conditions: c.sub.map((s) => ({ entity_id: s.entity_id, capability: s.capability, op: s.op, value: conv(s.capability, s.value) })) }
      ),
      actions: f.actions.map((a) => {
        const opt = commandsFor(a.capability).find((o) => o.command === a.command);
        const out: AutomationDef["actions"][number] = {
          entity_id: a.entity_id, capability: a.capability, command: a.command,
        };
        if (opt?.value) out.args = { [opt.argKey ?? "value"]: opt.value === "number" ? Number(a.value) : a.value };
        const delay = Number(a.delay);
        if (a.delay !== "" && delay > 0) out.delay_ms = delay; // preserve pulse timing
        return out;
      }),
    };
  }

  // Pre-flight a Starlark script (compile + dry-run + command validation) before
  // saving — turns "save and check the log" into "Check → green → Save".
  async function checkScript() {
    if (!form || checking) return;
    checking = true; checkResult = null;
    try {
      checkResult = await api.checkScript(form.script, form.triggers[0] && {
        entity_id: form.triggers[0].entity_id, capability: form.triggers[0].capability,
      });
    } catch (e) {
      checkResult = { ok: false, error: errMsg(e) };
    } finally {
      checking = false;
    }
  }

  async function save() {
    if (!form) return;
    const f = form;
    if (!f.name.trim()) { saveErr = t("auto.nameRequired"); return; }
    if (f.mode === "typed" && f.actions.length === 0) { saveErr = t("auto.addAtLeastOne"); return; }
    if (f.mode === "starlark" && !f.script.trim()) { saveErr = t("auto.scriptEmpty"); return; }
    busy = true; saveErr = null;
    try {
      const def = buildDef(f);
      if (f.id === null) await api.createAutomation(f.name.trim(), def, f.enabled);
      else await api.updateAutomation(f.id, f.name.trim(), def, f.enabled);
      form = null;
      await refresh();
    } catch (e) {
      saveErr = errMsg(e);
    } finally {
      busy = false;
    }
  }

  async function toggle(a: Automation) {
    try {
      await api.setAutomationEnabled(a.id, !a.enabled);
      await refresh();
    } catch (e) {
      loadErr = errMsg(e);
    }
  }

  async function remove(a: Automation) {
    const ok = await dialog.confirm({
      title: t("common.delete"),
      message: t("auto.confirmDelete", { name: a.name }),
      confirmLabel: t("common.delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteAutomation(a.id);
      await refresh();
    } catch (e) {
      loadErr = errMsg(e);
    }
  }

  function addCondition() {
    if (!form) return;
    form.conditions.push({ ...blankLeaf(entityIds[0] ?? ""), kind: "", sub: [] });
  }
  function addAction() {
    if (!form) return;
    const e = entityIds[0] ?? "";
    const cap = actuatorCapsOf(e)[0] ?? "";
    form.actions.push({ entity_id: e, capability: cap, command: commandsFor(cap)[0]?.command ?? "", value: "", delay: "" });
  }

  // value <select|input> helper: render a bool select vs free input
  function boolLabels(cap: string): [string, string] {
    return [capBadge(cap, true), capBadge(cap, false)];
  }

  function summary(a: Automation): string {
    const trigs = a.definition.triggers ?? [];
    const trg = trigs[0];
    if (!trg) return "—";
    const toTxt = trg.to === undefined || trg.to === null
      ? t("auto.sumChange")
      : typeof trg.to === "boolean"
        ? `= ${capBadge(trg.capability, trg.to)}`
        : `= ${trg.to}`;
    const holdTxt = trg.for_seconds ? ` ${fmtDelay(trg.for_seconds * 1000)}` : "";
    const more = trigs.length > 1 ? ` (+${trigs.length - 1})` : "";
    const head = `${t("auto.sumWhen")} ${entLabel(trg.entity_id)} · ${capLabel(trg.capability)} ${toTxt}${holdTxt}${more}`;
    if (a.definition.script && a.definition.script.trim()) return `${head} → ${t("auto.sumStarlark")}`;
    const acts = (a.definition.actions ?? [])
      .map((x) => `${x.delay_ms ? `+${fmtDelay(x.delay_ms)} ` : ""}${entLabel(x.entity_id)} · ${cmdLabel(x.command)}`)
      .join(", ");
    const condN = (a.definition.conditions ?? []).length;
    return `${head}${condN ? ` (${t("auto.sumCond", { n: condN })})` : ""} → ${acts}`;
  }

  // All entity_ids a rule references (trigger + actions + conditions) — for the
  // device filter and the name-search (which also matches device names).
  function autoEntityIds(a: Automation): string[] {
    const d = a.definition;
    const trigs = d.triggers ?? [];
    const out = [...trigs.map((x) => x.entity_id), ...(d.actions ?? []).map((x) => x.entity_id), ...(d.conditions ?? []).map((x) => x.entity_id)];
    return out.filter(Boolean) as string[];
  }
  // The device types a rule touches (from the devices it references).
  function autoTypes(a: Automation): Set<string> {
    const types = new Set<string>();
    for (const e of autoEntityIds(a)) {
      const d = devices.byId[e];
      if (d) types.add(entityType(d.deviceType, d.caps));
    }
    return types;
  }
  // Distinct device types appearing across all rules (alphabetical by label) for the dropdown.
  const typeOptions = $derived.by(() => {
    const types = new Set<string>();
    for (const a of items) for (const ty of autoTypes(a)) types.add(ty);
    return [...types].sort((x, y) => typeLabel(x).localeCompare(typeLabel(y), "hr"));
  });
  // Sorted-by-name + filtered list actually rendered.
  const shownItems = $derived.by(() => {
    const q = filterName.trim().toLowerCase();
    return items
      .filter((a) => {
        if (q && !(`${a.name} ${autoEntityIds(a).map(entLabel).join(" ")}`.toLowerCase().includes(q))) return false;
        if (filterType && !autoTypes(a).has(filterType)) return false;
        return true;
      })
      .sort((a, b) => a.name.localeCompare(b.name, "hr"));
  });


  // Names follow DOMAIN-Descriptor (LIGHT-Kitchen); the token before the first "-" is
  // the group. The name is the single source of truth — no separate category field.
  const domainOf = (n: string): string => { const i = n.indexOf("-"); return i > 0 ? n.slice(0, i) : n; };
  const descOf = (n: string): string => { const i = n.indexOf("-"); return i > 0 ? n.slice(i + 1) : ""; };
  // The guided create/edit form edits domain + descriptor separately and reassembles.
  function setName(domain: string, desc: string) {
    if (!form) return;
    const d = domain.trim();
    form.name = d && desc ? `${d}-${desc}` : d || desc;
  }
  // Existing domains, for the create-form datalist (pick one or type a new one).
  const domainOptions = $derived(
    [...new Set(items.map((a) => domainOf(a.name)).filter(Boolean))].sort((x, y) => x.localeCompare(y, "hr")),
  );
  // Rendered list grouped by domain (each group's items keep the name sort from shownItems).
  const groupedItems = $derived.by(() => {
    const groups = new Map<string, typeof shownItems>();
    for (const a of shownItems) {
      const d = domainOf(a.name) || "—";
      const g = groups.get(d) ?? (groups.set(d, []), groups.get(d)!);
      g.push(a);
    }
    return [...groups.entries()].sort((x, y) => x[0].localeCompare(y[0], "hr")).map(([domain, gi]) => ({ domain, items: gi }));
  });
</script>

<svelte:head><title>{t("nav.automations")}</title></svelte:head>

{#if !form}
  <PageHead sticky={false}>
    {#snippet aside()}
      <Button onclick={toggleRuns}>{t("auto.runs")}</Button>
      <Button tone="primary" onclick={startCreate}>{t("auto.new")}</Button>
    {/snippet}
  </PageHead>
{/if}

{#if showRuns && !form}
  <!-- Run history: every firing / error across all rules, newest first -->
  <div class="mb-5 rounded-lg border border-dida-border bg-dida-panel p-4">
    <div class="mb-2 flex items-center justify-between">
      <h2 class="{SECTION_TITLE_CLASS}">{t("auto.runsTitle")}</h2>
      <Button size="small" label={t("common.cancel")} title={t("common.cancel")} onclick={() => (showRuns = false)}>✕</Button>
    </div>
    {#if runsErr}
      <Notice tone="err">{runsErr}</Notice>
    {:else if runs.length === 0}
      <p class="text-s text-dida-text-faint">{t("auto.runsEmpty")}</p>
    {:else}
      <div class="max-h-96 overflow-y-auto">
        {#each runs as r (r.id)}
          <div class="flex items-start gap-2 border-b border-dida-border/40 py-1.5 text-s last:border-0">
            <span class="shrink-0 tabular-nums text-dida-text-faint">{dateTime(r.fired_at, true)}</span>
            <span class="shrink-0 rounded px-1.5 {RUN_BADGE[r.outcome][0]}">{t(RUN_BADGE[r.outcome][1])}</span>
            <span class="min-w-0 flex-1">
              <span class="font-medium">{r.name}</span>
              {#if r.outcome === "stale"}<span class="text-dida-text-faint"> — {t("auto.runStaleDetail", { n: formatNumber(Number(r.detail), { maximumFractionDigits: 1 }) })}</span>
              {:else if r.detail}<span class="whitespace-pre-wrap {r.outcome === 'error' ? 'text-dida-danger' : 'text-dida-text-faint'}"> — {r.detail}</span>{/if}
            </span>
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

{#if !form}
  <!-- AI draft: describe in plain language, Claude proposes a validated rule -->
  <div class="mb-5 rounded-lg border border-dida-accent/40 bg-dida-accent/5 p-4">
    <label for="aibox" class="block text-s font-semibold uppercase tracking-wide text-dida-accent">
      ✨ {t("ai.title")}
    </label>
    <div class="mt-2 flex flex-wrap items-start gap-2">
      <textarea
        id="aibox" bind:value={aiText}
        onkeydown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); aiGenerate(); } }}
        placeholder={t("ai.placeholder")}
        rows="2"
        class="min-w-0 flex-1 resize-y"
      ></textarea>
      <!-- target the draft: a typed Trigger→Condition→Action, or a Layer-2 Starlark script -->
      <Picks picks={modeTabs} chosen={[aiMode]} onpick={(k) => (aiMode = k as "typed" | "starlark")} />
      <Button tone="primary"
        onclick={aiGenerate} disabled={aiBusy || !aiText.trim()}
      >{aiBusy ? t("ai.generating") : t("ai.generate")}</Button>
    </div>
    {#if aiErr}
      <Notice tone="err">{aiErr}</Notice>
    {/if}
  </div>
{/if}

{#if loadErr}
  <Notice tone="err">{loadErr}</Notice>
{/if}

{#snippet formEditor()}
{#if form}
  <!-- create / edit rule editor. Rendered at the top for a NEW rule (next to the
       New / AI-draft controls), or inline right below the rule being edited — so
       the open editor is always in view, never lost off-screen at the top. -->
  <div class="mb-4 rounded-lg border border-dida-accent/60 bg-dida-panel p-4">
    <h2 class="mb-3 {SECTION_TITLE_CLASS}">
      {form.id === null ? t("auto.formNew") : t("auto.formEdit")}
    </h2>

    <div class="mb-1 grid grid-cols-[minmax(7rem,11rem)_1fr] gap-2">
      <div>
        <label class="block text-s text-dida-text-muted" for="adom">{t("auto.domain")}</label>
        <input id="adom" list="autodomains" value={domainOf(form.name)}
          oninput={(e) => setName((e.currentTarget as HTMLInputElement).value, descOf(form?.name ?? ""))}
          placeholder={t("auto.domainPh")} class="mt-1 w-full" />
        <datalist id="autodomains">{#each domainOptions as d (d)}<option value={d}></option>{/each}</datalist>
      </div>
      <div>
        <label class="block text-s text-dida-text-muted" for="anm">{t("auto.descriptor")}</label>
        <input id="anm" value={descOf(form.name)}
          oninput={(e) => setName(domainOf(form?.name ?? ""), (e.currentTarget as HTMLInputElement).value)}
          placeholder={t("auto.descriptorPh")} class="mt-1 w-full" />
      </div>
    </div>
    <p class="mb-3 text-s text-dida-text-faint">{t("auto.name")}: <span class="font-mono text-dida-text">{form.name || "—"}</span></p>

    <!-- MODE -->
    <div class="mb-3"><Picks picks={modeTabs} chosen={[form.mode]} onpick={(k) => form && (form.mode = k as "typed" | "starlark")} /></div>

    <!-- TRIGGERS (one or more — any fires the rule) -->
    <div class="mb-3 rounded border border-dida-border/60 p-3">
      <div class="mb-2 flex items-center justify-between">
        <p class="{SUBSECTION_TITLE_CLASS}">{t("auto.trigger")}</p>
        <Button size="small" onclick={() => form?.triggers.push(blankTrigger(triggerItems[0]?.id ?? ""))}>{t("auto.addTrigger")}</Button>
      </div>
      <div class="space-y-2">
        {#each form.triggers as tr, i (i)}
          <div class="grid grid-cols-[minmax(0,1fr)_8rem_8rem_auto_auto] items-center gap-2">
            <EntityPicker
              bind:value={tr.entity_id} items={triggerItems}
              onchange={(id) => { tr.capability = capsOf(id)[0] ?? ""; }}
            />
            <select bind:value={tr.capability} class="w-full">
              {#each capsOf(tr.entity_id) as c (c)}<option value={c}>{capLabel(c)}</option>{/each}
            </select>
            <div>
              {#if valueKind(tr.capability) === "bool"}
                <select bind:value={tr.to} class="w-full">
                  <option value="">{t("auto.anyChange")}</option>
                  <option value="true">{boolLabels(tr.capability)[0]}</option>
                  <option value="false">{boolLabels(tr.capability)[1]}</option>
                </select>
              {:else}
                <input bind:value={tr.to} class="w-full" placeholder={t("auto.valueOpt")} />
              {/if}
            </div>
            <label class="flex items-center gap-1 text-s text-dida-text-faint">
              {t("auto.hold")}
              <input bind:value={tr.for_seconds} type="number" min="0" step="1" class="w-14" placeholder="0" />
            </label>
            {#if form.triggers.length > 1}
              <Button size="small" label={t("common.delete")} title={t("common.delete")} onclick={() => form?.triggers.splice(i, 1)}>✕</Button>
            {:else}<span></span>{/if}
          </div>
          <!-- Starlark branches on this label via event["trigger_id"] -->
          {#if form.mode === "starlark" && form.triggers.length > 1}
            <input bind:value={tr.id} class="w-full text-s" placeholder={t("auto.triggerId")} />
          {/if}
        {/each}
      </div>
      <label class="mt-2 flex items-center gap-2 text-s text-dida-text-faint">
        {t("auto.cooldown")}
        <input bind:value={form.cooldown} type="number" min="0" step="1" class="w-20" placeholder="0" />
        <span class="text-dida-text-faint/80">{t("auto.cooldownHint")}</span>
      </label>
    </div>

    {#if form.mode === "typed"}
    <!-- CONDITIONS -->
    <div class="mb-3 rounded border border-dida-border/60 p-3">
      <div class="mb-2 flex items-center justify-between">
        <p class="{SUBSECTION_TITLE_CLASS}">{t("auto.conditions")}</p>
        <Button size="small" onclick={addCondition}>{t("auto.addCondition")}</Button>
      </div>
      {#snippet leafFields(l: FLeaf)}
        <EntityPicker bind:value={l.entity_id} items={triggerItems} onchange={(id) => { l.capability = capsOf(id)[0] ?? ""; }} />
        <select bind:value={l.capability} class="w-full">
          {#each capsOf(l.entity_id) as cc (cc)}<option value={cc}>{capLabel(cc)}</option>{/each}
        </select>
        <select bind:value={l.op} class="w-full">
          {#each OPERATORS as o (o)}<option value={o}>{o}</option>{/each}
        </select>
        <div>
          {#if valueKind(l.capability) === "bool"}
            <select bind:value={l.value} class="w-full">
              <option value="true">{boolLabels(l.capability)[0]}</option>
              <option value="false">{boolLabels(l.capability)[1]}</option>
            </select>
          {:else}
            <input bind:value={l.value} class="w-full" placeholder={t("auto.value")} />
          {/if}
        </div>
      {/snippet}
      {#each form.conditions as c, i (i)}
        <div class="mb-2 rounded border border-dida-border/40 p-2">
          <div class="mb-1.5 flex items-center gap-2">
            <select bind:value={c.kind} class="w-28"
              onchange={() => { if (c.kind !== "" && c.sub.length === 0) c.sub.push(blankLeaf(entityIds[0] ?? "")); }}>
              <option value="">{t("auto.condLeaf")}</option>
              <option value="or">{t("auto.condOr")}</option>
              <option value="not">{t("auto.condNot")}</option>
            </select>
            <span class="flex-1"></span>
            <Button size="small" label={t("common.remove")} title={t("common.remove")} onclick={() => form?.conditions.splice(i, 1)}>✕</Button>
          </div>
          {#if c.kind === ""}
            <div class="grid grid-cols-[minmax(0,1fr)_8.5rem_4.5rem_8.5rem] items-center gap-2">{@render leafFields(c)}</div>
          {:else}
            <div class="space-y-1.5 border-l-2 border-dida-border/40 pl-2">
              {#each c.sub as s, j (j)}
                <div class="grid grid-cols-[minmax(0,1fr)_8.5rem_4.5rem_8.5rem_auto] items-center gap-2">
                  {@render leafFields(s)}
                  <span class="justify-self-center"><Button size="small" label={t("common.remove")} title={t("common.remove")} onclick={() => c.sub.splice(j, 1)}>✕</Button></span>
                </div>
              {/each}
              <Button size="small" onclick={() => c.sub.push(blankLeaf(entityIds[0] ?? ""))}>+ {t("auto.condSub")}</Button>
            </div>
          {/if}
        </div>
      {:else}
        <p class="text-s text-dida-text-faint">{t("auto.noConditions")}</p>
      {/each}
    </div>

    <!-- ACTIONS -->
    <div class="mb-4 rounded border border-dida-border/60 p-3">
      <div class="mb-2 flex items-center justify-between">
        <p class="{SUBSECTION_TITLE_CLASS}">{t("auto.actions")}</p>
        <Button size="small" onclick={addAction}>{t("auto.addAction")}</Button>
      </div>
      {#each form.actions as a, i (i)}
        <div class="mb-2 grid grid-cols-[minmax(0,1fr)_8.5rem_8.5rem_8.5rem_auto_auto] items-center gap-2">
          <EntityPicker
            bind:value={a.entity_id} items={actionItems}
            onchange={(id) => {
              const caps = actionCapsOf(id);
              a.capability = caps[0] ?? "";
              a.command = commandsFor(a.capability)[0]?.command ?? "";
            }}
          />
          <select bind:value={a.capability} class="w-full">
            {#each actionCapsOf(a.entity_id) as cc (cc)}<option value={cc}>{capLabel(cc)}</option>{/each}
          </select>
          <select bind:value={a.command} class="w-full">
            {#each commandsFor(a.capability) as o (o.command)}<option value={o.command}>{cmdLabel(o.command)}</option>{/each}
          </select>
          <div>
            {#if actionValueKind(a) === "number"}
              <input bind:value={a.value} type="number" class="w-full" placeholder={t("auto.value")} />
            {:else if actionValueKind(a) === "color"}
              <input
                type="color" class="h-7 w-full cursor-pointer p-0.5"
                value={/^#[0-9a-fA-F]{6}$/.test(a.value) ? a.value : "#ffffff"}
                oninput={(e) => (a.value = e.currentTarget.value)}
              />
            {:else if actionValueKind(a) === "text"}
              <input bind:value={a.value} type="text" class="w-full" placeholder={t("auto.value")} />
            {:else if actionValueKind(a) === "option"}
              <select bind:value={a.value} class="w-full">
                {#each actionOptions(a.entity_id, a.capability) as o (o)}<option value={o}>{optionLabel(o)}</option>{/each}
              </select>
            {/if}
          </div>
          <label class="flex items-center gap-1 text-s text-dida-text-faint">
            {t("auto.afterMs")}
            <input bind:value={a.delay} type="number" min="0" step="100" class="w-16" placeholder="0" />
          </label>
          <span class="justify-self-center"><Button size="small" label={t("common.remove")} title={t("common.remove")} onclick={() => form && form.actions.splice(i, 1)}>✕</Button></span>
        </div>
      {:else}
        <p class="text-s text-dida-text-faint">{t("auto.addAtLeastOne")}</p>
      {/each}
    </div>
    {:else}
    <!-- STARLARK -->
    <div class="mb-4 rounded border border-dida-border/60 p-3">
      <p class="mb-2 {SUBSECTION_TITLE_CLASS}">{t("auto.starlarkScript")}</p>
      <textarea
        bind:value={form.script}
        oninput={() => (checkResult = null)}
        rows="12"
        spellcheck="false"
        class="w-full font-mono"
      ></textarea>
      <!-- Pre-flight: compile + dry-run in the sandbox, validate emitted commands -->
      <div class="mt-2 flex items-center gap-2">
        <Button size="small" onclick={checkScript} disabled={checking || !form.script.trim()}>
          {checking ? t("auto.checking") : t("auto.check")}
        </Button>
        {#if checkResult?.ok}<span class="text-s text-dida-ok">✓ {t("auto.checkOk")}</span>{/if}
      </div>
      {#if checkResult && !checkResult.ok}
        <Notice tone="err"><span class="whitespace-pre-wrap font-mono text-s">{checkResult.error}</span></Notice>
      {:else if checkResult?.ok && checkResult.commands?.length}
        <Notice>
          <div class="text-s text-dida-text-faint">{t("auto.checkWouldDrive")}</div>
          <ul class="font-mono text-s text-dida-text">
            {#each checkResult.commands as c (c)}<li>{c}</li>{/each}
          </ul>
        </Notice>
      {:else if checkResult?.ok}
        <p class="mt-2 text-s text-dida-text-faint">{t("auto.checkNoCmds")}</p>
      {/if}
    </div>
    {/if}

    {#if saveErr}
      <Notice tone="err">{saveErr}</Notice>
    {/if}

    <div class="flex items-center gap-2">
      <SaveButton size="small" dirty={formDirty} saving={busy} onclick={save} />
      <Button onclick={() => (form = null)}>{t("common.cancel")}</Button>
      <label class="ml-auto flex items-center gap-1.5 text-s text-dida-text-muted">
        <input type="checkbox" bind:checked={form.enabled} /> {t("auto.enabled")}
      </label>
    </div>
  </div>
{/if}
{/snippet}

<!-- NEW rule: editor anchored at the top, by the New / AI-draft controls. An
     EDIT opens inline below its own row (in the list loop) instead. -->
{#if form && form.id === null}
  {@render formEditor()}
{/if}

<!-- list -->
{#if items.length === 0 && !form}
  <div class="rounded-lg border border-dashed border-dida-border p-8 text-center text-dida-text-muted">
    {t("auto.emptyPre")} <strong>{t("auto.emptyBtn")}</strong>.
  </div>
{:else}
  {#if !form}
    <!-- filter row: by name (also matches referenced device names) + by device -->
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <input bind:value={filterName} placeholder={t("auto.filterName")} class="min-w-0 flex-1" />
      <select bind:value={filterType}>
        <option value="">{t("auto.filterAllTypes")}</option>
        {#each typeOptions as ty (ty)}<option value={ty}>{typeLabel(ty)}</option>{/each}
      </select>
      {#if filterName || filterType}
        <Button size="small" onclick={() => { filterName = ""; filterType = ""; }}>{t("auto.filterClear")}</Button>
      {/if}
    </div>
  {/if}
  <div class="flex flex-col gap-5">
    {#each groupedItems as g (g.domain)}
      <section>
        <h3 class="mb-2 {SUBSECTION_TITLE_CLASS}">{g.domain} <span class="ml-1 text-dida-text-faint">· {g.items.length}</span></h3>
        <div class="flex flex-col gap-2">
          {#each g.items as a (a.id)}
            {@const open = selectedId === a.id}
            <div class="rounded-lg border bg-dida-panel transition-colors {open ? 'border-dida-accent/60' : 'border-dida-border'}">
        <div class="flex items-start gap-3 p-3">
          <div class="mt-0.5"><Toggle
            size="small" checked={a.enabled} onclick={() => toggle(a)}
            label={t("auto.enabled")}
            title={a.enabled ? t("auto.enabledTitle") : t("auto.disabledTitle")}
          /></div>
          <!-- header: click to expand this rule (auto-collapses the others) -->
          <button type="button" onclick={() => (selectedId = open ? null : a.id)} class="min-w-0 flex-1 text-left">
            <div class="font-semibold leading-tight">{a.name}</div>
            <div class="font-mono text-s text-dida-text-faint {open ? '' : 'truncate'}">{summary(a)}</div>
            {#if open}
              {#if a.last_error}
                <div class="mt-1 text-s text-dida-danger">⚠ {a.last_error}</div>
              {:else if a.last_triggered_at}
                <div class="mt-1 text-s text-dida-text-faint">{t("auto.last", { when: dateTime(a.last_triggered_at) })}</div>
              {/if}
            {/if}
          </button>
          {#if open}
            <div class="flex shrink-0 gap-1">
              <Button size="small" onclick={() => runNow(a)} disabled={running === a.id || !a.enabled} title={t("ai.runTitle")}
              >{running === a.id ? t("ai.running") : `▶ ${t("ai.run")}`}</Button>
              <Button size="small" onclick={() => explain(a)} disabled={explaining[a.id]?.loading} title={t("ai.explainTitle")}
              >{explaining[a.id]?.loading ? t("ai.explaining") : `✨ ${t("ai.explain")}`}</Button>
              <Button size="small" onclick={() => startEdit(a)}>{t("common.edit")}</Button>
              <Button tone="danger" size="small" onclick={() => remove(a)}>{t("common.delete")}</Button>
            </div>
          {:else}
            <span class="mt-1 shrink-0 text-s text-dida-text-faint">▸</span>
          {/if}
        </div>
        {#if open && (explaining[a.id]?.text || explaining[a.id]?.err)}
          <div class="border-t border-dida-border/60 px-3 py-2">
            {#if explaining[a.id]?.text}
              <div class="whitespace-pre-wrap rounded border border-dida-accent/30 bg-dida-accent/5 p-2 text-s leading-relaxed text-dida-text">{explaining[a.id].text}</div>
            {:else}
              <Notice tone="err">{explaining[a.id].err}</Notice>
            {/if}
          </div>
        {/if}
      </div>
            {#if form && form.id === a.id}
              {@render formEditor()}
            {/if}
          {/each}
        </div>
      </section>
    {:else}
      <p class="rounded-lg border border-dashed border-dida-border p-6 text-center text-m text-dida-text-faint">{t("auto.filterNoMatch")}</p>
    {/each}
  </div>
{/if}
