<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Card, Notice } from "$lib/kit";
  import { api } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { translations } from "$lib/translations.svelte";

  // Every distinct display descriptor exposed on the UI (entity facet names +
  // device headers). English IS the key; we curate the Croatian (and any future
  // language) here. Missing → the UI falls back to the English key.
  const LANG = "hr"; // the one non-English language today; English is the base key
  let rows = $state<{ key: string; langs: Record<string, string> }[]>([]);
  let err = $state<string | null>(null);
  let msg = $state<string | null>(null);
  let q = $state("");
  let missingOnly = $state(false);
  let saving = $state<string | null>(null);
  let autofilling = $state(false);

  const filtered = $derived(
    rows.filter((r) => {
      if (missingOnly && (r.langs[LANG] ?? "").trim()) return false;
      const needle = q.trim().toLowerCase();
      if (!needle) return true;
      return r.key.toLowerCase().includes(needle) || (r.langs[LANG] ?? "").toLowerCase().includes(needle);
    }),
  );
  const missingCount = $derived(rows.filter((r) => !(r.langs[LANG] ?? "").trim()).length);

  async function load() {
    err = null;
    try {
      rows = await api.listTranslatableKeys();
    } catch (e) {
      err = errMsg(e);
    }
  }
  onMount(load);

  // Auto-grow the translation textarea to its content so long / multi-line values
  // are shown in full (never truncated), matching a multi-line English source. The
  // `_value` param makes the action re-measure when the value changes programmatically
  // (e.g. after autofill replaces the rows), not only on manual typing.
  function autosize(el: HTMLTextAreaElement, _value: string) {
    const resize = () => {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    };
    resize();
    el.addEventListener("input", resize);
    return { update: resize, destroy: () => el.removeEventListener("input", resize) };
  }

  async function autofill() {
    autofilling = true;
    err = null;
    msg = null;
    try {
      const res = await api.autofillTranslations(LANG);
      await load(); // pull the freshly-filled rows
      await translations.load(); // reflect live (if the UI is in hr)
      msg = t("translations.autofilled", { filled: res.filled, requested: res.requested });
    } catch (e) {
      err = errMsg(e);
    } finally {
      autofilling = false;
    }
  }

  async function save(row: { key: string; langs: Record<string, string> }, value: string) {
    const clean = value.trim();
    if ((row.langs[LANG] ?? "") === clean) return; // unchanged
    saving = row.key;
    msg = null;
    try {
      await api.putTranslation(row.key, LANG, clean);
      row.langs = { ...row.langs, [LANG]: clean };
      await translations.load(); // reflect the change live (if the UI is in hr)
      msg = t("translations.saved");
    } catch (e) {
      err = errMsg(e);
    } finally {
      saving = null;
    }
  }
</script>

<div class="mx-auto max-w-3xl space-y-4 p-4">

  {#if !auth.isAdmin}
    <Notice tone="err">{t("settings.adminOnly")}</Notice>
  {:else}
    {#if err}<Notice tone="err">{err}</Notice>{/if}
    {#if msg}<Notice>{msg}</Notice>{/if}

    <Card>
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <input class="w-full" placeholder={t("translations.search")} bind:value={q} />
        <label class="flex items-center gap-2 text-m text-dida-text-muted">
          <input type="checkbox" bind:checked={missingOnly} />
          {t("translations.missingOnly")} ({missingCount})
        </label>
        <span class="ml-auto"><Button size="small"

          disabled={autofilling || missingCount === 0}
          onclick={autofill}
        >
          {autofilling ? t("translations.autofilling") : t("translations.autofill", { count: missingCount })}
        </Button></span>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full table-fixed text-m">
          <thead class="text-left text-dida-text-muted">
            <tr>
              <th class="w-1/2 py-1 pr-3 font-medium">{t("translations.sourceColumn")}</th>
              <th class="w-1/2 py-1 font-medium">{t("translations.targetColumn")}</th>
            </tr>
          </thead>
          <tbody>
            {#each filtered as row (row.key)}
              <tr class="border-t border-dida-border align-top">
                <td class="py-1 pr-3">
                  <!-- mirrors the textarea's box model (transparent border + px-2 py-1.5 +
                       leading-snug) so the first line lines up with the translation on the right -->
                  <div class="whitespace-pre-wrap break-words border border-transparent px-2 py-1.5 leading-snug">
                    {row.key}
                  </div>
                </td>
                <td class="py-1">
                  <textarea
                    rows="1"
                    class="w-full resize-none overflow-hidden leading-snug"
                    value={row.langs[LANG] ?? ""}
                    placeholder={row.key}
                    disabled={saving === row.key}
                    use:autosize={row.langs[LANG] ?? ""}
                    onchange={(e) => save(row, (e.currentTarget as HTMLTextAreaElement).value)}
                  ></textarea>
                </td>
              </tr>
            {/each}
            {#if !filtered.length}
              <tr><td colspan="2" class="py-3 text-dida-text-muted">{t("translations.none")}</td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    </Card>
  {/if}
</div>
