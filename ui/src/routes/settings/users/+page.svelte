<script lang="ts">
  import { onMount } from "svelte";
  import { api, type User, type ControlRule, type AccessSetup } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { Button, Card, Notice, dialog, i18n } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { CONSUMER_PAGES } from "$lib/pages";
  import { auth } from "$lib/auth.svelte";
  import { devices } from "$lib/store.svelte";
  import { adapterLabel, capLabel } from "$lib/capabilities";
  import EntityPicker, { pickerItem } from "$lib/EntityPicker.svelte";

  // Consumer pages come from the single source of truth (ui/src/lib/pages.ts,
  // mirroring PAGE_ORDER in dida_api/users.py — the server authority). A user row
  // with allowed_pages = null sees them all; a subset restricts visibility.
  const pagesFor = (u: User): Set<string> => new Set(u.allowed_pages ?? CONSUMER_PAGES.map((p) => p.key));

  // --- Phase 2: per-user device control permissions (baseline + flip rules) ---
  type Scope = ControlRule["scope"];
  let expandedId = $state<string | null>(null);
  let addScope = $state<Scope>("entity");
  let addRef = $state("");
  let addViewScope = $state<Scope>("area");
  let addViewRef = $state("");

  const cmp = (a: string, b: string) => a.localeCompare(b, i18n.locale === "en" ? "en" : "hr");
  const pickerItems = $derived(Object.values(devices.byId).map(pickerItem));
  const capabilities = $derived(
    [...new Set(Object.values(devices.byId).flatMap((d) => d.capabilities))]
      .sort((a, b) => cmp(capLabel(a), capLabel(b))));

  const entChip = (id: string): string => {
    const d = devices.byId[id];
    if (!d) return id;
    // a chip must carry what tells same-named housemates apart: the adapter,
    // but only when name and room alone do not (both AVR faces are "Marantz")
    const twin = Object.values(devices.byId).some(
      (o) => o.entityId !== id && o.name === d.name && o.areaId === d.areaId);
    return `${d.name}${twin ? ` (${adapterLabel(d.adapter)})` : ""} · ${devices.areaName(d.areaId)}`;
  };
  function refLabel(r: ControlRule): string {
    if (r.scope === "area") { const a = devices.areas.find((x) => String(x.id) === r.ref); return a ? devices.roomLabel(a) : `#${r.ref}`; }
    if (r.scope === "entity") return entChip(r.ref);
    return capLabel(r.ref);
  }
  function refOptions(scope: Scope): { value: string; label: string }[] {
    if (scope === "area") return devices.sortedAreas.map((a) => ({ value: String(a.id), label: devices.roomLabel(a) }));
    return capabilities.map((c) => ({ value: c, label: capLabel(c) }));
  }

  let users = $state<User[]>([]);
  let nuName = $state("");
  let nuPw = $state("");
  let nuRole = $state("user");
  let userMsg = $state<string | null>(null);
  let resetId = $state<string | null>(null);
  let resetPw = $state("");

  async function loadUsers() {
    if (!auth.isAdmin) return;
    try { users = await api.listUsers(); } catch { users = []; }
  }
  async function addUser() {
    userMsg = null;
    if (!nuName.trim() || nuPw.length < 8) { userMsg = t("account.pwTooShort"); return; }
    try {
      await api.createUser(nuName.trim(), nuPw, nuRole);
      nuName = ""; nuPw = ""; nuRole = "user";
      await loadUsers();
    } catch (e) { userMsg = errMsg(e); }
  }
  async function changeRole(u: User, role: string) {
    try { await api.updateUser(u.id, { role }); await loadUsers(); }
    catch (e) { userMsg = errMsg(e); await loadUsers(); }
  }
  async function togglePage(u: User, page: string, on: boolean) {
    userMsg = null;
    const set = pagesFor(u);
    if (on) set.add(page); else set.delete(page);
    if (set.size === 0) { userMsg = t("users.needOnePage"); await loadUsers(); return; }
    // All pages checked ⇒ store null (full access, so future pages auto-grant).
    const arr = CONSUMER_PAGES.map((p) => p.key).filter((k) => set.has(k));
    const allowed_pages = arr.length === CONSUMER_PAGES.length ? null : arr;
    try { await api.updateUser(u.id, { allowed_pages }); await loadUsers(); }
    catch (e) { userMsg = errMsg(e); await loadUsers(); }
  }
  async function doResetPw(id: string) {
    if (resetPw.length < 8) { userMsg = t("account.pwTooShort"); return; }
    try { await api.updateUser(id, { password: resetPw }); resetId = null; resetPw = ""; userMsg = t("users.pwReset"); }
    catch (e) { userMsg = errMsg(e); }
  }
  async function saveControl(u: User, patch: { can_control?: boolean; control_rules?: ControlRule[]; view_hides?: ControlRule[] }) {
    userMsg = null;
    try { await api.updateUser(u.id, patch); await loadUsers(); }
    catch (e) { userMsg = errMsg(e); await loadUsers(); }
  }
  const addTo = (list: ControlRule[], scope: Scope, ref: string) =>
    list.some((r) => r.scope === scope && r.ref === ref) ? list : [...list, { scope, ref }];
  async function addRule(u: User) {
    if (!addRef) return;
    const next = addTo(u.control_rules, addScope, addRef); addRef = "";
    await saveControl(u, { control_rules: next });
  }
  const removeRule = (u: User, r: ControlRule) =>
    saveControl(u, { control_rules: u.control_rules.filter((x) => !(x.scope === r.scope && x.ref === r.ref)) });
  async function addHide(u: User) {
    if (!addViewRef) return;
    const next = addTo(u.view_hides, addViewScope, addViewRef); addViewRef = "";
    await saveControl(u, { view_hides: next });
  }
  const removeHide = (u: User, r: ControlRule) =>
    saveControl(u, { view_hides: u.view_hides.filter((x) => !(x.scope === r.scope && x.ref === r.ref)) });

  // --- Phone setup: one QR auto-logs the person in + lands on /onboard (which
  // offers one-tap location sharing). Rotate = new QR; remove = revoke both the
  // login token and location sharing. ---
  let accId = $state<string | null>(null);
  let accSetup = $state<AccessSetup | null>(null);
  let accBusy = $state(false);
  let accCopied = $state(false);

  async function openAccess(u: User) {
    if (accId === u.id) { accId = null; accSetup = null; return; }
    accId = u.id; accSetup = null; accCopied = false; accBusy = true;
    try { accSetup = await api.accessSetup(u.id); }
    catch (e) { userMsg = errMsg(e); accId = null; }
    finally { accBusy = false; }
  }
  async function rotateAccess(u: User) {
    const ok = await dialog.confirm({
      title: t("users.accessRotate"), message: t("users.accessRotateWarn"),
      confirmLabel: t("users.accessRotate"),
    });
    if (!ok) return;
    accBusy = true; accCopied = false;
    try { accSetup = await api.rotateAccess(u.id); }
    catch (e) { userMsg = errMsg(e); }
    finally { accBusy = false; }
  }
  async function revokeAccess(u: User) {
    const ok = await dialog.confirm({
      title: t("users.accessRevoke"), message: t("users.accessRevokeWarn", { name: u.username }),
      confirmLabel: t("users.accessRevoke"), danger: true,
    });
    if (!ok) return;
    try {
      // Removing a person's phone access revokes both their login QR and their
      // location sharing in one action.
      await Promise.all([api.revokeAccess(u.id), api.revokeOwntracks(u.id)]);
      accId = null; accSetup = null;
    } catch (e) { userMsg = errMsg(e); }
  }
  async function copyAccessLink() {
    if (!accSetup) return;
    try { await navigator.clipboard.writeText(accSetup.url); accCopied = true; setTimeout(() => (accCopied = false), 1500); }
    catch { /* clipboard blocked — the link field stays selectable as fallback */ }
  }

  async function removeUser(u: User) {
    const ok = await dialog.confirm({
      title: t("common.delete"), message: t("users.confirmDelete", { name: u.username }),
      confirmLabel: t("common.delete"), danger: true,
    });
    if (!ok) return;
    try { await api.deleteUser(u.id); await loadUsers(); }
    catch (e) { userMsg = errMsg(e); }
  }
  // The permission pickers read the device store the layout already loads —
  // the same names, rooms and ordering as every other picker in the app.
  onMount(loadUsers);
</script>

<svelte:head><title>{t("users.title")}</title></svelte:head>

{#if !auth.isAdmin}
  <p class="text-m text-dida-text-muted">{t("settings.adminOnly")}</p>
{:else}
  <Card>
    <div class="mb-4 flex flex-wrap items-end gap-2">
      <input bind:value={nuName} placeholder={t("login.username")}
        />
      <input type="password" bind:value={nuPw} placeholder={t("users.newPw")} autocomplete="new-password"
        />
      <select bind:value={nuRole}
       >
        <option value="user">{t("role.user")}</option>
        <option value="admin">{t("role.admin")}</option>
      </select>
      <Button tone="primary" onclick={addUser}>{t("users.add")}</Button>
      {#if userMsg}<span class="text-s text-dida-text-muted">{userMsg}</span>{/if}
    </div>
    <div class="flex flex-col gap-2.5">
      {#each users as u (u.id)}
        {@const isUser = u.role === "user"}
        <div class="rounded-lg border border-dida-border bg-dida-panel-2/30 p-3">
          <!-- header: name · role · last login -->
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span class="font-semibold">{u.username}</span>
            <select value={u.role} onchange={(e) => changeRole(u, e.currentTarget.value)}
             >
              <option value="user">{t("role.user")}</option>
              <option value="admin">{t("role.admin")}</option>
            </select>
            <span class="ml-auto text-s text-dida-text-faint">
              {u.last_login_at ? dateTime(u.last_login_at) : t("users.never")}
            </span>
          </div>

          {#if isUser}
            <!-- visible pages -->
            <div class="mt-3">
              <p class="mb-1.5 text-s font-medium text-dida-text-muted">{t("users.pages")}</p>
              <div class="flex flex-wrap gap-x-4 gap-y-1.5">
                {#each CONSUMER_PAGES as pg (pg.key)}
                  <label class="inline-flex items-center gap-1.5 text-m text-dida-text-muted">
                    <input type="checkbox" checked={pagesFor(u).has(pg.key)} class="accent-dida-accent"
                      onchange={(e) => togglePage(u, pg.key, e.currentTarget.checked)} />
                    {t(pg.nav)}
                  </label>
                {/each}
              </div>
            </div>
          {/if}

          <!-- actions -->
          <div class="mt-3 flex flex-wrap items-center gap-2">
            {#if resetId === u.id}
              <input type="password" bind:value={resetPw} placeholder={t("users.newPw")} autocomplete="new-password"
                class="w-48" />
              <Button tone="primary" size="small" onclick={() => doResetPw(u.id)} disabled={resetPw.length < 8}>{t("common.save")}</Button>
              <Button size="small" onclick={() => { resetId = null; resetPw = ""; }}>{t("common.cancel")}</Button>
            {:else}
              {#if isUser}
                <Button size="small"
                  onclick={() => { expandedId = expandedId === u.id ? null : u.id; addRef = ""; addViewRef = ""; }}>
                  {t("users.control")} {expandedId === u.id ? "▴" : "▾"}
                </Button>
              {/if}
              <Button size="small" onclick={() => openAccess(u)}>
                {t("users.setupPhone")} {accId === u.id ? "▴" : "▾"}
              </Button>
              <Button size="small" onclick={() => { resetId = u.id; resetPw = ""; }}>{t("users.resetPw")}</Button>
              <Button tone="danger" size="small" onclick={() => removeUser(u)}>{t("common.delete")}</Button>
            {/if}
          </div>

          {#if expandedId === u.id && isUser}
            <div class="mt-3 flex flex-col gap-3 border-t border-dida-border/50 pt-3">
              <!-- control baseline -->
              <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-m">
                <span class="font-medium">{t("users.control")}</span>
                <label class="inline-flex items-center gap-1.5">
                  <input type="radio" name="ctl-{u.id}" checked={u.can_control} class="accent-dida-accent"
                    onchange={() => saveControl(u, { can_control: true })} />
                  {t("users.controlAll")}
                </label>
                <label class="inline-flex items-center gap-1.5">
                  <input type="radio" name="ctl-{u.id}" checked={!u.can_control} class="accent-dida-accent"
                    onchange={() => saveControl(u, { can_control: false })} />
                  {t("users.controlNone")}
                </label>
              </div>
              <!-- control exceptions -->
              <div>
                <p class="mb-1.5 text-s text-dida-text-muted">{u.can_control ? t("users.denyList") : t("users.allowList")}</p>
                <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  {#each u.control_rules as r (r.scope + ":" + r.ref)}
                    <span class="inline-flex items-center gap-1 text-s">
                      <span class="text-dida-text-faint">{t(`users.scope.${r.scope}`)}:</span>
                      <span class="font-medium">{refLabel(r)}</span>
                      <Button size="small" label={t("common.delete")} title={t("common.delete")} onclick={() => removeRule(u, r)}>✕</Button>
                    </span>
                  {:else}
                    <span class="text-s text-dida-text-faint">{t("users.noExceptions")}</span>
                  {/each}
                </div>
                <div class="flex flex-wrap items-center gap-1.5">
                  <select bind:value={addScope} onchange={() => (addRef = "")}
                   >
                    <option value="entity">{t("users.scope.entity")}</option>
                    <option value="area">{t("users.scope.area")}</option>
                    <option value="capability">{t("users.scope.capability")}</option>
                  </select>
                  {#if addScope === "entity"}
                    <div class="w-72 max-w-full"><EntityPicker bind:value={addRef} items={pickerItems} /></div>
                  {:else}
                    <select bind:value={addRef}
                      class="max-w-[16rem]">
                      <option value="" disabled>{t("users.pickValue")}</option>
                      {#each refOptions(addScope) as opt (opt.value)}
                        <option value={opt.value}>{opt.label}</option>
                      {/each}
                    </select>
                  {/if}
                  <Button tone="primary" size="small" onclick={() => addRule(u)} disabled={!addRef}>{t("users.addException")}</Button>
                </div>
              </div>
              <!-- view hides -->
              <div class="border-t border-dida-border/40 pt-3">
                <p class="mb-1.5 text-s text-dida-text-muted">{t("users.hideList")}</p>
                <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  {#each u.view_hides as r (r.scope + ":" + r.ref)}
                    <span class="inline-flex items-center gap-1 text-s">
                      <span class="text-dida-text-faint">{t(`users.scope.${r.scope}`)}:</span>
                      <span class="font-medium">{refLabel(r)}</span>
                      <Button size="small" label={t("common.delete")} title={t("common.delete")} onclick={() => removeHide(u, r)}>✕</Button>
                    </span>
                  {:else}
                    <span class="text-s text-dida-text-faint">{t("users.noExceptions")}</span>
                  {/each}
                </div>
                <div class="flex flex-wrap items-center gap-1.5">
                  <select bind:value={addViewScope} onchange={() => (addViewRef = "")}
                   >
                    <option value="entity">{t("users.scope.entity")}</option>
                    <option value="area">{t("users.scope.area")}</option>
                    <option value="capability">{t("users.scope.capability")}</option>
                  </select>
                  {#if addViewScope === "entity"}
                    <div class="w-72 max-w-full"><EntityPicker bind:value={addViewRef} items={pickerItems} /></div>
                  {:else}
                    <select bind:value={addViewRef}
                      class="max-w-[16rem]">
                      <option value="" disabled>{t("users.pickValue")}</option>
                      {#each refOptions(addViewScope) as opt (opt.value)}
                        <option value={opt.value}>{opt.label}</option>
                      {/each}
                    </select>
                  {/if}
                  <Button tone="primary" size="small" onclick={() => addHide(u)} disabled={!addViewRef}>{t("users.addException")}</Button>
                </div>
              </div>
            </div>
          {/if}

          {#if accId === u.id}
            <div class="mt-3 border-t border-dida-border/50 pt-3">
              {#if accBusy && !accSetup}
                <p class="text-s text-dida-text-faint">…</p>
              {:else if accSetup}
                {#if !accSetup.reachable}
                  <div class="mb-3"><Notice tone="warn">{t("users.accessNotReachable")}</Notice></div>
                {/if}
                <p class="mb-3 text-s text-dida-text-muted">{t("users.setupScan")}</p>
                <div class="grid gap-4 sm:grid-cols-2">
                  <!-- Android app: one QR → guided single-button setup. -->
                  <div class="flex gap-3">
                    <div class="shrink-0 self-start rounded-lg bg-white p-2 [&>svg]:block [&>svg]:h-32 [&>svg]:w-32">
                      {@html accSetup.qr_svg_setup}
                    </div>
                    <div class="min-w-0">
                      <p class="text-m font-medium">{t("users.qrSetupTitle")}</p>
                      <p class="text-s text-dida-text-muted">{t("users.qrSetupHint")}</p>
                    </div>
                  </div>
                  <!-- Web only: guests / iPhone. -->
                  <div class="flex gap-3">
                    <div class="shrink-0 self-start rounded-lg bg-white p-2 [&>svg]:block [&>svg]:h-32 [&>svg]:w-32">
                      {@html accSetup.qr_svg}
                    </div>
                    <div class="min-w-0">
                      <p class="text-m font-medium">{t("users.qrLoginTitle")}</p>
                      <p class="text-s text-dida-text-muted">{t("users.qrLoginHint")}</p>
                    </div>
                  </div>
                </div>
                <div class="mt-3 flex flex-wrap items-center gap-2">
                  <Button size="small" onclick={copyAccessLink}>
                    {accCopied ? t("users.locationCopied") : t("users.locationCopyLink")}
                  </Button>
                  <Button size="small" onclick={() => rotateAccess(u)} disabled={accBusy}>
                    {t("users.accessRotate")}
                  </Button>
                  <Button tone="danger" size="small" onclick={() => revokeAccess(u)}>{t("users.accessRevoke")}</Button>
                </div>
              {/if}
            </div>
          {/if}

        </div>
      {/each}
    </div>
  </Card>
{/if}
