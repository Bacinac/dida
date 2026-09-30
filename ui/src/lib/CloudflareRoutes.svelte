<script lang="ts">
  // Settings → Cloudflare: CRUD over services.conf, the single ingress source. A save
  // writes that file and waits for the Proxmox host to apply it (Caddy, the tunnel,
  // DNS, the router's split-DNS, the Homepage tile) — so the reply reflects what the
  // ingress actually does, not what was merely stored. A rejected change is rolled
  // back on the host side. Routes are grouped by the origin host, alpha within.
  import { onMount } from "svelte";
  import { Button, Card, Field, Notice, SaveButton, Toggle, dialog } from "$lib/kit";
  import { api, type CfTunnelState, type CloudflareApply, type CloudflareRoute, type CloudflareRouteEdit } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { t } from "$lib/i18n";
  import { Section } from "$lib/ui";

  const blank = (): CloudflareRouteEdit =>
    ({ hostname: "", service: "", serve: "both", opts: [], section: null, icon: null });

  const SERVE: CloudflareRoute["serve"][] = ["both", "lan", "wan"];
  const hasOpt = (r: { opts: string[] }, o: string): boolean => r.opts.includes(o);
  const toggleOpt = (r: { opts: string[] }, o: string): void => {
    r.opts = hasOpt(r, o) ? r.opts.filter((x) => x !== o) : [...r.opts, o];
  };

  // Origin host a route points at (http://192.0.2.30:8686 → 192.0.2.30) — the grouping key.
  const serviceHost = (svc: string): string => svc.match(/^[a-z0-9+.-]+:\/\/([^:/]+)/i)?.[1] ?? "";
  const hostKey = (h: string): [number, number[], string] => {
    const p = h.split(".");
    return p.length === 4 && p.every((x) => /^\d+$/.test(x)) ? [0, p.map(Number), ""] : [1, [], h.toLowerCase()];
  };

  let tunnel = $state<CfTunnelState | null>(null);
  let tunnelName = $state("");
  let creating = $state(false);
  let routes = $state<CloudflareRoute[]>([]);
  let source = $state("");
  let apply = $state<CloudflareApply | null>(null);
  let loadErr = $state<string | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let msg = $state<{ tone: "info" | "warn"; text: string } | null>(null);
  let q = $state("");

  let add = $state<CloudflareRouteEdit>(blank());
  let editing = $state<string | null>(null);   // hostname being edited
  let draft = $state<CloudflareRouteEdit>(blank());

  const shown = $derived.by(() => {
    const s = q.trim().toLowerCase();
    return !s ? routes : routes.filter((r) => r.hostname.toLowerCase().includes(s) || r.service.toLowerCase().includes(s));
  });
  // Group by the origin HOST each route exposes (IPs sorted numerically), hostname
  // alpha within — so all services on one machine sit together.
  const groups = $derived.by(() => {
    const by = new Map<string, CloudflareRoute[]>();
    for (const r of shown) {
      const h = serviceHost(r.service);
      const arr = by.get(h);
      if (arr) arr.push(r); else by.set(h, [r]);
    }
    return [...by.entries()]
      .map(([host, items]) => ({ host, items: items.slice().sort((a, b) => a.hostname.localeCompare(b.hostname, "hr")) }))
      .sort((a, b) => {
        const ka = hostKey(a.host), kb = hostKey(b.host);
        if (ka[0] !== kb[0]) return ka[0] - kb[0];
        for (let i = 0; i < 4; i++) { const d = (ka[1][i] ?? 0) - (kb[1][i] ?? 0); if (d) return d; }
        return ka[2].localeCompare(kb[2]);
      });
  });
  const canAdd = $derived(add.hostname.trim() !== "" && add.service.trim() !== "");
  $effect(() => { if (add.service.startsWith("ssh://")) add.serve = "wan"; });
  $effect(() => { if (draft.service.startsWith("ssh://")) draft.serve = "wan"; });
  const canSave = $derived(draft.service.trim() !== "");
  let draftSaved = $state("");
  const draftDirty = $derived(JSON.stringify(draft) !== draftSaved);

  async function load() {
    loading = true;
    loadErr = null;
    try {
      tunnel = await api.cloudflareTunnelState();
    } catch {
      tunnel = null;
    }
    try {
      const res = await api.cloudflareRoutes();
      routes = res.routes;
      source = res.source_path;
      apply = res.apply;
    } catch (e) {
      loadErr = errMsg(e);
    } finally {
      loading = false;
    }
  }

  async function createTunnel() {
    creating = true;
    msg = null;
    try {
      const r = await api.cloudflareTunnelCreate(tunnelName.trim());
      msg = { tone: "info", text: t("cf.tunnel.created", { id: r.tunnel_id ?? "" }) };
      await load();
    } catch (e) {
      msg = { tone: "warn", text: errMsg(e) };
    } finally {
      creating = false;
    }
  }
  onMount(load);

  function startEdit(r: CloudflareRoute) {
    editing = r.hostname;
    draft = { hostname: r.hostname, service: r.service, serve: r.service.startsWith("ssh://") ? "wan" : r.serve,
              opts: [...r.opts], section: r.section, icon: r.icon };
    draftSaved = JSON.stringify(draft);
  }

  async function addRoute() {
    busy = true;
    msg = null;
    try {
      const res = await api.cloudflareAddRoute(add);
      msg = res.applied
        ? { tone: "info", text: `${t("cf.applied")} — ${(res.steps ?? []).join(" · ")}` }
        : { tone: "warn", text: `${t("cf.applyFailed")}: ${res.error ?? ""}` };
      if (res.applied) add = blank();
      await load();
    } catch (e) {
      msg = { tone: "warn", text: errMsg(e) };
    } finally {
      busy = false;
    }
  }

  async function saveEdit() {
    if (editing === null) return;
    busy = true;
    msg = null;
    try {
      const res = await api.cloudflareEditRoute(editing, draft);
      if (res.applied) editing = null;
      msg = res.applied
        ? { tone: "info", text: `${t("cf.applied")} — ${(res.steps ?? []).join(" · ")}` }
        : { tone: "warn", text: `${t("cf.applyFailed")}: ${res.error ?? ""}` };
      await load();
    } catch (e) {
      msg = { tone: "warn", text: errMsg(e) };
    } finally {
      busy = false;
    }
  }

  async function removeRoute(r: CloudflareRoute) {
    const ok = await dialog.confirm({
      title: t("cf.remove"),
      message: t("cf.removeConfirm", { host: r.hostname }),
      confirmLabel: t("cf.remove"),
      danger: true,
    });
    if (!ok) return;
    busy = true;
    msg = null;
    try {
      const res = await api.cloudflareRemoveRoute(r.hostname);
      msg = res.applied
        ? { tone: "info", text: `${t("cf.applied")} — ${(res.steps ?? []).join(" · ")}` }
        : { tone: "warn", text: `${t("cf.applyFailed")}: ${res.error ?? ""}` };
      await load();
    } catch (e) {
      msg = { tone: "warn", text: errMsg(e) };
    } finally {
      busy = false;
    }
  }
</script>

<!-- Rendered inside the cloudflare adapter's card on Settings → Adapters — the
     same home every adapter's special UI has. The card header carries the name;
     the subtitle line below explains what a save does. -->
<div class="mt-3 border-t border-dida-border pt-3">
<p class="mb-3 text-s text-dida-text-faint">{t("cf.subtitle")}</p>
{#if loadErr}
  <Notice tone="warn">{loadErr}</Notice>
{:else if loading}
  <p class="py-6 text-center text-m text-dida-text-faint">…</p>
{:else if tunnel?.mode === "none"}
  <!-- No ingress yet: a NEW installation mints its own tunnel here — with the
       owner's API token from this adapter's settings, on the owner's account. -->
  <Section title={t("cf.tunnel.title")}>
    <Card>
      <p class="mb-3 text-m text-dida-text-faint">{t("cf.tunnel.hint", { zone: tunnel.zone || "—" })}</p>
      {#if !tunnel.has_token}
        <Notice tone="warn">{t("cf.tunnel.needToken")}</Notice>
      {:else}
        <div class="flex flex-wrap items-end gap-3">
          <Field label={t("cf.tunnel.name")} id="cf-tunnel-name">
            <input id="cf-tunnel-name" bind:value={tunnelName} placeholder="dida-kuca" class="w-full font-mono" />
          </Field>
          <Button tone="primary" size="small" onclick={createTunnel} disabled={creating || !tunnelName.trim()}>
            {creating ? t("cf.tunnel.creating") : t("cf.tunnel.create")}
          </Button>
        </div>
      {/if}
      {#if msg}<Notice tone={msg.tone}>{msg.text}</Notice>{/if}
    </Card>
  </Section>
{:else}
  <Section title={t("cf.add")}>
    <Card>
      <div class="grid gap-3 @container sm:grid-cols-2">
        <Field label={t("cf.hostname")} id="cf-add-host">
          <input id="cf-add-host" bind:value={add.hostname} placeholder={t("cf.hostnamePh")} class="w-full font-mono" />
        </Field>
        <Field label={t("cf.service")} id="cf-add-svc">
          <input id="cf-add-svc" bind:value={add.service} placeholder={t("cf.servicePh")} class="w-full font-mono" />
        </Field>
      </div>
      <div class="mt-3 grid gap-3 @container sm:grid-cols-3">
        <Field label={t("cf.serve")} id="cf-add-serve">
          <select id="cf-add-serve" bind:value={add.serve} class="w-full" disabled={add.service.startsWith("ssh://")}>
            {#each SERVE as s (s)}<option value={s}>{t(`cf.serve.${s}`)}</option>{/each}
          </select>
        </Field>
        <Field label={t("cf.section")} id="cf-add-section">
          <input id="cf-add-section" value={add.section ?? ""} oninput={(e) => (add.section = e.currentTarget.value || null)}
                 placeholder={t("cf.sectionPh")} class="w-full" />
        </Field>
        <Field label={t("cf.icon")} id="cf-add-icon">
          <input id="cf-add-icon" value={add.icon ?? ""} oninput={(e) => (add.icon = e.currentTarget.value || null)}
                 placeholder={t("cf.iconPh")} class="w-full font-mono" />
        </Field>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-m text-dida-text-muted">
          <Toggle size="small" checked={hasOpt(add, "nochunk")} onclick={() => toggleOpt(add, "nochunk")} label={t("cf.chunked")} />
          {t("cf.chunked")}
        </label>
        <Button tone="primary" size="small" onclick={addRoute} disabled={!canAdd || busy}>+ {t("cf.add")}</Button>
      </div>
    </Card>
  </Section>

  <Section title={`${t("cf.source")} · ${source || "—"}`}>
    <Card>
      {#if apply && apply.ok === false}
        <Notice tone="warn">{t("cf.applyFailed")}: {apply.error}</Notice>
      {/if}
      <input bind:value={q} placeholder="{t('cf.hostname')} / {t('cf.service')}" class="w-full mb-3" />
      {#if !shown.length}
        <p class="py-4 text-center text-m text-dida-text-faint">{t("cf.empty")}</p>
      {/if}
      <div class="space-y-2">
        {#each groups as g (g.host || "__none__")}
          <p class="pt-2 font-mono text-xs font-semibold uppercase tracking-wide text-dida-text-muted">{g.host || t("cf.ungrouped")}</p>
          {#each g.items as r (r.id)}
          <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
            {#if editing === r.hostname}
              <div class="space-y-2">
                <p class="truncate font-mono text-m font-semibold">{r.hostname}</p>
                <input bind:value={draft.service} placeholder={t("cf.servicePh")} class="w-full font-mono" />
                <div class="grid gap-2 sm:grid-cols-3">
                  <select bind:value={draft.serve} class="w-full" disabled={draft.service.startsWith("ssh://")}>
                    {#each SERVE as s (s)}<option value={s}>{t(`cf.serve.${s}`)}</option>{/each}
                  </select>
                  <input value={draft.section ?? ""} oninput={(e) => (draft.section = e.currentTarget.value || null)}
                         placeholder={t("cf.sectionPh")} class="w-full" />
                  <input value={draft.icon ?? ""} oninput={(e) => (draft.icon = e.currentTarget.value || null)}
                         placeholder={t("cf.iconPh")} class="w-full font-mono" />
                </div>
                <div class="flex flex-wrap items-center gap-4">
                  <label class="flex items-center gap-2 text-m text-dida-text-muted">
                    <Toggle size="small" checked={hasOpt(draft, "nochunk")} onclick={() => toggleOpt(draft, "nochunk")} label={t("cf.chunked")} />
                    {t("cf.chunked")}
                  </label>
                  <div class="ml-auto flex items-center gap-1.5">
                    <SaveButton size="small" dirty={draftDirty} saving={busy} blocked={!canSave} onclick={saveEdit} />
                    <Button size="small" onclick={() => (editing = null)}>{t("common.cancel")}</Button>
                  </div>
                </div>
              </div>
            {:else}
              <div class="flex items-center gap-3">
                <div class="min-w-0 flex-1">
                  <p class="truncate font-mono text-m font-semibold">{r.hostname}</p>
                  <p class="truncate font-mono text-s text-dida-text-muted">{r.service}</p>
                  <p class="mt-0.5 text-xs uppercase tracking-wide text-dida-text-faint">
                    {[t(`cf.serve.${r.serve}`), hasOpt(r, "nochunk") ? t("cf.chunked") : "", r.section ?? ""].filter(Boolean).join(" · ")}
                  </p>
                </div>
                <div class="flex shrink-0 items-center gap-1.5">
                  <Button size="small" onclick={() => startEdit(r)}>{t("cf.edit")}</Button>
                  <Button size="small" onclick={() => removeRoute(r)}>{t("cf.remove")}</Button>
                </div>
              </div>
            {/if}
          </div>
          {/each}
        {/each}
      </div>
    </Card>
  </Section>

  {#if msg}<Notice tone={msg.tone}>{msg.text}</Notice>{/if}
{/if}
</div>
