<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Card, Picks, SaveButton, Tag, dialog } from "$lib/kit";
  import { api, type Automation, type Condition, type ComputedHelper, type Scalar, type VirtualEntity } from "$lib/api";
  import { devices } from "$lib/store.svelte";
  import { capLabel as deviceCapLabel } from "$lib/capabilities";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { LABEL_CLASS } from "$lib/ui";
  import { auth } from "$lib/auth.svelte";

  const BADGE = "inline-block rounded px-1.5 py-0.5 text-s";
  const cmp = (a: string, b: string) => a.localeCompare(b, "hr");

  // A helper is one of two KINDS, unified into a single list and a single builder:
  //   manual   — a value YOU set by hand (virtual entity, `virtual:*`).
  //   computed — a value a RULE derives from statuses (computed helper, `helper:*`).
  type Row = { kind: "manual"; v: VirtualEntity } | { kind: "computed"; c: ComputedHelper };

  let virtuals = $state<VirtualEntity[]>([]);
  let computed = $state<ComputedHelper[]>([]);
  let autos = $state<Automation[]>([]);
  // A virtual helper deliberately grouped under a real device (its device_key
  // points elsewhere — e.g. the mower's intensity slider) is already surfaced on
  // that device's card; listing it here too is just noise. Keep only standalone
  // helpers (no device_key, or one that IS the helper itself).
  const standaloneVirtual = (v: VirtualEntity): boolean =>
    (devices.byId[v.entity_id]?.deviceKey ?? v.entity_id) === v.entity_id;
  const rows = $derived<Row[]>(
    [
      ...virtuals.filter(standaloneVirtual).map((v): Row => ({ kind: "manual", v })),
      ...computed.map((c): Row => ({ kind: "computed", c })),
    ].sort((a, b) => cmp(rowName(a), rowName(b))),
  );
  const rowName = (r: Row): string => (r.kind === "manual" ? r.v.name : r.c.name);
  const rowId = (r: Row): string => (r.kind === "manual" ? r.v.entity_id : r.c.entity_id);

  async function load() {
    try { [virtuals, computed, autos] = await Promise.all([api.listVirtual(), api.listComputed(), api.listAutomations()]); }
    catch (e) { bMsg = errMsg(e); }
  }

  // Automations a helper drives: any that trigger on / reference its entity_id
  // (trigger, action, condition, or Starlark script). Lets each helper link to
  // the rules built on it — a manual switch like "Big Screen" to its On/Off pair.
  function condRefs(cs: Condition[] | undefined, eid: string): boolean {
    return (cs ?? []).some((c) => c.entity_id === eid || condRefs(c.conditions, eid));
  }
  function drivesEntity(a: Automation, eid: string): boolean {
    const d = a.definition;
    const trigs = d.triggers ?? [];
    return trigs.some((tr) => tr.entity_id === eid)
      || (d.actions ?? []).some((ac) => ac.entity_id === eid)
      || condRefs(d.conditions, eid)
      || (!!d.script && d.script.includes(eid));
  }
  const autoCount = (eid: string): number => autos.filter((a) => drivesEntity(a, eid)).length;

  const capLabel = (c: string): string =>
    c === "boolean" ? t("helpers.boolean")
      : c === "on_off" ? t("helpers.switch")
      : c === "enum" ? t("helpers.enum")
      : c === "time" ? t("helpers.time")
      : c === "number" ? t("helpers.number")
      : c === "text" ? t("helpers.value")
      : c;

  // ── Builder (one form, two kinds) ──
  type Leaf = { entity_id: string; capability: string; op: string; value: string };
  type Branch = { conds: Leaf[]; value: string };
  const OPERATORS = ["==", "!=", "<", "<=", ">", ">="];
  const blankBranch = (): Branch => ({ conds: [{ entity_id: "", capability: "", op: "==", value: "" }], value: "" });

  let bKind = $state<"manual" | "computed">("computed");
  let bName = $state("");
  let bMsg = $state<string | null>(null);
  // The builder is a collapsed panel: it opens on "Add helper" (new) or Edit
  // (an existing computed rule), not permanently taking the top of the page.
  let builderOpen = $state(false);
  // manual sub-form
  let mType = $state("boolean");
  let mOptions = $state(""); // comma-separated, enum only
  let mMin = $state(0); let mMax = $state(100); let mStep = $state(1); // number slider range
  let mSetting = $state(true);

  // A number helper's live slider range (from its published number_options).
  const numRange = (id: string): { min: number; max: number; step: number } | null => {
    const v = devices.byId[id]?.caps?.number_options?.value;
    if (typeof v !== "string") return null;
    try { const o = JSON.parse(v); return { min: Number(o.min), max: Number(o.max), step: Number(o.step ?? 1) }; }
    catch { return null; }
  };
  // computed sub-form
  let cCap = $state("boolean");
  let cMode = $state<"typed" | "advanced">("typed");
  let cBranches = $state<Branch[]>([blankBranch()]);
  let cDefault = $state("");
  let cScript = $state("");
  let cEditId = $state<number | null>(null); // set = editing an existing computed helper
  let saving = $state(false);
  const computedForm = () => JSON.stringify({ bName, cCap, cMode, cBranches, cDefault, cScript });
  let computedSaved = $state("");
  const dirty = $derived(cEditId === null || computedForm() !== computedSaved);
  const CH_CAPS = ["boolean", "text", "number", "enum", "time"];

  function resetBuilder() {
    bName = ""; bMsg = null;
    mType = "boolean"; mOptions = ""; mMin = 0; mMax = 100; mStep = 1; mSetting = true;
    cCap = "boolean"; cMode = "typed"; cBranches = [blankBranch()]; cDefault = ""; cScript = ""; cEditId = null;
  }
  function openNew() { resetBuilder(); bKind = "computed"; builderOpen = true; }
  function closeBuilder() { resetBuilder(); builderOpen = false; }

  const statusEntities = $derived(
    Object.values(devices.byId)
      .filter((d) => !d.entityId.startsWith("helper:") && !d.entityId.startsWith("derived:"))
      .sort((a, b) => cmp(a.name, b.name)),
  );
  // The condition leaf picks from the whole house — the shared picker, not a
  // flat select, so two same-named sensors in different rooms stay tellable.
  const leafItems = $derived(statusEntities.map(pickerItem));
  const capsOf = (eid: string): string[] => Object.keys(devices.byId[eid]?.caps ?? {});
  const coerce = (s: string): Scalar => {
    const v = s.trim();
    if (v === "true") return true;
    if (v === "false") return false;
    if (v !== "" && !Number.isNaN(Number(v))) return Number(v);
    return s;
  };

  const addBranch = () => { cBranches = [...cBranches, blankBranch()]; };
  const addCond = (b: Branch) => { b.conds = [...b.conds, { entity_id: "", capability: "", op: "==", value: "" }]; };

  function scriptKeydown(e: KeyboardEvent) {
    if (e.key !== "Tab" || e.shiftKey) return;
    e.preventDefault();
    const ta = e.currentTarget as HTMLTextAreaElement;
    const s = ta.selectionStart, en = ta.selectionEnd;
    cScript = cScript.slice(0, s) + "    " + cScript.slice(en);
    requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = s + 4; });
  }

  function editComputed(c: ComputedHelper) {
    resetBuilder();
    bKind = "computed"; cEditId = c.id; bName = c.name; cCap = c.capability;
    if (c.definition.script) {
      cMode = "advanced"; cScript = c.definition.script;
    } else {
      cMode = "typed";
      cBranches = (c.definition.branches ?? []).map((b) => ({
        value: String(b.value ?? ""),
        conds: b.conditions.map((cd) => ({ entity_id: cd.entity_id, capability: cd.capability, op: cd.op || "==", value: String(cd.value ?? "") })),
      }));
      if (cBranches.length === 0) cBranches = [blankBranch()];
      cDefault = String(c.definition.default ?? "");
    }
    computedSaved = computedForm();
    builderOpen = true;
    scrollTo({ top: 0, behavior: "smooth" });
  }

  async function save() {
    bMsg = null;
    const name = bName.trim();
    if (!name) return;
    saving = true;
    try {
      if (bKind === "manual") {
        const options = mType === "enum" ? mOptions.split(",").map((o) => o.trim()).filter(Boolean) : null;
        const range = mType === "number" ? { min: mMin, max: mMax, step: mStep } : undefined;
        await api.createVirtual(name, mType, options, mSetting ? "config" : "control", range);
      } else {
        const definition = cMode === "advanced"
          ? { script: cScript.trim() }
          : {
              branches: cBranches
                .filter((b) => b.conds.some((c) => c.entity_id))
                .map((b) => ({
                  conditions: b.conds.filter((c) => c.entity_id).map((c) => ({ entity_id: c.entity_id, capability: c.capability, op: c.op, value: coerce(c.value) })),
                  value: coerce(b.value),
                })),
              default: coerce(cDefault),
            };
        if (cEditId !== null) await api.updateComputed(cEditId, name, cCap, definition, true);
        else await api.createComputed(name, cCap, definition);
      }
      resetBuilder();
      builderOpen = false;
      await load();
    } catch (e) { bMsg = errMsg(e); }
    finally { saving = false; }
  }

  async function remove(r: Row) {
    const ok = await dialog.confirm({ title: t("common.delete"), message: t("helpers.confirmDelete", { name: rowName(r) }), confirmLabel: t("common.delete"), danger: true });
    if (!ok) return;
    try {
      if (r.kind === "manual") await api.deleteVirtual(r.v.entity_id);
      else { await api.deleteComputed(r.c.id); if (cEditId === r.c.id) resetBuilder(); }
      await load();
    } catch (e) { bMsg = errMsg(e); }
  }

  // ── Live value (manual is editable via a command; computed is read-only) ──
  const liveVal = (id: string, cap: string): unknown => devices.byId[id]?.caps?.[cap]?.value;
  async function send(v: VirtualEntity, command: string, value?: Scalar) {
    bMsg = null;
    try {
      await api.sendCommand({ entity_id: v.entity_id, capability: v.capability, command, ...(value === undefined ? {} : { args: { value } }) });
    } catch (e) { bMsg = errMsg(e); }
  }
  const toHHMM = (min: unknown): string => {
    const m = typeof min === "number" ? min : Number(min);
    if (!Number.isFinite(m)) return "00:00";
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  };
  const fromHHMM = (s: string): number => {
    const [h, m] = s.split(":").map(Number);
    return (h || 0) * 60 + (m || 0);
  };
  const ruleSummary = (c: ComputedHelper): string =>
    c.definition.script
      // List view: drop comment lines (free prose, often in another language) and
      // collapse to the logic itself. The full script stays in the Edit panel.
      ? c.definition.script.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).join(" · ")
      : (c.definition.branches ?? [])
          .map((b) => "IF " + b.conditions.map((cd) => `${cd.entity_id} ${cd.op} ${cd.value}`).join(" & ") + ` → ${b.value}`)
          .join("  ·  ") + `  ·  else ${c.definition.default}`;

  onMount(load);
</script>

<svelte:head><title>{t("helpers.pageTitle")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  <div class="max-w-5xl space-y-6">

    <!-- ── One builder: Manual (virtual) or Computed (rule) — collapsed until
         "Add helper" or an Edit opens it ── -->
    {#if builderOpen}
    <Card title={cEditId !== null ? t("helpers.editHelper") : t("helpers.newHelper")}>
      {#snippet actions()}
        <Picks picks={[{ key: "manual", label: t("helpers.kindManual") }, { key: "computed", label: t("helpers.kindComputed") }]}
          chosen={[bKind]} onpick={(k) => (bKind = k as typeof bKind)} disabled={cEditId !== null} />
      {/snippet}

      <div class="space-y-3">
        <p class="text-s text-dida-text-muted">{bKind === "manual" ? t("helpers.manualHint") : t("helpers.computedHint")}</p>

        <div class="flex flex-wrap items-end gap-3">
          <label class="space-y-1">
            <span class={LABEL_CLASS}>{t("helpers.name")}</span>
            <input bind:value={bName} placeholder={t("helpers.namePlaceholder")} class="w-52" />
          </label>

          {#if bKind === "manual"}
            <label class="space-y-1">
              <span class={LABEL_CLASS}>{t("helpers.type")}</span>
              <select bind:value={mType}>
                <option value="boolean">{t("helpers.boolean")}</option>
                <option value="on_off">{t("helpers.switch")}</option>
                <option value="enum">{t("helpers.enum")}</option>
                <option value="number">{t("helpers.number")}</option>
                <option value="time">{t("helpers.time")}</option>
              </select>
            </label>
            {#if mType === "enum"}
              <label class="min-w-56 flex-1 space-y-1">
                <span class={LABEL_CLASS}>{t("helpers.options")}</span>
                <input bind:value={mOptions} placeholder={t("helpers.optionsPlaceholder")} class="w-full" />
              </label>
            {:else if mType === "number"}
              <label class="space-y-1"><span class={LABEL_CLASS}>{t("helpers.rangeMin")}</span>
                <input type="number" bind:value={mMin} class="w-20" /></label>
              <label class="space-y-1"><span class={LABEL_CLASS}>{t("helpers.rangeMax")}</span>
                <input type="number" bind:value={mMax} class="w-20" /></label>
              <label class="space-y-1"><span class={LABEL_CLASS}>{t("helpers.rangeStep")}</span>
                <input type="number" bind:value={mStep} class="w-20" /></label>
            {/if}
            <label class="flex items-center gap-1.5 pb-2 text-m text-dida-text-muted">
              <input type="checkbox" bind:checked={mSetting} />
              {t("helpers.config")}
            </label>
          {:else}
            <label class="space-y-1">
              <span class={LABEL_CLASS}>{t("helpers.type")}</span>
              <select bind:value={cCap}>
                {#each CH_CAPS as c (c)}<option value={c}>{capLabel(c)}</option>{/each}
              </select>
            </label>
            <div class="ml-auto self-end"><Picks picks={[{ key: "typed", label: t("helpers.ruleTyped") }, { key: "advanced", label: t("helpers.ruleAdvanced") }]}
              chosen={[cMode]} onpick={(k) => (cMode = k as typeof cMode)} /></div>
          {/if}
        </div>

        {#if bKind === "computed"}
          {#if cMode === "advanced"}
            <textarea bind:value={cScript} rows="14" spellcheck="false" onkeydown={scriptKeydown}
              placeholder={'value = "night" if state("astro:sun", "sun_elevation") < -6 else "day"'}
              class="min-h-[16rem] w-full resize-y font-mono leading-relaxed"></textarea>
            <p class="text-s text-dida-text-faint">{t("helpers.advancedHint")}</p>
          {:else}
            {#each cBranches as b, bi (bi)}
              <div class="rounded border border-dida-border/50 bg-dida-panel-2/30 p-2">
                <div class="mb-1.5 text-s font-medium text-dida-text-muted">{t("helpers.branchIf")}</div>
                {#each b.conds as leaf, li (li)}
                  <div class="mb-1 flex flex-wrap items-center gap-1">
                    <div class="w-60 max-w-full">
                      <EntityPicker bind:value={leaf.entity_id} items={leafItems}
                        onchange={(id) => (leaf.capability = capsOf(id)[0] ?? "")} />
                    </div>
                    <select bind:value={leaf.capability}>
                      {#each capsOf(leaf.entity_id) as cp (cp)}<option value={cp}>{deviceCapLabel(cp)}</option>{/each}
                    </select>
                    <select bind:value={leaf.op}>{#each OPERATORS as o (o)}<option value={o}>{o}</option>{/each}</select>
                    <input bind:value={leaf.value} placeholder={t("helpers.value")} class="w-28" />
                    {#if b.conds.length > 1}<Button size="small" label={t("common.remove")} title={t("common.remove")} onclick={() => (b.conds = b.conds.filter((_, i) => i !== li))}>✕</Button>{/if}
                  </div>
                {/each}
                <div class="mt-1.5 flex flex-wrap items-center gap-1">
                  <Button size="small" onclick={() => addCond(b)}>+ {t("helpers.branchAnd")}</Button>
                  <span class="ml-3 text-s text-dida-text-muted">→ {t("helpers.value")}:</span>
                  <input bind:value={b.value} placeholder={t("helpers.value")} class="w-32" />
                  {#if cBranches.length > 1}<Button size="small" onclick={() => (cBranches = cBranches.filter((_, i) => i !== bi))}>{t("common.delete")}</Button>{/if}
                </div>
              </div>
            {/each}
            <div class="flex flex-wrap items-center gap-2">
              <Button size="small" onclick={addBranch}>+ {t("helpers.branchAdd")}</Button>
              <span class="ml-3 text-s text-dida-text-muted">{t("helpers.branchElse")}:</span>
              <input bind:value={cDefault} placeholder={t("helpers.value")} class="w-32" />
            </div>
          {/if}
        {/if}

        <div class="flex items-center gap-2">
          <SaveButton {dirty} {saving} blocked={!bName.trim()} onclick={save} label={cEditId !== null ? undefined : t("helpers.add")} />
          <Button onclick={closeBuilder}>{t("common.cancel")}</Button>
          {#if bMsg}<span class="text-s text-dida-danger">{bMsg}</span>{/if}
        </div>
      </div>
    </Card>
    {/if}

    <!-- ── One list: every helper, manual and computed together ── -->
    <Card title={t("helpers.allTitle")}>
      {#snippet actions()}
        {#if !builderOpen}<Button tone="primary" size="small" onclick={openNew}>+ {t("helpers.add")}</Button>{/if}
      {/snippet}
      <div class="divide-y divide-dida-border/40">
        {#each rows as r (rowId(r))}
          <div class="flex items-start gap-3 py-3 first:pt-0 last:pb-0 hover:bg-dida-panel-2/30 {r.kind === 'computed' && cEditId === r.c.id ? 'bg-dida-accent/5' : ''}">
            <!-- Name + metadata. min-w-0 lets the rule summary truncate rather than
                 push the row wider than the card — the source of the old h-scroll. -->
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="font-medium">{rowName(r)}</span>
                <span class="{BADGE} bg-dida-panel-2 text-dida-text-muted">{capLabel(r.kind === "manual" ? r.v.capability : r.c.capability)}</span>
                <span class="{BADGE} {r.kind === 'computed' ? 'bg-dida-accent/15 text-dida-accent' : 'bg-dida-panel-2 text-dida-text-muted'}">
                  {r.kind === "computed" ? t("helpers.kindComputed") : t("helpers.kindManual")}
                </span>
                {#if r.kind === "manual"}
                  <span class="{BADGE} {r.v.category === 'config' ? 'bg-dida-accent/15 text-dida-accent' : 'bg-dida-panel-2 text-dida-text-muted'}">
                    {r.v.category === "config" ? t("helpers.config") : t("helpers.control")}
                  </span>
                {/if}
              </div>
              <div class="mt-0.5 text-s text-dida-text-faint">{rowId(r)}</div>
              {#if r.kind === "computed"}
                <code class="mt-1 block truncate text-s text-dida-text-faint" title={ruleSummary(r.c)}>{ruleSummary(r.c)}</code>
                {#if r.c.last_error}<div class="mt-0.5 text-s text-dida-danger">⚠ {r.c.last_error}</div>{/if}
              {/if}
            </div>

            <!-- Value: editable for manual, read-only for computed -->
            <div class="shrink-0 pt-0.5 text-m">
              {#if r.kind === "manual"}
                {@const v = liveVal(r.v.entity_id, r.v.capability)}
                {#if r.v.capability === "boolean" || r.v.capability === "on_off"}
                  <Tag tone={v ? "busy" : "quiet"} onclick={() => send(r.v, v ? "turn_off" : "turn_on")}>{v ? t("common.on") : t("common.off")}</Tag>
                {:else if r.v.capability === "time"}
                  <input type="time" value={toHHMM(v)}
                    onchange={(e) => send(r.v, "set_time", fromHHMM((e.currentTarget as HTMLInputElement).value))}
                    />
                {:else if r.v.capability === "enum"}
                  <select value={String(v ?? "")}
                    onchange={(e) => send(r.v, "set_option", (e.currentTarget as HTMLSelectElement).value)}
                   >
                    {#each Array.isArray(r.v.options) ? r.v.options : [] as o (o)}<option value={o}>{o}</option>{/each}
                  </select>
                {:else if r.v.capability === "number"}
                  {@const rg = numRange(r.v.entity_id)}
                  {#if rg}
                    <div class="flex items-center gap-2">
                      <input type="range" min={rg.min} max={rg.max} step={rg.step} value={Number(v ?? rg.min)}
                        onchange={(e) => send(r.v, "set_value", Number((e.currentTarget as HTMLInputElement).value))}
                        class="w-28 sm:w-36" aria-label={rowName(r)} />
                      <span class="w-8 text-right font-mono text-s tabular-nums text-dida-text">{v ?? rg.min}</span>
                    </div>
                  {:else}
                    <input type="number" value={String(v ?? 0)}
                      onchange={(e) => send(r.v, "set_value", Number((e.currentTarget as HTMLInputElement).value))}
                      class="w-24" />
                  {/if}
                {/if}
              {:else}
                <span class="font-mono">{liveVal(r.c.entity_id, r.c.capability) ?? "—"}</span>
              {/if}
            </div>

            <!-- Actions -->
            <div class="flex shrink-0 items-center gap-1 pt-0.5">
              {#if autoCount(rowId(r)) > 0}
                <a href="/automations?q={encodeURIComponent(rowName(r))}"
                   class="rounded px-2 py-1 text-s text-dida-accent hover:underline"
                   title={t("nav.automations")}>{t("nav.automations")} ({autoCount(rowId(r))})</a>
              {/if}
              {#if r.kind === "computed"}
                <Button size="small" onclick={() => editComputed(r.c)}>{t("common.edit")}</Button>
              {/if}
              <Button tone="danger" size="small" onclick={() => remove(r)}>{t("common.delete")}</Button>
            </div>
          </div>
        {/each}
        {#if rows.length === 0}
          <div class="px-4 py-3 text-s text-dida-text-faint">{t("helpers.empty")}</div>
        {/if}
      </div>
    </Card>
  </div>
{/if}
