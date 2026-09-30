<script lang="ts">
  import { onMount } from "svelte";
  import { Button, Notice, PageHead, SaveButton, dialog, formatNumber } from "$lib/kit";
  import { api, type AdapterCfg, type BabaSite, type EsphomeNode, type FrigateSite, type HomekitDevice, type RuntimeState, type TuyaCloudDevice } from "$lib/api";
  import { errMsg } from "$lib/errors";
  import { devices } from "$lib/store.svelte";
  import { groupDevices } from "$lib/devices";
  import CapabilityControl from "$lib/CapabilityControl.svelte";
  import ZigbeeTools from "$lib/ZigbeeTools.svelte";
  import CloudflareRoutes from "$lib/CloudflareRoutes.svelte";
  import { capLabel, capRank, DEVICE_TYPES, typeLabel } from "$lib/capabilities";
  import { SECTION_TITLE_CLASS, SUBSECTION_TITLE_CLASS } from "$lib/ui";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { tr } from "$lib/translations.svelte";
  import { auth } from "$lib/auth.svelte";

  let cfgs = $state<AdapterCfg[]>([]);
  let form = $state<Record<string, Record<string, string>>>({});
  let formInit = $state<Record<string, Record<string, string>>>({}); // saved snapshot → dirty check
  let busy = $state<string | null>(null);
  let msg = $state<Record<string, string>>({});
  let loadErr = $state<string | null>(null);

  // Fleet totals for the page header (moved off the retired Status page). People
  // (tracked presence — the `location` capability) are split OUT of the device
  // count, since a person isn't a device. Devices are counted as PHYSICAL units
  // (groupDevices merges an adapter's many entities into one card, exactly as the
  // Devices page shows them) — not raw entities, which run to the hundreds (one
  // ESPHome node alone exposes ~13). Capabilities are internal, so not surfaced.
  const isPerson = (d: (typeof devices.list)[number]) => d.capabilities.includes("location");
  const totalPeople = $derived(devices.list.filter(isPerson).length);
  const totalDevices = $derived(groupDevices(devices.list.filter((d) => !isPerson(d))).length);
  const totalAdapters = $derived(new Set(devices.list.map((d) => d.adapter)).size);

  // A Save button is enabled only when its input differs from the last-saved value.
  function dirty(adapter: string): boolean {
    return JSON.stringify(form[adapter]) !== JSON.stringify(formInit[adapter]);
  }

  // Where the Zigbee console answers, straight from the server: it knows this host's
  // address and the port the console is published on, and neither belongs hard-coded
  // in a frontend that would then be a third place to get it wrong. Filled by
  // loadAnnLang(), which already reads settings — one fetch, not two.
  let zigbeeConsole = $state("");

  async function load() {
    try {
      cfgs = await api.listAdapters();
      const f: Record<string, Record<string, string>> = {};
      for (const c of cfgs) {
        f[c.adapter] = {};
        for (const fld of c.fields) f[c.adapter][fld.key] = fld.secret ? "" : fld.value;
      }
      form = f;
      formInit = structuredClone(f);
      loadErr = null;
    } catch (e) {
      loadErr = errMsg(e);
    }
  }
  async function save(adapter: string) {
    busy = adapter;
    msg = { ...msg, [adapter]: "" };
    try {
      await api.updateAdapterConfig(adapter, form[adapter]);
      msg = { ...msg, [adapter]: t("settings.saved") };
      await load();
      // A card whose Connect/Pair button is gated on saved credentials has to be
      // told they landed: the gate reads a status fetched when the page opened,
      // which still says "not configured", and the button stays dead with the
      // fields filled in right above it.
      if (adapter === "androidtv") loadAtvStatus(); // saved host → enable Pair
      if (adapter === "smartthings") loadStStatus(); // saved credentials → enable Connect
      if (adapter === "contacts") loadCtStatus();
      // The adapter reconnects on its own poll; catch the connect result (a badge
      // moving connecting → ok/error) with a couple of delayed status refreshes.
      setTimeout(refreshStatuses, 2500);
      setTimeout(refreshStatuses, 7000);
    } catch (e) {
      msg = { ...msg, [adapter]: errMsg(e) };
    } finally {
      busy = null;
    }
  }
  // Refresh ONLY the connection-status badges — never touches `form`, so it's
  // safe to poll while a card is open (a full load() would clobber unsaved edits).
  async function refreshStatuses() {
    try {
      const fresh = await api.listAdapters();
      const byName = new Map(fresh.map((c) => [c.adapter, c.status]));
      cfgs = cfgs.map((c) => ({ ...c, status: byName.get(c.adapter) ?? null }));
      loadAtvStatus(); // keep the Android TV paired/host state in sync too
    } catch {
      /* non-fatal */
    }
  }
  // --- HomeKit: interactive pairing (discover on the LAN, pair by setup code) ---
  let hkDevices = $state<HomekitDevice[]>([]);
  let hkLoading = $state(false);
  let hkPins = $state<Record<string, string>>({});
  let hkNames = $state<Record<string, string>>({});
  let hkBusy = $state<string | null>(null);
  let hkMsg = $state("");

  // Accordion: at most one adapter open (null = all collapsed, the default).
  let expandedKey = $state<string | null>(null);

  // --- one global sweep across every discoverable adapter; results land in each
  //     adapter's card as "found, not added yet" (no per-adapter scan button) ---
  let foundAll = $state<Record<string, import("$lib/api").DiscoveredDevice[]>>({});
  let scanningAll = $state(false);
  let scannedAll = $state(false);
  let scanAllMsg = $state("");
  let failedAll = $state<Record<string, string>>({});
  let addingKey = $state<string | null>(null);
  const foundCount = $derived(Object.values(foundAll).reduce((n, a) => n + a.length, 0));
  async function scanAll() {
    scanningAll = true;
    scanAllMsg = "";
    try {
      const r = await api.discoverAll();
      foundAll = r.found;
      failedAll = r.failed;
      scannedAll = true;
    } catch (e) {
      scanAllMsg = errMsg(e);
    } finally {
      scanningAll = false;
    }
  }
  // Add every found device for an adapter — sequential so the server-side append
  // never races the config blob (each add reads-then-writes it).
  async function addAllFound(adapter: string) {
    for (const d of [...(foundAll[adapter] ?? [])]) await addFound(adapter, d);
  }
  // Rescan ONE adapter (its toolbar button) — found devices land in its card.
  let rescanning = $state<string | null>(null);
  async function rescanAdapter(adapter: string) {
    rescanning = adapter;
    try {
      const r = await api.discoverAdapter(adapter);
      foundAll = { ...foundAll, [adapter]: r.error ? [] : (r.devices ?? []) };
      if (r.error) scanAllMsg = r.error;
    } catch (e) {
      scanAllMsg = errMsg(e);
    } finally {
      rescanning = null;
    }
  }
  async function addFound(adapter: string, d: import("$lib/api").DiscoveredDevice) {
    addingKey = adapter + d.label;
    try {
      await api.applyDiscovered(adapter, d); // server-side append — never clobbers earlier adds
      foundAll = { ...foundAll, [adapter]: (foundAll[adapter] ?? []).filter((x) => x !== d) };
      if (adapter === "esphome") await loadNodes(); // show the new node as a card
    } catch (e) {
      scanAllMsg = errMsg(e);
    } finally {
      addingKey = null;
    }
  }

  async function hkDiscover() {
    hkLoading = true;
    hkMsg = "";
    try {
      const r = await api.homekitDiscovered();
      hkDevices = r.devices ?? [];
      if (!hkDevices.length) hkMsg = t("homekit.none");
    } catch (e) {
      hkMsg = errMsg(e);
    } finally {
      hkLoading = false;
    }
  }
  async function hkPair(d: HomekitDevice) {
    hkBusy = d.device_id;
    hkMsg = "";
    try {
      const r = await api.homekitPair(d.device_id, hkPins[d.device_id] ?? "", hkNames[d.device_id] ?? d.name);
      hkMsg = `${t("homekit.paired")} ${r.name}`;
      hkPins[d.device_id] = "";
      await hkDiscover();
    } catch (e) {
      hkMsg = errMsg(e);
    } finally {
      hkBusy = null;
    }
  }

  // --- SmartThings: OAuth connect (Samsung appliances/AV via the cloud) ------
  let stStatus = $state<{ configured: boolean; connected: boolean } | null>(null);
  let stLocations = $state<{ id: string; name: string }[]>([]);
  let stBusy = $state(false);
  async function loadStStatus() {
    if (!auth.isAdmin) return;
    try {
      stStatus = await api.smartthingsStatus();
      stLocations = stStatus?.connected ? (await api.smartthingsLocations()).locations : [];
    } catch {
      stStatus = null;
    }
  }
  async function stConnect() {
    stBusy = true;
    try {
      const { url } = await api.smartthingsLogin(); // top-level redirect → cloud → /callback
      window.location.href = url;
    } catch (e) {
      msg = { ...msg, smartthings: errMsg(e) };
      stBusy = false;
    }
  }
  async function stDisconnect() {
    if (!(await dialog.confirm({ title: t("smartthings.disconnect"), message: t("smartthings.disconnectConfirm"), danger: true }))) return;
    stBusy = true;
    try {
      await api.smartthingsDisconnect();
      await loadStStatus();
    } catch (e) {
      msg = { ...msg, smartthings: errMsg(e) };
    } finally {
      stBusy = false;
    }
  }

  // --- Contacts: the household address book (Google People API) ---------------
  // DIDA is the house's one reader of the book. Announcing is opted into per
  // person: the book holds everyone, the kitchen speaker is for the household.
  let ctStatus = $state<Awaited<ReturnType<typeof api.contactsStatus>> | null>(null);
  let ctPeople = $state<Awaited<ReturnType<typeof api.contactsPeople>>>([]);
  let ctFilter = $state("");
  let ctBusy = $state(false);
  // Accent- and case-blind, like the server-side match: typing "Boskovic" has to
  // find "Bošković", or the filter is useless on exactly the names it is for.
  const flat = (s: string) =>
    s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/[đĐ]/g, "d").toLowerCase();
  // The announced window in words, from the adapter's own setting — so the card
  // cannot promise "and the day before" while the setting says otherwise.
  const ctWindow = $derived.by(() => {
    const n = Number(form["contacts"]?.announce_days ?? 1);
    if (!Number.isFinite(n) || n <= 0) return t("contacts.window0");
    return n === 1 ? t("contacts.window1") : t("contacts.windowN", { days: n });
  });
  const ctShown = $derived(
    ctPeople.filter((p) => !ctFilter.trim() || flat(p.name).includes(flat(ctFilter))),
  );
  // The adapter stamps the report in the HOUSE's zone (with its offset), so this
  // only has to render it — never re-interpret it as the browser's.
  const ctWhen = (iso: string) => dateTime(iso);
  async function loadCtStatus() {
    if (!auth.isAdmin) return;
    try {
      ctStatus = await api.contactsStatus();
      ctPeople = await api.contactsPeople();
    } catch {
      ctStatus = null;
    }
  }
  async function ctConnect() {
    ctBusy = true;
    try {
      const { url } = await api.contactsLogin(); // top-level redirect → Google → /callback
      window.location.href = url;
    } catch (e) {
      msg = { ...msg, contacts: errMsg(e) };
      ctBusy = false;
    }
  }
  async function ctDisconnect() {
    if (!(await dialog.confirm({ title: t("contacts.disconnect"), message: t("contacts.disconnectConfirm"), danger: true }))) return;
    ctBusy = true;
    try {
      await api.contactsDisconnect();
      await loadCtStatus();
    } catch (e) {
      msg = { ...msg, contacts: errMsg(e) };
    } finally {
      ctBusy = false;
    }
  }
  async function ctToggle(person: { id: number; announce: boolean }) {
    try {
      await api.contactsSetAnnounce(person.id, !person.announce);
      await loadCtStatus();
    } catch (e) {
      msg = { ...msg, contacts: errMsg(e) };
    }
  }
  async function ctImport(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    ctBusy = true;
    try {
      const report = await api.contactsImport(await file.text());
      msg = { ...msg, contacts: t("contacts.imported", { added: report.added, updated: report.updated }) };
      await loadCtStatus();
    } catch (e) {
      msg = { ...msg, contacts: errMsg(e) };
    } finally {
      ctBusy = false;
    }
  }

  // --- Announce (TTS): default spoken-language + per-speaker test. Lives in this
  // adapter's card (was a standalone /settings/tts page). announce_lang is a global
  // app-setting; the speaker list is the auto-discovered announce:* entities. -----
  const ANN_LANGS = ["hr", "en", "de", "it", "sl", "es", "fr"];
  let annLangSaved = $state("hr");
  let annLang = $state("hr");
  let annSaving = $state(false);
  let annTesting = $state<string | null>(null);
  async function loadAnnLang() {
    if (!auth.isAdmin) return;
    try {
      const s = await api.getSettings();
      annLangSaved = s?.announce_lang ?? "hr";
      annLang = annLangSaved;
      zigbeeConsole = s?.zigbee_console_url ?? "";
    } catch { /* announce adapter / settings unavailable */ }
  }
  async function saveAnnLang() {
    annSaving = true;
    try { await api.updateSettings({ announce_lang: annLang }); annLangSaved = annLang; }
    catch (e) { msg = { ...msg, announce: errMsg(e) }; }
    finally { annSaving = false; }
  }
  async function annTest(entityId: string) {
    if (!(await dialog.confirm({ title: t("tts.testTitle"), message: t("tts.testBody"), confirmLabel: t("tts.test") }))) return;
    annTesting = entityId;
    try {
      await api.sendCommand({ entity_id: entityId, capability: "announce", command: "say",
        args: { value: t("tts.testPhrase"), language: annLang } });
    } catch (e) { msg = { ...msg, announce: errMsg(e) }; }
    finally { annTesting = null; }
  }
  // Announce targets = registered announce:* entities (auto-discovered from every
  // Cast/DLNA/HEOS/Volumio speaker by the announce adapter, plus any CSV aliases).
  const annTargets = $derived(devices.list.filter((d) => d.entityId.startsWith("announce:")));

  // --- Android TV over ADB: connect (one-time "Allow debugging?" prompt on TV) --
  let atvStatus = $state<{ connected: boolean; host: string; apps: number } | null>(null);
  let atvBusy = $state(false);
  let atvMsg = $state("");
  async function loadAtvStatus() {
    if (!auth.isAdmin) return;
    try {
      atvStatus = await api.androidtvStatus();
    } catch {
      atvStatus = null; // adapter not running / not enabled
    }
  }
  async function atvConnect() {
    atvBusy = true;
    atvMsg = t("androidtv.connecting");
    try {
      await api.androidtvConnect();
      // The connect finishes ON the device (accept the prompt) — poll status a few
      // times so the badge + pulled-app list refresh once it lands.
      for (const d of [2500, 6000, 10000]) setTimeout(loadAtvStatus, d);
      setTimeout(load, 10500); // reload config → the editor shows the pulled apps
    } catch (e) {
      atvMsg = errMsg(e);
    } finally {
      atvBusy = false;
    }
  }

  // --- Android TV launchable-app list editor (the list is PULLED off the device,
  // then annotated here). Mirrored into form["androidtv"].apps (a JSON string) so
  // the existing dirty-tracking + Save button persist the rename / show-hide.
  type AtvApp = { name: string; link: string; hidden: boolean };
  function parseAtvApps(raw: string | undefined): AtvApp[] {
    if (!raw || !raw.trim()) return [];
    try {
      const d = JSON.parse(raw);
      if (!Array.isArray(d)) return [];
      return d
        .filter((a) => a && a.name && a.link)
        .map((a) => ({ name: String(a.name), link: String(a.link), hidden: !!a.hidden }));
    } catch {
      return [];
    }
  }
  let atvAppList = $state<AtvApp[]>([]);
  // Re-seed from the SAVED snapshot: runs on initial load and after each save
  // (formInit changes only then), never clobbering in-progress edits.
  $effect(() => {
    atvAppList = parseAtvApps(formInit["androidtv"]?.apps);
  });
  function atvSyncApps() {
    form = { ...form, androidtv: { ...(form["androidtv"] ?? {}), apps: JSON.stringify(atvAppList) } };
  }
  function atvAddApp() {
    atvAppList = [...atvAppList, { name: "", link: "", hidden: false }];
    atvSyncApps();
  }
  function atvRemoveApp(i: number) {
    atvAppList = atvAppList.filter((_, j) => j !== i);
    atvSyncApps();
  }
  function atvToggleApp(i: number, show: boolean) {
    atvAppList[i].hidden = !show;
    atvSyncApps();
  }
  function atvEditApp(i: number, key: "name" | "link", value: string) {
    atvAppList[i][key] = value;
    atvSyncApps();
  }

  onMount(() => {
    load();
    loadRuntime();
    loadNodes();
    loadStStatus();
    loadCtStatus();
    loadAtvStatus();
    loadAnnLang();
    // The OAuth callbacks redirect back here with ?<adapter>=connected|error.
    const q = new URLSearchParams(window.location.search);
    const r = q.get("smartthings");
    if (r) {
      msg = {
        ...msg,
        smartthings: r === "connected"
          ? t("smartthings.connected")
          : `${t("smartthings.errConnect")}${q.get("reason") ? ` (${q.get("reason")})` : ""}`,
      };
      expandedKey = "smartthings";
      history.replaceState(null, "", window.location.pathname);
      setTimeout(loadStStatus, 1500);
    }
    const rc = q.get("contacts");
    if (rc) {
      msg = {
        ...msg,
        contacts: rc === "connected"
          ? t("contacts.connected")
          // "offline" is Google having returned no refresh token — the grant is an
          // hour long and unrenewable, which is the failure worth naming outright.
          : rc === "error" && q.get("reason") === "offline"
            ? t("contacts.errOffline")
            : `${t("contacts.errConnect")}${q.get("reason") ? ` (${q.get("reason")})` : ""}`,
      };
      expandedKey = "contacts";
      history.replaceState(null, "", window.location.pathname);
      setTimeout(loadCtStatus, 1500);
    }
  });


  // --- what this installation actually runs -------------------------------------
  // An adapter is a compose profile of the same name. Off means its container does
  // not exist at all, so the page lists it apart from the ones in use instead of
  // presenting a catalogue of hardware nobody here owns.
  let runtime = $state<RuntimeState | null>(null);
  let toggling = $state<string | null>(null);

  async function loadRuntime() {
    try { runtime = await api.adapterRuntime(); } catch { runtime = null; }
  }
  const profileOf = (adapter: string) => runtime?.profiles?.[adapter] ?? null;
  // No profile = always on (astro, notify, announce, and the core services).
  const isOff = (adapter: string) => profileOf(adapter)?.enabled === false;

  async function setRunning(profile: string, enabled: boolean) {
    if (!enabled && !(await dialog.confirm({
      title: t("runtime.stopTitle", { name: profile }),
      message: t("runtime.stopBody"),
      confirmLabel: t("runtime.stop"),
      danger: true,
    }))) return;
    toggling = profile;
    try {
      await api.setAdapterRuntime(profile, enabled);
      // The containers come up in the background — poll until the runner is idle,
      // then reload the cards so status badges reflect the new process set.
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        await loadRuntime();
        if (!runtime?.applying) break;
      }
      await load();
    } catch (e) {
      msg = { ...msg, [profile]: errMsg(e) };
    } finally {
      toggling = null;
    }
  }

  // Adapters grouped by how they're set up (from the API `category`), with the
  // switched-off ones lifted out of those groups entirely.
  const liveCfgs = $derived(cfgs.filter((c) => !isOff(c.adapter)));
  const discoverCfgs = $derived(liveCfgs.filter((c) => c.category === "discover"));
  const autoCfgs = $derived(liveCfgs.filter((c) => c.category === "auto"));
  const accountCfgs = $derived(liveCfgs.filter((c) => c.category === "account"));
  const offCfgs = $derived(cfgs.filter((c) => isOff(c.adapter)));
  // Profiles that aren't adapters but are switched the same way (the zigbee
  // bridge, the ESPHome build dashboard, the Matter bridge).
  // Anything added later falls back to its profile name rather than a blank row.
  const svcLabel = (name: string): string =>
    name === "zigbee" ? t("runtime.svc.zigbee")
      : name === "esphome-dashboard" ? t("runtime.svc.esphomeDashboard")
        : name === "matter" ? t("runtime.svc.matter")
          : name === "cloudflare-tunnel" ? t("runtime.svc.cloudflareTunnel")
            : name;
  const serviceProfiles = $derived(
    Object.entries(runtime?.profiles ?? {})
      .filter(([name]) => !cfgs.some((c) => c.adapter === name))
      // `zigbee` is the z2m container, and it is Zigbee: it lives on the Zigbee /
      // MQTT card with the bridge tools and the console link, not in a services
      // list a page away. Somebody looking for Zigbee opens the card called Zigbee.
      .filter(([name]) => name !== "zigbee")
      .sort(([a], [b]) => a.localeCompare(b, "hr")),
  );
  // The z2m container's own on/off state, now shown on that card.
  const zigbeeProfile = $derived(runtime?.profiles?.zigbee ?? null);
  // The console asks for its token once per browser. Fetched only on the press —
  // and shown as well as copied, because navigator.clipboard is unavailable over
  // plain http, which is exactly how this console is opened.
  let zigbeeToken = $state("");
  let zigbeeTokenMsg = $state("");
  async function copyZigbeeToken() {
    try {
      const { token } = await api.zigbeeToken();
      zigbeeToken = token;
      try {
        await navigator.clipboard.writeText(token);
        zigbeeTokenMsg = t("zigbee.tokenCopied");
      } catch {
        zigbeeTokenMsg = t("zigbee.tokenShown");
      }
    } catch (e) {
      zigbeeTokenMsg = errMsg(e);
    }
  }
  // Groups are by KIND, not by state: a device adapter that's switched off still
  // belongs with the devices (compact row + Switch on), never in a separate "not
  // running" bucket. Each row carries its own state, so there's one place to look
  // for a thing whether it's up or not.
  const deviceOff = $derived(offCfgs.filter((c) => c.category === "discover" || c.category === "auto"));
  const accountOff = $derived(offCfgs.filter((c) => c.category === "account"));

  // Compact filter (header): narrow every group at once by adapter name or id.
  // The list is bounded, so this is a find-by-name convenience, not paging.
  let adapterFilter = $state("");
  const fq = $derived(adapterFilter.trim().toLowerCase());
  const matchCfg = (c: AdapterCfg) =>
    !fq || adapterLabel(c).toLowerCase().includes(fq) || c.adapter.toLowerCase().includes(fq);
  const matchSvc = (name: string) =>
    !fq || svcLabel(name).toLowerCase().includes(fq) || name.toLowerCase().includes(fq);
  const devicesLive = $derived([...discoverCfgs, ...autoCfgs].filter(matchCfg));
  const deviceOffF = $derived(deviceOff.filter(matchCfg));
  const accountLive = $derived(accountCfgs.filter(matchCfg));
  const accountOffF = $derived(accountOff.filter(matchCfg));
  const servicesF = $derived(serviceProfiles.filter(([name]) => matchSvc(name)));
  const shownTotal = $derived(
    discoverCfgs.length + autoCfgs.length + accountCfgs.length
    + deviceOff.length + accountOff.length + serviceProfiles.length);
  const shownVisible = $derived(
    devicesLive.length + deviceOffF.length + accountLive.length + accountOffF.length + servicesF.length);

  // --- per-device field toggles (expose/hide) shown inside each adapter card ---
  // Metadata capabilities are companions (options lists), not user-facing fields.
  const META_CAPS = new Set([
    "number_options", "enum_options", "effect_options", "source_options",
    "hvac_mode_options", "fan_mode_options", "vacuum_mode_options",
    // media_display is a static "this player has a screen" marker, not a field.
    "media_display",
  ]);
  const realCaps = (caps: string[]) => caps.filter((c) => !META_CAPS.has(c));
  const EMPTY_CS = { value: false, unit: null, updatedAt: 0 }; // placeholder for a field with no state yet (e.g. a press button)
  // Options for an enum-like control, read from the field's <cap>_options metadata.
  function fieldOptions(f: (typeof devices.list)[number], cap: string): string[] {
    const optCap = cap === "enum" ? "enum_options" : cap === "effect" ? "effect_options"
      : cap === "source" ? "source_options"
      : cap === "hvac_mode" ? "hvac_mode_options" : cap === "fan_mode" ? "fan_mode_options"
      : cap === "vacuum_mode" ? "vacuum_mode_options" : null;
    const raw = optCap ? f.caps[optCap]?.value : null;
    if (typeof raw !== "string") return [];
    try { const a = JSON.parse(raw); return Array.isArray(a) ? a.map(String) : []; } catch { return []; }
  }
  // Device value range for a `number` field, from its number_options metadata.
  function fieldRange(f: (typeof devices.list)[number], cap: string) {
    if (cap !== "number") return undefined;
    const raw = f.caps["number_options"]?.value;
    if (typeof raw !== "string") return undefined;
    try {
      const o = JSON.parse(raw);
      return { min: o.min ?? undefined, max: o.max ?? undefined, step: o.step ?? undefined };
    } catch { return undefined; }
  }
  // Commandable capabilities → a field is a "control"; the rest are sensors.
  const CONTROL_CAPS = new Set([
    "on_off", "brightness", "color_temp", "color_rgb", "effect", "open_close", "lock",
    "boolean", "enum", "number", "hvac_mode", "target_temperature", "fan_mode", "fan_speed",
    "press", "source", "volume", "mute", "media_transport", "announce", "mower", "vacuum",
  ]);
  // Voice-assistant eligibility: the Matter bridge exposes entities carrying on_off
  // (lights/switches) or hvac_mode (climate). Only those get a voice (🎙) toggle.
  const VOICE_CAPS = new Set(["on_off", "hvac_mode"]);
  const voiceEligible = (f: (typeof devices.list)[number]) =>
    realCaps(f.capabilities).some((c) => VOICE_CAPS.has(c));
  // Group one adapter's entities into devices (by device_key) → their fields.
  function adapterDevices(adapter: string) {
    const groups = new Map<string, { key: string; name: string; fields: typeof devices.list }>();
    for (const d of devices.list) {
      if (d.adapter !== adapter) continue;
      const key = d.deviceKey ?? d.entityId;
      if (!groups.has(key)) groups.set(key, { key, name: devices.deviceLabel(key) ?? d.deviceKey ?? d.name, fields: [] });
      groups.get(key)!.fields.push(d);
    }
    for (const g of groups.values()) g.fields.sort((a, b) => a.name.localeCompare(b.name, "hr"));
    return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name, "hr"));
  }
  // The adapter's editable config fields, minus the two that get a bespoke UI
  // elsewhere (tuya raw config blob, androidtv apps list). Rendered in order; a
  // field's `group` starts a subheading when it differs from the previous one.
  function cfgFields(c: AdapterCfg) {
    return c.fields.filter(
      (fld) =>
        !(c.adapter === "tuya" && fld.key === "config") &&
        !(c.adapter === "androidtv" && fld.key === "apps") &&
        !(["frigate", "baba"].includes(c.adapter) && fld.key === "sites"), // sites get a per-location editor
    );
  }
  // Candidates for an `entity` config field: the adapter's own devices, kept only
  // if exposed (respect the same curation as the rest of the UI) and — when the
  // field requires a capability (entity_cap, e.g. media_display) — only devices
  // that advertise it. So a hidden/removed device or a screenless speaker drops out.
  function entityOptions(adapter: string, cap: string) {
    return adapterDevices(adapter).filter(
      (g) =>
        g.fields.some((f) => f.exposed) &&
        (!cap || g.fields.some((f) => f.capabilities.includes(cap))),
    );
  }
  // Split a device's fields into Controls / Settings / Sensors / Diagnostics /
  // Unsupported. The adapter's `category` separates everyday controls from the
  // pile of configuration knobs (a TRV alone has ~17), so they don't drown the
  // important fields; the legacy `diagnostic` flag still folds untagged
  // technical entities into diagnostics.
  // The field's rank = its most-actuator capability's display order (capRank puts
  // switches/lights/covers/locks ahead of momentary `press` buttons). Lets the
  // controls bucket list all switches first, buttons last.
  function fieldRank(f: (typeof devices.list)[number]): number {
    const caps = realCaps(f.capabilities);
    return caps.length ? Math.min(...caps.map(capRank)) : Number.MAX_SAFE_INTEGER;
  }
  function fieldBuckets(fields: typeof devices.list) {
    const b: Record<string, typeof devices.list> = { controls: [], settings: [], sensors: [], diagnostic: [], unsupported: [] };
    for (const f of fields) {
      if (f.capabilities.length === 0) b.unsupported.push(f);
      else if (f.category === "config") b.settings.push(f);
      else if (f.category === "diagnostic" || f.diagnostic) b.diagnostic.push(f);
      else if (realCaps(f.capabilities).some((c) => CONTROL_CAPS.has(c))) b.controls.push(f);
      else b.sensors.push(f);
    }
    const byName = (p: (typeof fields)[number], q: (typeof fields)[number]) => p.name.localeCompare(q.name, "hr");
    // Controls: switches/lights before buttons (rank), alpha within; the rest
    // plain alphabetical.
    b.controls.sort((p, q) => fieldRank(p) - fieldRank(q) || byName(p, q));
    b.settings.sort(byName);
    b.sensors.sort(byName);
    b.diagnostic.sort(byName);
    b.unsupported.sort(byName);
    return (["controls", "settings", "sensors", "diagnostic", "unsupported"] as const)
      .map((k) => ({ key: k, label: t(`fields.${k}`), fields: b[k] }))
      .filter((g) => g.fields.length);
  }
  const nodeLabel = (node: EsphomeNode) =>
    devices.deviceLabel(node.key) ?? node.name ?? node.host;

  // Group-level expose toggle (turn a whole Sensors/Diagnostics group on/off).
  const toggleable = (fields: typeof devices.list) => fields.filter((f) => f.capabilities.length > 0);
  const groupAllOn = (fields: typeof devices.list) => {
    const tg = toggleable(fields); return tg.length > 0 && tg.every((f) => f.exposed);
  };
  const groupSomeOn = (fields: typeof devices.list) => toggleable(fields).some((f) => f.exposed);
  async function setGroupExposed(fields: typeof devices.list, exposed: boolean) {
    await Promise.all(toggleable(fields).filter((f) => f.exposed !== exposed)
      .map((f) => devices.setExposed(f.entityId, exposed)));
  }
  function indet(node: HTMLInputElement, on: boolean) {
    node.indeterminate = on;
    return { update: (v: boolean) => { node.indeterminate = v; } };
  }
  function focusEl(node: HTMLInputElement) { node.focus(); node.select(); }

  // Inline device rename (sets the device label). Pre-fills with the current
  // human name (label, else the generated one) so editing starts from it.
  let editKey = $state<string | null>(null);
  let editVal = $state("");
  function startEdit(key: string) { editKey = key; editVal = devices.deviceLabel(key) ?? ""; }
  async function saveEdit(key: string) {
    const k = key; editKey = null;
    await devices.renameDevice(k, editVal);
  }

  // Inline per-entity (per-gang) rename — sets the entity label; empty clears it
  // back to the adapter's own name (pre-fill starts from the current label).
  let editEntity = $state<string | null>(null);
  let editEntityVal = $state("");
  function startEntityEdit(f: (typeof devices.list)[number]) { editEntity = f.entityId; editEntityVal = f.label ?? ""; }
  async function saveEntityName(entityId: string) {
    const id = entityId, val = editEntityVal; editEntity = null;
    await devices.renameEntity(id, val);
  }

  // Human-friendly adapter names: a custom label (set via ✎) wins; else a built-in
  // brand/title-cased default so the header never shows a bare technical id.
  const ADAPTER_NAMES: Record<string, string> = {
    govee: "Govee", mqtt: "Zigbee / MQTT", unifi: "UniFi", homekit: "HomeKit",
    esphome: "ESPHome", dlna: "DLNA", heos: "HEOS", denon: "Denon / Marantz",
    baba: "BABA (NVR)", androidtv: "Android TV", samsungtv: "Samsung TV", opus: "OPUS", cloudflare: "Cloudflare",
  };
  const friendlyName = (adapter: string) =>
    ADAPTER_NAMES[adapter] || (adapter.charAt(0).toUpperCase() + adapter.slice(1));
  const adapterLabel = (c: AdapterCfg) => c.label || friendlyName(c.adapter);
  let editAdapter = $state<string | null>(null);
  let editAdapterVal = $state("");
  function startEditAdapter(c: AdapterCfg) { editAdapter = c.adapter; editAdapterVal = c.label ?? ""; }
  async function saveAdapterName(adapter: string) {
    const a = adapter, val = editAdapterVal.trim();
    editAdapter = null;
    try {
      await api.setAdapterName(a, val);
      cfgs = cfgs.map((c) => (c.adapter === a ? { ...c, label: val } : c));
    } catch (e) {
      msg = { ...msg, [a]: errMsg(e) };
    }
  }

  // Room per device (sets area on all its entities) + auto-match from the name.
  function deviceArea(key: string): number | null {
    const d = devices.list.find((x) => x.deviceKey === key && x.areaId != null);
    return d ? d.areaId : null;
  }
  // The device's type OVERRIDE ("" = auto, i.e. not overridden). Shown only when
  // its entities agree — a mixed device (per-gang types) reads as "" (the "—"
  // placeholder) here; the per-gang selects in the expanded view are then the
  // source of truth.
  function deviceKind(key: string): string {
    const dts = new Set(
      devices.list.filter((x) => x.deviceKey === key || x.entityId === key)
        .map((x) => x.deviceType).filter(Boolean) as string[],
    );
    return dts.size === 1 ? [...dts][0] : "";
  }
  // A "gang" = an entity driving a physical load (relay / dimmer / cover / lock) —
  // the thing whose type you'd override. Momentary buttons (Restart / Identify)
  // and sensors don't count, so a light that also exposes a Restart button isn't
  // mistaken for a multi-gang device.
  const GANG_CAPS = ["on_off", "open_close", "lock", "brightness"];
  function isGang(f: (typeof devices.list)[number]): boolean {
    return realCaps(f.capabilities).some((c) => GANG_CAPS.includes(c));
  }
  function areaName(a: (typeof devices.areas)[number]): string {
    return a.name ?? (a.kind ? t(`room.kind.${a.kind}` as Parameters<typeof t>[0]) : "—");
  }
  function suggestAreaId(name: string | null): number | null {
    if (!name) return null;
    const n = name.toLowerCase();
    // Prefer a proper-named room (Ada/Bea/Cleo), else fall back to the room KIND
    // (generic rooms have no name — "living-room-light" → the living room).
    return devices.areas.find((a) => a.name && n.includes(a.name.toLowerCase()))?.id
      ?? devices.areas.find((a) => a.kind && n.includes(a.kind.toLowerCase()))?.id
      ?? null;
  }
  // Per-node collapse in the esphome card (all collapsed by default).
  // Accordion across ALL device cards (esphome nodes + every adapter's devices):
  // opening one closes the rest. Holds the open device_key, or null.
  let openDevice = $state<string | null>(null);

  // --- ESPHome: node list is the config, edited card-by-card (no JSON) ---
  let esNodes = $state<EsphomeNode[]>([]);
  let adapterUp = $state(true); // esphome adapter answering the status request
  let enName = $state(""); let enHost = $state(""); let enPsk = $state("");
  let enBusy = $state(false);
  // Node list sorted by display name; header KPIs (configured / connected).
  const sortedNodes = $derived([...esNodes].sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b), "hr")));
  const esStats = $derived({
    total: esNodes.length,
    // "Online" per the adapter's live status; fall back to "has fields" only if the
    // adapter isn't answering (state absent) so the KPI is never blank.
    connected: esNodes.filter((n) => (n.state ? n.state === "online" : nodeFields(n.key).length > 0)).length,
  });
  // Auto-assign a room to a newly-seen node from its name, once (never overrides).
  const autoRoomed = new Set<string>();
  $effect(() => {
    for (const node of esNodes) {
      if (autoRoomed.has(node.key)) continue;
      if (deviceArea(node.key) != null) { autoRoomed.add(node.key); continue; } // already roomed
      // Match on the device NAME — it loads separately (via the devices table), so
      // wait for it rather than giving up: don't mark tried until we have a name.
      const label = devices.deviceLabel(node.key) ?? devices.deviceRawName(node.key);
      if (!label) continue;
      autoRoomed.add(node.key);
      const sug = suggestAreaId(label);
      if (sug != null) devices.setDeviceArea(node.key, sug);
    }
  });
  async function loadNodes() {
    try {
      const r = await api.esphomeNodes();
      esNodes = r.nodes;
      adapterUp = r.adapter_up;
    } catch { /* non-fatal */ }
  }
  // While the esphome card is open, refresh status so a node's badge moves
  // connecting → online (or → error) live, without a manual reload.
  $effect(() => {
    if (expandedKey !== "esphome") return;
    const id = setInterval(loadNodes, 5000);
    return () => clearInterval(id);
  });
  // Keep connection badges live while a card is open (status-only, form-safe).
  $effect(() => {
    if (expandedKey === null) return;
    const id = setInterval(refreshStatuses, 8000);
    return () => clearInterval(id);
  });

  // Per-node connection status → badge (dot colour + label + raw reason on hover).
  type Tone = "ok" | "warn" | "bad" | "idle";
  function nodeStatus(n: EsphomeNode): { tone: Tone; label: string; title: string } {
    if (!adapterUp) return { tone: "idle", label: t("esphome.adapterDown"), title: "" };
    const s = n.state ?? "connecting";
    if (s === "online") return { tone: "ok", label: "", title: "" };
    if (s === "connecting") return { tone: "warn", label: t("esphome.connecting"), title: "" };
    if (s === "offline") return { tone: "idle", label: t("esphome.offline"), title: "" };
    // error: localize the code, keep the raw adapter message as the hover title
    const code = (n.code ?? "error") as "encryption" | "unresolved" | "unreachable" | "error";
    return { tone: "bad", label: t(`esphome.err.${code}` as Parameters<typeof t>[0]), title: n.reason ?? "" };
  }
  const TONE_DOT: Record<Tone, string> = {
    ok: "bg-dida-ok", warn: "bg-dida-warn animate-pulse", bad: "bg-dida-danger", idle: "bg-dida-text-faint",
  };
  const TONE_TXT: Record<Tone, string> = {
    ok: "text-dida-ok", warn: "text-dida-warn", bad: "text-dida-danger", idle: "text-dida-text-faint",
  };

  // Adapter-level connection status (from `dida.status.<name>`) → header badge.
  // ok = connected (green, no label — the dot is enough); connecting = amber;
  // error = red, and the reason is SHOWN, not hovered: this is the onboarding
  // page, where "error" alone tells you nothing you can act on.
  function adapterBadge(c: AdapterCfg): { tone: Tone; label: string; title: string } | null {
    const s = c.status;
    if (!s || s.state === "idle") return null;
    if (s.state === "ok") return { tone: "ok", label: "", title: tr(s.detail) };
    if (s.state === "connecting") return { tone: "warn", label: t("adapters.connecting"), title: tr(s.detail) };
    const reason = tr(s.detail);
    return { tone: "bad", label: reason || t("adapters.connError"), title: reason };
  }

  // Per-node connection edit (host / noise_psk / password) — no remove+re-add.
  let connKey = $state<string | null>(null);
  let ceHost = $state(""); let cePsk = $state(""); let cePwd = $state("");
  let ceBusy = $state(false);
  let ceSaved = $state("");
  const ceDirty = $derived(ceHost !== ceSaved || cePsk.trim() !== "" || cePwd.trim() !== "");
  function openConnEdit(n: EsphomeNode) {
    connKey = connKey === n.key ? null : n.key;
    ceHost = n.host; cePsk = ""; cePwd = ""; ceSaved = n.host;
  }
  async function saveConn(n: EsphomeNode) {
    ceBusy = true;
    try {
      const body: import("$lib/api").NodeEdit = { host: ceHost.trim() };
      if (cePsk.trim()) body.noise_psk = cePsk.trim();   // blank = keep existing secret
      if (cePwd.trim()) body.password = cePwd.trim();
      await api.esphomeEditNode(n.key, body);
      connKey = null;
      await loadNodes();
    } finally {
      ceBusy = false;
    }
  }
  async function addNode() {
    enBusy = true;
    try {
      await api.applyDiscovered("esphome", {
        label: enHost,
        appendJson: { config: { name: enName.trim(), host: enHost.trim(), noise_psk: enPsk.trim() } },
      });
      enName = ""; enHost = ""; enPsk = "";
      await loadNodes();
    } finally {
      enBusy = false;
    }
  }
  async function removeNode(key: string) {
    const ok = await dialog.confirm({
      title: t("common.remove"),
      message: t("esphome.confirmRemove", { name: devices.deviceLabel(key) ?? key }),
      confirmLabel: t("common.remove"),
      danger: true,
    });
    if (!ok) return;
    await api.esphomeRemoveNode(key);
    await loadNodes();
  }
  // Fields of one esphome node (matched to its device_key), from the live store.
  const nodeFields = (key: string) => adapterDevices("esphome").find((g) => g.key === key)?.fields ?? [];

  // --- Frigate: locations (one adapter, many NVRs), edited card-by-card ---
  // A location = a Frigate NVR {name,url,user,password,go2rtc}. The cameras that
  // report from it are grouped under its card (matched by the camera descriptor's
  // `site`), so it's always clear which camera lives at which location.
  let frSites = $state<FrigateSite[]>([]);
  let frName = $state(""); let frUrl = $state(""); let frUser = $state(""); let frPass = $state(""); let frGo2 = $state("");
  let frBusy = $state(false);
  let frAddOpen = $state(false);
  const sortedSites = $derived([...frSites].sort((a, b) => a.name.localeCompare(b.name, "hr")));
  async function loadSites() {
    try { frSites = (await api.frigateSites()).sites; } catch { /* non-fatal */ }
  }
  $effect(() => { if (expandedKey === "frigate") void loadSites(); });
  async function addSite() {
    frBusy = true;
    try {
      // Non-empty path key (slug of name, else url) — an empty key would hit the
      // trailing-slash redirect. Unknown key → the API appends a new location.
      const key = frSiteKey(frName || frUrl);
      await api.frigateSaveSite(key, { name: frName.trim(), url: frUrl.trim(), user: frUser.trim(), password: frPass.trim(), go2rtc: frGo2.trim() });
      frName = ""; frUrl = ""; frUser = ""; frPass = ""; frGo2 = "";
      frAddOpen = false;
      await loadSites();
    } catch (e) { msg = { ...msg, frigate: errMsg(e) }; } finally { frBusy = false; }
  }
  async function removeSite(key: string, name: string) {
    const ok = await dialog.confirm({
      title: t("common.remove"), message: t("frigate.confirmRemoveSite", { name }),
      confirmLabel: t("common.remove"), danger: true,
    });
    if (!ok) return;
    try { await api.frigateRemoveSite(key); await loadSites(); } catch (e) { msg = { ...msg, frigate: errMsg(e) }; }
  }
  // Per-site connection edit (name / url / user / password / go2rtc) in place.
  let siteEditKey = $state<string | null>(null);
  let seName = $state(""); let seUrl = $state(""); let seUser = $state(""); let sePass = $state(""); let seGo2 = $state("");
  let seBusy = $state(false);
  const seForm = () => JSON.stringify([seName, seUrl, seUser, seGo2]);
  let seSaved = $state("");
  const seDirty = $derived(seForm() !== seSaved || sePass.trim() !== "");
  function openSiteEdit(s: FrigateSite) {
    if (siteEditKey === s.key) { siteEditKey = null; return; }
    siteEditKey = s.key; seName = s.name; seUrl = s.url; seUser = s.user; sePass = ""; seGo2 = s.go2rtc;
    seSaved = seForm();
  }
  async function saveSite(s: FrigateSite) {
    seBusy = true;
    try {
      await api.frigateSaveSite(s.key, {
        name: seName.trim(), url: seUrl.trim(), user: seUser.trim(),
        password: sePass.trim() || undefined, go2rtc: seGo2.trim(),
      });
      siteEditKey = null;
      await loadSites();
    } catch (e) { msg = { ...msg, frigate: errMsg(e) }; } finally { seBusy = false; }
  }
  // A frigate camera device's location — read from its `camera` descriptor's `site`.
  function cameraSite(g: { fields: typeof devices.list }): string {
    for (const f of g.fields) {
      const v = f.caps?.["camera"]?.value;
      if (typeof v === "string") { try { return JSON.parse(v).site ?? ""; } catch { /* bad descriptor */ } }
    }
    return "";
  }
  // Same slug as the API's _site_key (dida_core.slug) so a camera's site matches a
  // location's key. Cameras whose site matches no location fall under "" (shown as
  // "unassigned" so nothing silently disappears).
  const frSiteKey = (name: string): string =>
    name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "";
  // Shared by both vision adapters — each groups its cameras under the location
  // they report from, matched on the descriptor's `site`.
  function camerasBySiteOf(adapter: string) {
    const by = new Map<string, ReturnType<typeof adapterDevices>>();
    for (const g of adapterDevices(adapter)) {
      const k = frSiteKey(cameraSite(g));
      if (!by.has(k)) by.set(k, []);
      by.get(k)!.push(g);
    }
    return by;
  }
  const camerasBySite = $derived(camerasBySiteOf("frigate"));
  // Cameras whose site doesn't match any configured location (e.g. site renamed).
  const orphanCameras = $derived(camerasBySite.get("") ?? []);

  // --- BABA: installs (one adapter, many boxes), same card-per-location shape ---
  // A location = one BABA install {name, nats_url(+user/password), go2rtc(+user/password),
  // api_url, peer_key} — three planes on one box: state (NATS), media (go2rtc)
  // and archive (REST). Removing a location here is the ONLY thing that deletes
  // its cameras; the adapter's roster prune deliberately never crosses installs.
  let bbSites = $state<BabaSite[]>([]);
  let bbName = $state(""); let bbNats = $state(""); let bbNatsUser = $state(""); let bbNatsPass = $state("");
  let bbGo2 = $state("");
  let bbGo2User = $state(""); let bbGo2Pass = $state(""); let bbApi = $state(""); let bbPeer = $state("");
  let bbBusy = $state(false);
  let bbAddOpen = $state(false);
  const bbSorted = $derived([...bbSites].sort((a, b) => a.name.localeCompare(b.name, "hr")));
  const bbCamerasBySite = $derived(camerasBySiteOf("baba"));
  const bbOrphanCameras = $derived(bbCamerasBySite.get("") ?? []);
  async function loadBabaSites() {
    try { bbSites = (await api.babaSites()).sites; } catch { /* non-fatal */ }
  }
  $effect(() => { if (expandedKey === "baba") void loadBabaSites(); });
  async function addBabaSite() {
    bbBusy = true;
    try {
      const key = frSiteKey(bbName || bbNats);
      await api.babaSaveSite(key, {
        name: bbName.trim(), nats_url: bbNats.trim(), nats_user: bbNatsUser.trim(),
        nats_password: bbNatsPass.trim() || undefined, go2rtc: bbGo2.trim(),
        go2rtc_user: bbGo2User.trim(), go2rtc_password: bbGo2Pass.trim() || undefined,
        api_url: bbApi.trim(), peer_key: bbPeer.trim() || undefined,
      });
      bbName = ""; bbNats = ""; bbNatsUser = ""; bbNatsPass = ""; bbGo2 = ""; bbGo2User = ""; bbGo2Pass = ""; bbApi = ""; bbPeer = "";
      bbAddOpen = false;
      await loadBabaSites();
    } catch (e) { msg = { ...msg, baba: errMsg(e) }; } finally { bbBusy = false; }
  }
  async function removeBabaSite(key: string, name: string) {
    const ok = await dialog.confirm({
      title: t("common.remove"), message: t("baba.confirmRemoveSite", { name }),
      confirmLabel: t("common.remove"), danger: true,
    });
    if (!ok) return;
    try { await api.babaRemoveSite(key); await loadBabaSites(); } catch (e) { msg = { ...msg, baba: errMsg(e) }; }
  }
  let bbEditKey = $state<string | null>(null);
  let beName = $state(""); let beNats = $state(""); let beNatsUser = $state(""); let beNatsPass = $state("");
  let beGo2 = $state("");
  let beGo2User = $state(""); let beGo2Pass = $state(""); let beApi = $state(""); let bePeer = $state("");
  let beBusy = $state(false);
  const beForm = () => JSON.stringify([beName, beNats, beNatsUser, beGo2, beGo2User, beApi]);
  let beSaved = $state("");
  const beDirty = $derived(beForm() !== beSaved || [beNatsPass, beGo2Pass, bePeer].some((v) => v.trim() !== ""));
  function openBabaEdit(s: BabaSite) {
    if (bbEditKey === s.key) { bbEditKey = null; return; }
    bbEditKey = s.key;
    beName = s.name; beNats = s.nats_url; beNatsUser = s.nats_user; beNatsPass = ""; beGo2 = s.go2rtc;
    beGo2User = s.go2rtc_user; beGo2Pass = ""; beApi = s.api_url; bePeer = "";
    beSaved = beForm();
  }
  async function saveBabaSite(s: BabaSite) {
    beBusy = true;
    try {
      await api.babaSaveSite(s.key, {
        name: beName.trim(), nats_url: beNats.trim(), nats_user: beNatsUser.trim(),
        nats_password: beNatsPass.trim() || undefined, go2rtc: beGo2.trim(),
        go2rtc_user: beGo2User.trim(), go2rtc_password: beGo2Pass.trim() || undefined,
        api_url: beApi.trim(), peer_key: bePeer.trim() || undefined,
      });
      bbEditKey = null;
      await loadBabaSites();
    } catch (e) { msg = { ...msg, baba: errMsg(e) }; } finally { beBusy = false; }
  }

  // --- Tuya: cloud onboarding (pull local keys + locate on the LAN, then add) ---
  let tuyaFound = $state<TuyaCloudDevice[]>([]);
  let tuyaBusy = $state(false);
  let tuyaMsg = $state("");
  const tuyaAddable = $derived(tuyaFound.filter((d) => d.online && !d.already));
  async function tuyaFetch() {
    tuyaBusy = true;
    tuyaMsg = t("tuya.cloud.fetching");
    try {
      tuyaFound = (await api.tuyaCloudDevices()).devices;
      tuyaMsg = tuyaFound.length ? "" : t("tuya.cloud.none");
    } catch (e) {
      tuyaMsg = errMsg(e);
    } finally {
      tuyaBusy = false;
    }
  }
  async function tuyaAdd(ids: string[]) {
    if (!ids.length) return;
    tuyaBusy = true;
    try {
      await api.tuyaCloudAdd(ids);
      await tuyaFetch(); // refresh so added devices flip to "configured"
    } catch (e) {
      tuyaMsg = errMsg(e);
    } finally {
      tuyaBusy = false;
    }
  }
  const tuyaAddAll = () => tuyaAdd(tuyaAddable.map((d) => d.id));

  // Remove a device from any adapter (same UX everywhere, like esphome nodes).
  // Tuya has a clean config-removal (frees the cloud slot + re-fetchable); every
  // other adapter uses the generic delete (engine blocks re-adds so it's durable).
  async function removeDevice(adapter: string, key: string, name: string) {
    const ok = await dialog.confirm({
      title: t("common.remove"),
      message: t("esphome.confirmRemove", { name }),
      confirmLabel: t("common.remove"),
      danger: true,
    });
    if (!ok) return;
    try {
      if (adapter === "tuya") await api.tuyaRemoveDevice(key);
      else await api.deleteDevice(key);
    } catch (e) {
      msg = { ...msg, [adapter]: errMsg(e) };
      return;
    }
    if (openDevice === key) openDevice = null;
    for (const id of Object.keys(devices.byId)) {
      const d = devices.byId[id];
      if (d.deviceKey === key || d.entityId === key) delete devices.byId[id];
    }
  }
  // How many of a device's capabilities a regular (non-admin) user sees on the
  // Devices page — shown/total — for the card header. `shown` = not hidden AND the
  // entity is exposed; `total` = every real capability.
  const deviceExposure = (fields: typeof devices.list) => {
    let total = 0, shown = 0;
    for (const f of fields) {
      const caps = realCaps(f.capabilities);
      total += caps.length;
      if (f.exposed) shown += caps.filter((c) => !f.hiddenCaps.includes(c)).length;
    }
    return { shown, total };
  };
</script>

<svelte:head><title>{t("system.adapters")}</title></svelte:head>

<PageHead
  sticky={false}
  facts={[
    { text: `${formatNumber(totalDevices)} ${t("system.devices")}` },
    ...(totalPeople ? [{ text: `${formatNumber(totalPeople)} ${t("system.people")}` }] : []),
    { text: `${formatNumber(totalAdapters)} ${t("system.adapters")}` },
  ]}
/>

{#if loadErr}
  <Notice tone="err">{loadErr}</Notice>
{/if}

{#snippet fieldRow(f: (typeof devices.list)[number], perGang: boolean, multiEntity: boolean)}
  {@const caps = realCaps(f.capabilities)}
  {#if caps.length === 0}
    <div class="flex items-center gap-3 rounded border border-dida-border bg-dida-panel px-2 py-1.5 text-m opacity-50">
      <span class="min-w-0 flex-1 truncate">{f.name}</span>
      <span class="shrink-0 text-s text-dida-text-faint" title={f.deviceType ?? ""}>{t("adapters.unsupported")}</span>
    </div>
  {:else}
    {@const gangType = perGang && f.exposed && isGang(f)}
    <!-- The entity name is renameable when it stands on its own row (a single-cap
         entity in a multi-entity device — e.g. one presence ZONE of an FP2), or on
         an actuator gang. A single-entity device is renamed from its header instead. -->
    {@const nameEditable = gangType || (caps.length === 1 && multiEntity)}
    <!-- One row PER capability: label · control · [per-gang type] · show/hide
         checkbox (unchecking a cap hides it from Devices; hiding every cap also
         un-exposes the entity). The type select shows only for a multi-gang
         device's actuator gangs — e.g. one relay drives a light, another a switch. -->
    {#each caps as c, ci (c)}
      <div class="flex min-w-0 items-center gap-3 rounded border border-dida-border bg-dida-panel px-2 py-1.5 text-m">
        {#if nameEditable && ci === 0 && editEntity === f.entityId}
          <input
            bind:value={editEntityVal} use:focusEl placeholder={f.rawName}
            onkeydown={(e) => { if (e.key === "Enter") saveEntityName(f.entityId); if (e.key === "Escape") editEntity = null; }}
            onblur={() => saveEntityName(f.entityId)}
            class="min-w-0 max-w-[45%] shrink-0" />
        {:else}
          <span class="min-w-0 max-w-[45%] shrink-0 truncate">{caps.length > 1 ? capLabel(c) : f.name}</span>
          {#if nameEditable && ci === 0}
            <Button size="small" label={t("common.rename")} title={t("common.rename")} onclick={() => startEntityEdit(f)}>✎</Button>
          {/if}
        {/if}
        <div class="min-w-0 flex-1">
          <CapabilityControl compact entityId={f.entityId} capability={c}
            cs={f.caps[c] ?? EMPTY_CS} options={fieldOptions(f, c)} range={fieldRange(f, c)} />
        </div>
        {#if gangType && ci === 0}
          <select value={f.deviceType ?? ""} title={t("adapters.deviceType")}
            onchange={(e) => devices.setEntityType(f.entityId, e.currentTarget.value)}
            class="shrink-0 py-0.5 text-s">
            <option value="" disabled>—</option>
            {#each DEVICE_TYPES as ty (ty)}<option value={ty}>{typeLabel(ty)}</option>{/each}
          </select>
        {/if}
        {#if ci === 0 && voiceEligible(f)}
          <!-- Shows the EFFECTIVE state (house rule, or an override where one was
               set); the dot marks entities someone decided about by hand. -->
          <button type="button" aria-pressed={f.voiceEffective} aria-label={t("fields.voice")}
            title={f.voiceExposed === null ? t("fields.voice") : t("fields.voiceOverride")}
            onclick={() => devices.setVoiceExposed(f.entityId, !f.voiceEffective)}
            class="relative shrink-0 text-m leading-none {f.voiceEffective ? 'text-dida-accent' : 'text-dida-text-faint hover:text-dida-text-muted'}">
            🎙{#if f.voiceExposed !== null}<span class="absolute -right-0.5 -top-0.5 size-1.5 rounded-full bg-current"></span>{/if}
          </button>
        {/if}
        <input type="checkbox" checked={f.exposed && !f.hiddenCaps.includes(c)} title={t("fields.show")}
          onchange={(e) => devices.setCapExposed(f.entityId, c, e.currentTarget.checked)}
          class="shrink-0 accent-dida-accent" />
      </div>
    {/each}
  {/if}
{/snippet}

{#snippet bucketedFields(fields: (typeof devices.list))}
  <!-- A device is "multi-gang" when more than one of its EXPOSED entities is an
       actuator (separate relays). Only then does per-gang typing make sense; a
       single-gang device is typed once from its header dropdown. Hidden entities
       (the device's own status/power/restart, which the user un-exposed) are not
       outputs, so they never count as gangs nor get a type control. -->
  {@const perGang = fields.filter((f) => f.exposed && isGang(f)).length > 1}
  {@const multiEntity = fields.length > 1}
  {#each fieldBuckets(fields) as b (b.key)}
    <div class="mt-3 first:mt-0">
      <div class="mb-1 flex items-center justify-between gap-2">
        <span class="{SUBSECTION_TITLE_CLASS}">{b.label}</span>
        {#if toggleable(b.fields).length}
          <input type="checkbox" checked={groupAllOn(b.fields)}
            use:indet={groupSomeOn(b.fields) && !groupAllOn(b.fields)}
            onchange={() => setGroupExposed(b.fields, !groupAllOn(b.fields))}
            title={t("fields.toggleGroup")} class="accent-dida-accent" />
        {/if}
      </div>
      <div class="flex flex-col gap-1">
        {#each b.fields as f (f.entityId)}{@render fieldRow(f, perGang, multiEntity)}{/each}
      </div>
    </div>
  {/each}
{/snippet}

<!-- The SAME card shell as adapterCard's header row, so all three groups read as
     one surface — a stopped adapter or a service is just a card whose right-side
     action is the run toggle instead of the expand chevron. -->
{#snippet offAdapterRow(c: AdapterCfg)}
  <div class="overflow-hidden rounded-lg border border-dida-border bg-dida-panel opacity-70">
    <div class="flex w-full items-center justify-between gap-2 p-4">
      <span class="flex min-w-0 items-center gap-2">
        <span class="truncate text-m font-semibold">{adapterLabel(c)}</span>
        <span class="shrink-0 font-mono text-s font-normal text-dida-text-faint">{c.adapter}</span>
        <span class="h-1.5 w-1.5 shrink-0 rounded-full bg-dida-text-faint"></span>
      </span>
      <Button size="small" onclick={() => setRunning(c.adapter, true)} disabled={toggling !== null}
      >{toggling === c.adapter ? t("runtime.starting") : t("runtime.start")}</Button>
    </div>
  </div>
{/snippet}

{#snippet serviceRow(name: string, p: { enabled: boolean; running: number; total: number })}
  {@const expandable = p.enabled && name === "cloudflare-tunnel"}
  <div class="overflow-hidden rounded-lg border border-dida-border bg-dida-panel {p.enabled ? '' : 'opacity-70'}">
    <div class="flex w-full items-center justify-between gap-2 p-4">
      <span class="flex min-w-0 items-center gap-2">
        {#if expandable}
          <button type="button" onclick={() => (expandedKey = expandedKey === name ? null : name)}
            class="flex min-w-0 items-center gap-2 text-left hover:text-dida-accent">
            <span class="truncate text-m font-semibold">{svcLabel(name)}</span>
            <span class="shrink-0 font-mono text-s font-normal text-dida-text-faint">{name}</span>
          </button>
        {:else}
          <span class="truncate text-m font-semibold">{svcLabel(name)}</span>
          <span class="shrink-0 font-mono text-s font-normal text-dida-text-faint">{name}</span>
        {/if}
        <span class="h-1.5 w-1.5 shrink-0 rounded-full {p.enabled ? 'bg-dida-ok' : 'bg-dida-text-faint'}"></span>
        {#if p.enabled}
          <span class="shrink-0 font-sans text-s font-normal text-dida-text-faint">{p.running}/{p.total}</span>
        {/if}
      </span>
      <span class="flex shrink-0 items-center gap-2">
        <Button size="small" disabled={toggling !== null}
          onclick={() => setRunning(name, !p.enabled)}
        >{toggling === name
            ? (p.enabled ? t("runtime.stopping") : t("runtime.starting"))
            : (p.enabled ? t("runtime.stop") : t("runtime.start"))}</Button>
        {#if expandable}
          <button type="button" onclick={() => (expandedKey = expandedKey === name ? null : name)}
            aria-label={svcLabel(name)} class="text-s text-dida-text-faint hover:text-dida-accent">{expandedKey === name ? "▾" : "▸"}</button>
        {/if}
      </span>
    </div>
    {#if expandedKey === name}
      <div class="@container px-4 pb-4 [&>*:first-child]:mt-0">
        {#if name === "cloudflare-tunnel"}<CloudflareRoutes />{/if}
      </div>
    {/if}
  </div>
{/snippet}

{#snippet statusDot(c: AdapterCfg)}
  {@const badge = adapterBadge(c)}
  {#if badge}
    <span class="flex items-center gap-1 font-sans text-s font-normal {TONE_TXT[badge.tone]}" title={badge.title}>
      <span class="h-1.5 w-1.5 rounded-full {TONE_DOT[badge.tone]}"></span>{badge.label}
    </span>
  {:else if c.fields.some((f) => f.configured) || adapterDevices(c.adapter).length}
    <span class="h-1.5 w-1.5 rounded-full bg-dida-ok" title={t("settings.configured")}></span>
  {/if}
{/snippet}

{#snippet deviceRow(c: AdapterCfg, grp: ReturnType<typeof adapterDevices>[number])}
  {@const ex = deviceExposure(grp.fields)}
  {@const down = grp.fields.some((f) => f.reachable === false)}
  <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
    <div class="flex items-center gap-2 text-m" class:mb-2={openDevice === grp.key}>
      {#if editKey === grp.key}
        <input bind:value={editVal} use:focusEl placeholder={grp.name}
          onkeydown={(e) => { if (e.key === "Enter") saveEdit(grp.key); if (e.key === "Escape") editKey = null; }}
          onblur={() => saveEdit(grp.key)} class="w-48 shrink-0" />
      {:else}
        <button type="button" onclick={() => (openDevice = openDevice === grp.key ? null : grp.key)}
          class="flex min-w-0 items-center gap-1.5 text-left hover:text-dida-accent">
          <span class="shrink-0 text-s text-dida-text-faint">{openDevice === grp.key ? "▾" : "▸"}</span>
          <span class="truncate font-medium">{grp.name}</span>
        </button>
        <Button size="small" label={t("common.rename")} title={t("common.rename")} onclick={() => startEdit(grp.key)}>✎</Button>
      {/if}
      <span class="flex shrink-0 items-center gap-1 text-s {down ? 'text-dida-warn' : 'text-dida-text-faint'}"
        title={down ? t("card.unreachable") : t("fields.shownTotal")}>
        <span class="h-1.5 w-1.5 rounded-full {down ? 'bg-dida-warn' : 'bg-dida-ok'}"></span>{down ? t("card.unreachable") : `${ex.shown}/${ex.total}`}
      </span>
      <select value={deviceKind(grp.key)} title={t("adapters.deviceType")}
        onchange={(e) => devices.setDeviceType(grp.key, e.currentTarget.value)}
        class="ml-auto shrink-0 py-0.5 text-s">
        <option value="" disabled>—</option>
        {#each DEVICE_TYPES as ty (ty)}<option value={ty}>{typeLabel(ty)}</option>{/each}
      </select>
      <select value={deviceArea(grp.key) ?? ""} title={t("adapters.room")}
        onchange={(e) => devices.setDeviceArea(grp.key, e.currentTarget.value ? Number(e.currentTarget.value) : null)}
        class="shrink-0 py-0.5 text-s">
        <option value="">{t("adapters.noRoom")}</option>
        {#each devices.sortedAreas as a (a.id)}<option value={a.id}>{areaName(a)}</option>{/each}
      </select>
      <Button tone="danger" size="small" onclick={() => removeDevice(c.adapter, grp.key, grp.name)}>{t("common.remove")}</Button>
    </div>
    {#if openDevice === grp.key}
      {@render bucketedFields(grp.fields)}
    {/if}
  </div>
{/snippet}

{#snippet frigateLocations(c: AdapterCfg)}
  <div class="mt-3 border-t border-dida-border pt-3">
    <!-- Add a location — form hidden until the ＋ toolbar button opens it -->
    {#if frAddOpen}
      <div class="rounded border border-dida-border bg-dida-panel-2 p-3">
        <div class="mb-2 text-s font-semibold">{t("frigate.addLocation")}</div>
        <div class="flex flex-wrap items-center gap-2">
          <input bind:value={frName} placeholder={t("frigate.locationName")} autocomplete="off" class="w-32" />
          <input bind:value={frUrl} placeholder="https://frigate.example:8971" autocomplete="off" spellcheck="false" class="min-w-56 flex-1 font-mono text-s" />
          <input bind:value={frUser} placeholder={t("frigate.user")} autocomplete="off" class="w-28" />
          <input bind:value={frPass} type="password" placeholder={t("frigate.password")} autocomplete="new-password" class="w-32" />
          <Button tone="primary" onclick={addSite} disabled={frBusy || !frUrl.trim()}>{t("discover.add")}</Button>
          <Button onclick={() => (frAddOpen = false)}>{t("common.cancel")}</Button>
        </div>
        <p class="mt-1 text-s text-dida-text-faint">{t("frigate.addHint")}</p>
      </div>
    {/if}

    {#if sortedSites.length}
      <div class="mt-3 flex flex-col gap-3">
        {#each sortedSites as s (s.key)}
          {@const cams = camerasBySite.get(s.key) ?? []}
          <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
            <div class="flex flex-wrap items-center gap-2 text-m">
              <span class="truncate font-semibold">📍 {s.name}</span>
              <span class="hidden min-w-0 truncate font-mono text-s text-dida-text-faint sm:inline">{s.url}</span>
              {#if s.has_password}<span class="shrink-0 text-s" title={t("esphome.encrypted")}>🔒</span>{/if}
              <span class="shrink-0 text-s text-dida-text-faint">{cams.length} {t("frigate.cameras")}</span>
              <span class="ml-auto"><Button size="small" label={t("esphome.editConn")} title={t("esphome.editConn")} selected={siteEditKey === s.key} onclick={() => openSiteEdit(s)}>🔧</Button></span>
              <Button tone="danger" size="small" onclick={() => removeSite(s.key, s.name)}>{t("common.remove")}</Button>
            </div>

            {#if siteEditKey === s.key}
              <div class="mt-2 flex flex-wrap items-end gap-2 rounded border border-dida-border bg-dida-panel-2 p-2">
                <label class="text-s text-dida-text-faint">{t("frigate.locationName")}
                  <input bind:value={seName} class="mt-0.5 block w-32" /></label>
                <label class="min-w-56 flex-1 text-s text-dida-text-faint">Frigate URL
                  <input bind:value={seUrl} autocomplete="off" spellcheck="false" class="mt-0.5 block w-full font-mono text-s" /></label>
                <label class="text-s text-dida-text-faint">{t("frigate.user")}
                  <input bind:value={seUser} autocomplete="off" class="mt-0.5 block w-28" /></label>
                <label class="text-s text-dida-text-faint">{t("frigate.password")}
                  <input bind:value={sePass} type="password" placeholder={s.has_password ? t("esphome.secretKept") : "—"} autocomplete="new-password" class="mt-0.5 block w-32" /></label>
                <label class="text-s text-dida-text-faint">go2rtc
                  <input bind:value={seGo2} autocomplete="off" spellcheck="false" class="mt-0.5 block w-40 font-mono text-s" /></label>
                <SaveButton size="small" dirty={seDirty} saving={seBusy} blocked={!seUrl.trim()} onclick={() => saveSite(s)} />
                <Button size="small" onclick={() => (siteEditKey = null)}>{t("common.cancel")}</Button>
              </div>
            {/if}

            {#if cams.length}
              <div class="mt-2 flex flex-col gap-2 border-t border-dida-border pt-2">
                {#each cams as grp (grp.key)}{@render deviceRow(c, grp)}{/each}
              </div>
            {:else}
              <p class="mt-2 text-s text-dida-text-faint">{t("frigate.noCameras")}</p>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if orphanCameras.length}
      <div class="mt-3"><Notice tone="warn">
        <div class="text-s font-semibold">{t("frigate.unassigned")}</div>
        <div class="flex flex-col gap-2">
          {#each orphanCameras as grp (grp.key)}{@render deviceRow(c, grp)}{/each}
        </div>
      </Notice></div>
    {/if}
  </div>
{/snippet}

{#snippet babaLocations(c: AdapterCfg)}
  <div class="mt-3 border-t border-dida-border pt-3">
    {#if bbAddOpen}
      <div class="rounded border border-dida-border bg-dida-panel-2 p-3">
        <div class="mb-2 text-s font-semibold">{t("baba.addLocation")}</div>
        <div class="flex flex-wrap items-center gap-2">
          <input bind:value={bbName} placeholder={t("baba.locationName")} autocomplete="off" class="w-32" />
          <input bind:value={bbNats} placeholder="nats://baba.example:4222" autocomplete="off" spellcheck="false" class="min-w-56 flex-1 font-mono text-s" />
          <input bind:value={bbNatsUser} placeholder={t("baba.natsUser")} autocomplete="off" class="w-28" />
          <input bind:value={bbNatsPass} type="password" placeholder={t("baba.natsPassword")} autocomplete="new-password" class="w-32" />
          <input bind:value={bbGo2} placeholder="http://baba.example:11984" autocomplete="off" spellcheck="false" class="min-w-44 flex-1 font-mono text-s" />
          <input bind:value={bbGo2User} placeholder={t("baba.go2rtcUser")} autocomplete="off" class="w-28" />
          <input bind:value={bbGo2Pass} type="password" placeholder={t("baba.go2rtcPassword")} autocomplete="new-password" class="w-32" />
          <input bind:value={bbApi} placeholder="http://baba.example:8080" autocomplete="off" spellcheck="false" class="min-w-44 flex-1 font-mono text-s" />
          <input bind:value={bbPeer} type="password" placeholder={t("baba.peerKey")} autocomplete="new-password" class="w-40" />
          <Button tone="primary" onclick={addBabaSite} disabled={bbBusy || !bbNats.trim()}>{t("discover.add")}</Button>
          <Button onclick={() => (bbAddOpen = false)}>{t("common.cancel")}</Button>
        </div>
        <p class="mt-1 text-s text-dida-text-faint">{t("baba.addHint")}</p>
      </div>
    {/if}

    {#if bbSorted.length}
      <div class="mt-3 flex flex-col gap-3">
        {#each bbSorted as s (s.key)}
          {@const cams = bbCamerasBySite.get(s.key) ?? []}
          <div class="rounded-lg border border-dida-border bg-dida-panel p-3">
            <div class="flex flex-wrap items-center gap-2 text-m">
              <span class="truncate font-semibold">📍 {s.name}</span>
              <span class="hidden min-w-0 truncate font-mono text-s text-dida-text-faint sm:inline">{s.nats_url}</span>
              {#if s.has_peer_key}<span class="shrink-0 text-s" title={t("esphome.encrypted")}>🔒</span>{/if}
              <span class="shrink-0 text-s text-dida-text-faint">{cams.length} {t("frigate.cameras")}</span>
              <span class="ml-auto"><Button size="small" label={t("esphome.editConn")} title={t("esphome.editConn")} selected={bbEditKey === s.key} onclick={() => openBabaEdit(s)}>🔧</Button></span>
              <Button tone="danger" size="small" onclick={() => removeBabaSite(s.key, s.name)}>{t("common.remove")}</Button>
            </div>

            {#if bbEditKey === s.key}
              <div class="mt-2 flex flex-wrap items-end gap-2 rounded border border-dida-border bg-dida-panel-2 p-2">
                <label class="text-s text-dida-text-faint">{t("baba.locationName")}
                  <input bind:value={beName} class="mt-0.5 block w-32" /></label>
                <label class="min-w-56 flex-1 text-s text-dida-text-faint">NATS
                  <input bind:value={beNats} autocomplete="off" spellcheck="false" class="mt-0.5 block w-full font-mono text-s" /></label>
                <label class="text-s text-dida-text-faint">{t("baba.natsUser")}
                  <input bind:value={beNatsUser} autocomplete="off" class="mt-0.5 block w-28" /></label>
                <label class="text-s text-dida-text-faint">{t("baba.natsPassword")}
                  <input bind:value={beNatsPass} type="password" placeholder={s.has_nats_password ? t("esphome.secretKept") : "—"} autocomplete="new-password" class="mt-0.5 block w-32" /></label>
                <label class="min-w-44 flex-1 text-s text-dida-text-faint">go2rtc
                  <input bind:value={beGo2} autocomplete="off" spellcheck="false" class="mt-0.5 block w-full font-mono text-s" /></label>
                <label class="text-s text-dida-text-faint">{t("baba.go2rtcUser")}
                  <input bind:value={beGo2User} autocomplete="off" class="mt-0.5 block w-28" /></label>
                <label class="text-s text-dida-text-faint">{t("baba.go2rtcPassword")}
                  <input bind:value={beGo2Pass} type="password" placeholder={s.has_go2rtc_password ? t("esphome.secretKept") : "—"} autocomplete="new-password" class="mt-0.5 block w-32" /></label>
                <label class="min-w-44 flex-1 text-s text-dida-text-faint">{t("baba.apiUrl")}
                  <input bind:value={beApi} autocomplete="off" spellcheck="false" class="mt-0.5 block w-full font-mono text-s" /></label>
                <label class="text-s text-dida-text-faint">{t("baba.peerKey")}
                  <input bind:value={bePeer} type="password" placeholder={s.has_peer_key ? t("esphome.secretKept") : "—"} autocomplete="new-password" class="mt-0.5 block w-40" /></label>
                <SaveButton size="small" dirty={beDirty} saving={beBusy} blocked={!beNats.trim()} onclick={() => saveBabaSite(s)} />
                <Button size="small" onclick={() => (bbEditKey = null)}>{t("common.cancel")}</Button>
              </div>
            {/if}

            {#if cams.length}
              <div class="mt-2 flex flex-col gap-2 border-t border-dida-border pt-2">
                {#each cams as grp (grp.key)}{@render deviceRow(c, grp)}{/each}
              </div>
            {:else}
              <p class="mt-2 text-s text-dida-text-faint">{t("frigate.noCameras")}</p>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if bbOrphanCameras.length}
      <div class="mt-3"><Notice tone="warn">
        <div class="text-s font-semibold">{t("frigate.unassigned")}</div>
        <div class="flex flex-col gap-2">
          {#each bbOrphanCameras as grp (grp.key)}{@render deviceRow(c, grp)}{/each}
        </div>
      </Notice></div>
    {/if}
  </div>
{/snippet}

{#snippet adapterCard(c: AdapterCfg)}
  <div class="overflow-hidden rounded-lg border border-dida-border bg-dida-panel">
    <div class="flex w-full items-center justify-between gap-2 p-4">
      <span class="flex min-w-0 items-center gap-2">
        {#if editAdapter === c.adapter}
          <input bind:value={editAdapterVal} use:focusEl placeholder={adapterLabel(c)}
            onkeydown={(e) => { if (e.key === "Enter") saveAdapterName(c.adapter); if (e.key === "Escape") editAdapter = null; }}
            onblur={() => saveAdapterName(c.adapter)} class="w-48 shrink-0" />
        {:else}
          <button type="button" onclick={() => (expandedKey = expandedKey === c.adapter ? null : c.adapter)}
            class="flex min-w-0 items-center gap-2 text-left hover:text-dida-accent">
            <span class="truncate text-m font-semibold">{adapterLabel(c)}</span>
            <span class="shrink-0 font-mono text-s font-normal text-dida-text-faint">{c.adapter}</span>
          </button>
          <Button size="small" label={t("common.rename")} title={t("common.rename")} onclick={() => startEditAdapter(c)}>✎</Button>
        {/if}
        {@render statusDot(c)}
        {#if adapterDevices(c.adapter).length}
          <span class="shrink-0 font-sans text-s font-normal text-dida-text-faint">
            {adapterDevices(c.adapter).length} {t("esphome.devices")}{#if c.adapter === "esphome" && esStats.total} · {esStats.connected}/{esStats.total} {t("esphome.online")}{/if}
          </span>
        {/if}
      </span>
      <button type="button" onclick={() => (expandedKey = expandedKey === c.adapter ? null : c.adapter)}
        aria-label={adapterLabel(c)} class="shrink-0 text-s text-dida-text-faint hover:text-dida-accent">{expandedKey === c.adapter ? "▾" : "▸"}</button>
    </div>
    {#if expandedKey === c.adapter}
    <div class="@container px-4 pb-4 [&>*:first-child]:mt-0">
    <!-- Uniform action toolbar — adapter-level actions top-right (Save lives with the fields it saves) -->
    {#if msg[c.adapter] || c.discoverable || profileOf(c.adapter) || ["tuya", "homekit", "smartthings", "contacts", "androidtv", "frigate", "baba"].includes(c.adapter)}
      <div class="mb-3 flex items-center justify-end gap-2">
        {#if msg[c.adapter]}<span class="mr-auto text-s text-dida-text-muted">{msg[c.adapter]}</span>{/if}
        {#if profileOf(c.adapter)}
          <Button size="small" onclick={() => setRunning(c.adapter, false)} disabled={toggling !== null}
          >{toggling === c.adapter ? t("runtime.stopping") : t("runtime.stop")}</Button>
        {/if}
        {#if c.adapter === "tuya" && tuyaMsg}<span class="mr-auto text-s text-dida-text-muted">{tuyaMsg}</span>{/if}
        {#if c.adapter === "homekit" && hkMsg}<span class="mr-auto text-s text-dida-text-muted">{hkMsg}</span>{/if}
        {#if c.adapter === "androidtv" && atvMsg}<span class="mr-auto text-s text-dida-text-muted">{atvMsg}</span>{/if}
        {#if c.discoverable}
          <Button size="small" onclick={() => rescanAdapter(c.adapter)} disabled={rescanning === c.adapter}
          >{rescanning === c.adapter ? t("discover.scanning") : t("adapters.rescan")}</Button>
        {/if}
        {#if c.adapter === "tuya"}
          <Button size="small" onclick={tuyaFetch} disabled={tuyaBusy}
          >{tuyaBusy ? t("tuya.cloud.fetching") : t("tuya.cloud.fetch")}</Button>
        {/if}
        {#if c.adapter === "homekit"}
          <Button size="small" onclick={hkDiscover} disabled={hkLoading}
          >{hkLoading ? t("homekit.searching") : t("homekit.discover")}</Button>
        {/if}
        {#if c.adapter === "smartthings"}
          {#if stStatus?.connected}
            <span class="mr-auto flex items-center gap-1 text-s text-dida-ok">
              <span class="h-1.5 w-1.5 rounded-full bg-dida-ok"></span>{t("smartthings.connected")}
            </span>
            <Button tone="danger" size="small" onclick={stDisconnect} disabled={stBusy}>{t("smartthings.disconnect")}</Button>
          {:else}
            <Button size="small" onclick={stConnect} disabled={stBusy || !stStatus?.configured}
              title={stStatus?.configured ? "" : t("smartthings.fillFirst")}
            >{t("smartthings.connect")}</Button>
          {/if}
        {/if}
        {#if c.adapter === "contacts"}
          {#if ctStatus?.connected}
            <span class="mr-auto flex items-center gap-1 text-s text-dida-ok">
              <span class="h-1.5 w-1.5 rounded-full bg-dida-ok"></span>{t("contacts.connected")}
            </span>
            <Button tone="danger" size="small" onclick={ctDisconnect} disabled={ctBusy}>{t("contacts.disconnect")}</Button>
          {:else}
            <Button size="small" onclick={ctConnect} disabled={ctBusy || !ctStatus?.configured}
              title={ctStatus?.configured ? "" : t("contacts.fillFirst")}
            >{t("contacts.connect")}</Button>
          {/if}
        {/if}
        {#if c.adapter === "androidtv"}
          {#if atvStatus?.connected}
            <span class="mr-auto flex items-center gap-1 text-s text-dida-ok">
              <span class="h-1.5 w-1.5 rounded-full bg-dida-ok"></span>{t("androidtv.connected")}
            </span>
            <Button size="small" onclick={atvConnect} disabled={atvBusy}>{t("androidtv.reconnect")}</Button>
          {:else}
            <Button size="small" onclick={atvConnect} disabled={atvBusy || !atvStatus?.host}
              title={atvStatus?.host ? "" : t("androidtv.saveHostFirst")}
            >{atvBusy ? t("androidtv.connecting") : t("androidtv.connect")}</Button>
          {/if}
        {/if}
        {#if c.adapter === "frigate"}
          <Button size="small" onclick={() => (frAddOpen = !frAddOpen)}>＋ {t("frigate.addLocation")}</Button>
        {/if}
        {#if c.adapter === "baba"}
          <Button size="small" onclick={() => (bbAddOpen = !bbAddOpen)}>＋ {t("baba.addLocation")}</Button>
        {/if}
      </div>
    {/if}
    {#if c.discoverable && foundAll[c.adapter]?.length}
      <!-- Found on the network by the global scan, not added yet -->
      <div class="mt-3 rounded border border-dida-accent/40 bg-dida-accent/5 p-3">
        <div class="mb-2 flex items-center justify-between gap-2">
          <span class="text-s font-semibold text-dida-accent">{t("discover.pending")}</span>
          <Button tone="primary" size="small" onclick={() => addAllFound(c.adapter)} disabled={addingKey !== null}
          >{t("discover.addAll")} ({foundAll[c.adapter].length})</Button>
        </div>
        <div class="flex flex-col gap-1">
          {#each foundAll[c.adapter] as d (d.label)}
            <div class="flex items-center justify-between gap-2 rounded border border-dida-border bg-dida-panel px-2 py-1.5 text-m">
              <span>{d.label}</span>
              <Button tone="primary" size="small" onclick={() => addFound(c.adapter, d)} disabled={addingKey === c.adapter + d.label}>+ {t("discover.add")}</Button>
            </div>
          {/each}
        </div>
      </div>
    {/if}
    {#if c.adapter === "esphome"}
      <!-- Add node (name + host + noise_psk) — no JSON -->
      <div class="mt-3 rounded border border-dida-border bg-dida-panel-2 p-3">
        <div class="mb-2 text-s font-semibold">{t("esphome.addNode")}</div>
        <div class="flex flex-wrap items-center gap-2">
          <input bind:value={enName} placeholder={t("esphome.nodeName")} autocomplete="off" class="w-32" />
          <input bind:value={enHost} placeholder="203.0.113.51" autocomplete="off" class="w-40" />
          <input bind:value={enPsk} placeholder={t("esphome.psk")} autocomplete="off" spellcheck="false" class="min-w-48 flex-1 font-mono" />
          <Button tone="primary" onclick={addNode} disabled={enBusy || !enHost.trim()}>{t("discover.add")}</Button>
        </div>
        <p class="mt-1 text-s text-dida-text-faint">{t("esphome.pskHelp")}</p>
      </div>
      <!-- One card per node: header + remove, its fields with expose toggles -->
      {#if esNodes.length}
        <div class="mt-3 flex flex-col gap-3">
          {#each sortedNodes as node (node.key)}
            {@const st = nodeStatus(node)}
            {@const ex = deviceExposure(nodeFields(node.key))}
            <div class="rounded-lg border border-dida-border bg-dida-panel-2 p-3">
              <div class="flex items-center gap-2 text-m" class:mb-2={openDevice === node.key || connKey === node.key}>
                {#if editKey === node.key}
                  <input bind:value={editVal} use:focusEl placeholder={nodeLabel(node)}
                    onkeydown={(e) => { if (e.key === "Enter") saveEdit(node.key); if (e.key === "Escape") editKey = null; }}
                    onblur={() => saveEdit(node.key)} class="w-48 shrink-0" />
                {:else}
                  <button onclick={() => (openDevice = openDevice === node.key ? null : node.key)}
                    class="flex min-w-0 items-center gap-1.5 text-left">
                    <span class="shrink-0 text-s text-dida-text-faint">{openDevice === node.key ? "▾" : "▸"}</span>
                    <span class="truncate font-medium">{nodeLabel(node)}</span>
                  </button>
                  <Button size="small" label={t("common.rename")} title={t("common.rename")} onclick={() => startEdit(node.key)}>✎</Button>
                {/if}
                <span class="flex shrink-0 items-center gap-1 text-s {TONE_TXT[st.tone]}" title={st.title}>
                  <span class="h-1.5 w-1.5 rounded-full {TONE_DOT[st.tone]}"></span>{st.label}
                </span>
                {#if ex.total}<span class="shrink-0 text-s text-dida-text-faint" title={t("fields.shownTotal")}>{ex.shown}/{ex.total}</span>{/if}
                <span class="hidden shrink-0 font-mono text-s text-dida-text-faint sm:inline">
                  {#if devices.deviceRawName(node.key)}{devices.deviceRawName(node.key)} · {/if}{node.host}
                </span>
                {#if node.has_psk || node.has_password}<span class="shrink-0 text-s" title={t("esphome.encrypted")}>🔒</span>{/if}
                <Button size="small" label={t("esphome.editConn")} title={t("esphome.editConn")} selected={connKey === node.key} onclick={() => openConnEdit(node)}>🔧</Button>
                <select value={deviceKind(node.key)} title={t("adapters.deviceType")}
                  onchange={(e) => devices.setDeviceType(node.key, e.currentTarget.value)}
                  class="ml-auto shrink-0 py-0.5 text-s">
                  <option value="" disabled>—</option>
                  {#each DEVICE_TYPES as ty (ty)}<option value={ty}>{typeLabel(ty)}</option>{/each}
                </select>
                <select value={deviceArea(node.key) ?? ""} title={t("adapters.room")}
                  onchange={(e) => devices.setDeviceArea(node.key, e.currentTarget.value ? Number(e.currentTarget.value) : null)}
                  class="shrink-0 py-0.5 text-s">
                  <option value="">{t("adapters.noRoom")}</option>
                  {#each devices.sortedAreas as a (a.id)}<option value={a.id}>{areaName(a)}</option>{/each}
                </select>
                <Button tone="danger" size="small" onclick={() => removeNode(node.key)}>{t("common.remove")}</Button>
              </div>
              {#if connKey === node.key}
                <!-- Edit connection in place (host / noise_psk / password) — no remove+re-add -->
                <div class="mb-2 flex flex-wrap items-end gap-2 rounded border border-dida-border bg-dida-panel p-2">
                  <label class="text-s text-dida-text-faint">{t("esphome.host")}
                    <input bind:value={ceHost} placeholder="203.0.113.51" autocomplete="off" class="mt-0.5 block w-40" />
                  </label>
                  <label class="min-w-48 flex-1 text-s text-dida-text-faint">{t("esphome.psk")}
                    <input bind:value={cePsk} placeholder={node.has_psk ? t("esphome.secretKept") : t("esphome.pskBlank")}
                      autocomplete="off" spellcheck="false" class="mt-0.5 block w-full font-mono" />
                  </label>
                  <label class="text-s text-dida-text-faint">{t("esphome.password")}
                    <input bind:value={cePwd} type="password" placeholder={node.has_password ? t("esphome.secretKept") : "—"}
                      autocomplete="new-password" class="mt-0.5 block w-36" />
                  </label>
                  <SaveButton size="small" dirty={ceDirty} saving={ceBusy} blocked={!ceHost.trim()} onclick={() => saveConn(node)} />
                  <Button size="small" onclick={() => (connKey = null)}>{t("common.cancel")}</Button>
                </div>
              {/if}
              {#if openDevice === node.key}
                {#if nodeFields(node.key).length}
                  {@render bucketedFields(nodeFields(node.key))}
                {:else}
                  <p class="text-s {TONE_TXT[st.tone]}">
                    {st.label}{#if node.state === "error" && node.reason} — <span class="text-dida-text-faint">{node.reason}</span>{/if}
                  </p>
                {/if}
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    {:else}
      <!-- Even responsive grid: fields fill aligned columns (~3 on a wide card)
           instead of ragged wrapping rows. Textareas span the whole width.
           The tuya device-list JSON is hidden — it's populated by the cloud Fetch. -->
      {#if c.fields.length}
      <div class="mt-3 grid grid-cols-1 gap-x-4 gap-y-3 @sm:grid-cols-2 @xl:grid-cols-3 @3xl:grid-cols-4">
        {#each cfgFields(c) as fld, i (fld.key)}
          {#if fld.group && fld.group !== cfgFields(c)[i - 1]?.group}
            <h4 class="col-span-full mt-2 {SUBSECTION_TITLE_CLASS}">{tr(fld.group)}</h4>
          {/if}
            <div class="flex flex-col gap-1" class:col-span-full={fld.type === "textarea"} title={tr(fld.help ?? "")}>
              <label for="{c.adapter}-{fld.key}" class="text-s font-medium text-dida-text-muted">{tr(fld.label)}</label>
              {#if fld.type === "bool"}
                <input id="{c.adapter}-{fld.key}" type="checkbox"
                  checked={form[c.adapter][fld.key] === "true"}
                  onchange={(e) => (form[c.adapter][fld.key] = e.currentTarget.checked ? "true" : "false")}
                  class="mt-1 h-4 w-4 accent-dida-accent" />
              {:else if fld.type === "textarea"}
                <textarea id="{c.adapter}-{fld.key}" bind:value={form[c.adapter][fld.key]}
                  rows="3" autocomplete="off" spellcheck="false"
                  placeholder={fld.secret && fld.configured ? `•••••••• (${t("adapters.secretKept")})` : fld.placeholder}
                  class="w-full font-mono"></textarea>
              {:else if fld.type === "select"}
                <select id="{c.adapter}-{fld.key}" bind:value={form[c.adapter][fld.key]} class="w-full">
                  {#each fld.options as o (o)}<option value={o}>{o}</option>{/each}
                </select>
              {:else if fld.type === "entity"}
                <!-- Entity picker: options are the adapter's own devices — curated the
                     same way as everywhere else (only exposed devices), and narrowed to
                     the required capability (entity_cap, e.g. media_display → screens).
                     Empty = off. A saved-but-currently-absent value is kept so it isn't
                     lost. Hide/Remove a device under Adapters → it leaves this list too. -->
                <select id="{c.adapter}-{fld.key}" bind:value={form[c.adapter][fld.key]} class="w-full">
                  <option value="">{t("adapters.entityNone")}</option>
                  {#each entityOptions(c.adapter, fld.entity_cap) as grp (grp.key)}<option value={grp.key}>{grp.name}</option>{/each}
                  {#if form[c.adapter][fld.key] && !entityOptions(c.adapter, fld.entity_cap).some((g) => g.key === form[c.adapter][fld.key])}
                    <option value={form[c.adapter][fld.key]}>{form[c.adapter][fld.key]}</option>
                  {/if}
                </select>
              {:else if c.adapter === "smartthings" && fld.key === "location"}
                <!-- Home-location gate: a ST account can span several homes. Pick one
                     so devices at another house never enter DIDA. Names come from the
                     account (published to app_settings by the adapter on connect). -->
                <select id="{c.adapter}-{fld.key}" bind:value={form[c.adapter][fld.key]} class="w-full">
                  <option value="">{t("smartthings.allLocations")}</option>
                  {#each stLocations as loc (loc.id)}<option value={loc.id}>{loc.name}</option>{/each}
                  {#if form[c.adapter][fld.key] && !stLocations.some((l) => l.id === form[c.adapter][fld.key])}
                    <option value={form[c.adapter][fld.key]}>{form[c.adapter][fld.key]}</option>
                  {/if}
                </select>
              {:else}
                <input id="{c.adapter}-{fld.key}" bind:value={form[c.adapter][fld.key]}
                  type={fld.secret ? "password" : fld.type === "number" ? "number" : "text"}
                  autocomplete={fld.secret ? "new-password" : "off"}
                  placeholder={fld.secret && fld.configured ? "••••••••" : fld.placeholder}
                  class="w-full" />
              {/if}
            </div>
        {/each}
        {#if c.adapter !== "esphome"}
          <!-- Save belongs to the form it saves — last cell of the fields grid -->
          <div class="flex items-end">
            <SaveButton size="small" dirty={dirty(c.adapter)} saving={busy === c.adapter} onclick={() => save(c.adapter)} />
          </div>
        {/if}
      </div>
      {/if}
      {#if c.adapter === "contacts"}
        <!-- The tick is the only thing on this card a person decides, so it is the
             only thing the card explains. The address URI is setup, and disappears
             once there is nothing left to set up. -->
        {#if !ctStatus?.connected}
          <p class="mt-3 text-s text-dida-text-faint">
            {t("contacts.redirectUri")}: <span class="font-mono">{ctStatus?.redirect_uri || "—"}</span>
          </p>
        {/if}
        {#if ctStatus?.last_sync}
          <p class="mt-1 text-s text-dida-text-muted">
            {t("contacts.lastSync", {
              at: ctStatus.last_sync.at ? ctWhen(ctStatus.last_sync.at) : "—",
              read: ctStatus.last_sync.read,
              added: ctStatus.last_sync.added,
              updated: ctStatus.last_sync.updated,
              removed: ctStatus.last_sync.removed,
            })}
          </p>
          {#if ctStatus.last_sync.no_year?.length}
            <!-- Reported, never invented: a day from the book plus a year from
                 anywhere else is a date nobody has ever held. -->
            <p class="mt-1 text-s text-dida-warn">
              {t("contacts.noYear", { count: ctStatus.last_sync.no_year.length })}
              {ctStatus.last_sync.no_year.slice(0, 8).map((p) => p.name).join(", ")}
            </p>
          {/if}
        {/if}
        <p class="mt-3 text-s text-dida-text-muted">{t("contacts.tick", { when: ctWindow })}</p>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <input bind:value={ctFilter} placeholder={t("contacts.search")} class="w-48" />
          <span class="text-s text-dida-text-faint">
            {t("contacts.counts", { people: ctStatus?.people ?? 0, announced: ctStatus?.announced ?? 0 })}
          </span>
          <label class="ml-auto cursor-pointer text-s text-dida-accent hover:underline">
            {t("contacts.import")}
            <input type="file" accept=".vcf,.csv,text/vcard,text/csv" class="hidden"
              onchange={ctImport} disabled={ctBusy} />
          </label>
        </div>
        {#if ctShown.length}
          <div class="mt-2 flex max-h-96 flex-col gap-1 overflow-y-auto">
            {#each ctShown as p (p.id)}
              <label class="flex cursor-pointer items-center gap-3 rounded border border-dida-border bg-dida-panel-2 px-3 py-1.5">
                <input type="checkbox" checked={p.announce} onchange={() => ctToggle(p)}
                  class="h-4 w-4 accent-dida-accent" />
                <span class="min-w-40 flex-1 text-m">{p.name}</span>
                <span class="font-mono text-s text-dida-text-faint">{p.born_on}</span>
                {#if p.source !== "google"}<span class="text-s text-dida-text-faint">{p.source}</span>{/if}
              </label>
            {/each}
          </div>
        {:else}
          <p class="mt-2 text-s text-dida-text-faint">{t("contacts.empty")}</p>
        {/if}
      {/if}
      {#if c.adapter === "homekit"}
        <!-- Pairing flow: Discover (toolbar) → enter setup code → Pair. Paired
             accessories render as device cards below (generic path). -->
        <p class="mt-3 text-s text-dida-text-faint">{t("homekit.intro")}</p>
        {#if hkDevices.length}
          <div class="mt-2 flex flex-col gap-2">
            {#each hkDevices as d (d.device_id)}
              <div class="flex flex-wrap items-center gap-2 rounded border border-dida-border bg-dida-panel-2 px-3 py-2">
                <div class="min-w-40 flex-1">
                  <div class="text-m">{d.name}</div>
                  <div class="font-mono text-s text-dida-text-faint">{d.model || d.device_id}</div>
                </div>
                {#if d.pairable}
                  <input bind:value={hkNames[d.device_id]} placeholder={d.name} class="w-36" />
                  <input bind:value={hkPins[d.device_id]} placeholder="123-45-678" inputmode="numeric"
                    autocomplete="off" class="w-32" />
                  <Button tone="primary" onclick={() => hkPair(d)} disabled={hkBusy === d.device_id || !(hkPins[d.device_id] ?? "").trim()}
                  >{hkBusy === d.device_id ? t("homekit.pairing") : t("homekit.pair")}</Button>
                {:else}
                  <span class="text-s text-dida-text-muted">{t("homekit.alreadyPaired")}</span>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      {/if}
      {#if c.adapter === "announce"}
        <!-- TTS: default spoken language + a Test on every auto-discovered speaker.
             Google Translate TTS gives one voice per language, so language is the
             only knob; the speaker list mirrors the announce:* entities. -->
        <div class="mt-4 flex flex-wrap items-end gap-3">
          <label class="text-m">
            <span class="mb-1 block text-s text-dida-text-muted">{t("tts.defaultLang")}</span>
            <select bind:value={annLang}>
              {#each ANN_LANGS as l (l)}<option value={l}>{l}</option>{/each}
            </select>
          </label>
          <SaveButton size="small" dirty={annLang !== annLangSaved} saving={annSaving} onclick={saveAnnLang} />
        </div>
        <div class="mt-4">
          <span class="text-s font-medium text-dida-text-muted">{t("tts.targets")}</span>
          {#if annTargets.length === 0}
            <p class="text-s text-dida-text-faint">{t("tts.noTargets")}</p>
          {:else}
            <div class="flex flex-col gap-1.5">
              {#each annTargets as tt (tt.entityId)}
                <div class="flex items-center justify-between gap-2 rounded border border-dida-border bg-dida-panel-2 px-3 py-2">
                  <span class="text-m font-medium">{tt.name}<span class="ml-2 font-mono text-s text-dida-text-faint">{tt.entityId}</span></span>
                  <Button size="small" onclick={() => annTest(tt.entityId)} disabled={annTesting === tt.entityId}
                  >{annTesting === tt.entityId ? "…" : `🔊 ${t("tts.test")}`}</Button>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
      {#if c.adapter === "androidtv"}
        <!-- ADB flow: enable Network debugging (developer mode) on the device,
             save the host, click Poveži → accept the on-TV prompt. Apps are then
             pulled off the device; the RSA key is stored encrypted. -->
        <p class="mt-3 text-s text-dida-text-faint">{t("androidtv.intro")}</p>
        <!-- Launchable apps PULLED off the device; rename / show-hide here. -->
        <div class="mt-4">
          <div class="mb-1 flex items-center justify-between gap-2">
            <span class="text-s font-medium text-dida-text-muted">{t("androidtv.apps")}{#if atvStatus?.apps} · {atvStatus.apps}{/if}</span>
            <Button size="small" onclick={atvAddApp}>{t("androidtv.addApp")}</Button>
          </div>
          <p class="mb-2 text-s text-dida-text-faint">{t("androidtv.appsHint")}</p>
          {#if atvAppList.length}
            <div class="flex flex-col gap-1.5">
              {#each atvAppList as app, i (i)}
                <div class="flex flex-wrap items-center gap-2 rounded border border-dida-border bg-dida-panel-2 px-2 py-1.5">
                  <input value={app.name} oninput={(e) => atvEditApp(i, "name", e.currentTarget.value)}
                    placeholder={t("androidtv.appName")} class="w-36" />
                  <input value={app.link} oninput={(e) => atvEditApp(i, "link", e.currentTarget.value)}
                    placeholder="com.netflix.ninja" autocomplete="off" spellcheck="false"
                    class="min-w-0 flex-1 font-mono text-s" />
                  <label class="flex shrink-0 items-center gap-1.5 text-s text-dida-text-muted" title={t("androidtv.showInUi")}>
                    <input type="checkbox" checked={!app.hidden}
                      onchange={(e) => atvToggleApp(i, e.currentTarget.checked)} class="h-4 w-4 accent-dida-accent" />
                    {t("androidtv.show")}
                  </label>
                  <Button size="small" label={t("common.delete")} title={t("common.delete")} onclick={() => atvRemoveApp(i)}>✕</Button>
                </div>
              {/each}
            </div>
          {:else}
            <p class="text-s text-dida-text-faint">{t("androidtv.noApps")}</p>
          {/if}
        </div>
      {/if}
      {#if c.adapter === "tuya" && tuyaFound.length}
        <div class="mt-3 flex flex-col gap-1.5">
          {#if tuyaAddable.length}
            <div class="flex items-center justify-between gap-2">
              <span class="text-s text-dida-text-muted">{tuyaAddable.length} {t("tuya.cloud.addable")}</span>
              <Button size="small" onclick={tuyaAddAll} disabled={tuyaBusy}>{t("tuya.cloud.addAll")}</Button>
            </div>
          {/if}
          {#each tuyaFound as d (d.id)}
            <div class="flex flex-wrap items-center gap-2 rounded border border-dida-border bg-dida-panel px-2 py-1.5 text-m">
              <span class="h-1.5 w-1.5 shrink-0 rounded-full {d.already ? 'bg-dida-text-faint' : d.online ? 'bg-dida-ok' : 'bg-dida-danger'}"></span>
              <span class="min-w-0 flex-1 truncate">{d.name}</span>
              <span class="shrink-0 font-mono text-s text-dida-text-faint">{d.cloud ? t("tuya.cloud.viaCloud") : (d.ip ?? t("tuya.cloud.notFound"))}</span>
              {#if d.caps.length}<span class="shrink-0 text-s text-dida-text-faint">{d.caps.join(" · ")}</span>{/if}
              {#if d.already}
                <span class="shrink-0 text-s text-dida-ok">{t("settings.configured")}</span>
              {:else if d.online}
                <Button tone="primary" size="small" onclick={() => tuyaAdd([d.id])} disabled={tuyaBusy}>{t("discover.add")}</Button>
              {:else}
                <span class="shrink-0 text-s text-dida-danger" title={t("tuya.cloud.offlineHint")}>{t("tuya.cloud.offline")}</span>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
      {#if c.adapter === "mqtt" && zigbeeProfile}
        <!-- The Zigbee bridge itself: its container's state, and the tools that
             drive it (permit-join, interview, rename, remove, the map). They used
             to sit in the services list under a second name for the same thing. -->
        <div class="mt-3 border-t border-dida-border pt-3">
          <div class="mb-2 flex items-center justify-between gap-2">
            <span class="text-m font-semibold">{t("runtime.svc.zigbee")}</span>
            <span class="flex items-center gap-2">
              <span class="text-s text-dida-text-faint">{zigbeeProfile.running}/{zigbeeProfile.total}</span>
              <Button size="small"
                onclick={() => setRunning("zigbee", !zigbeeProfile.enabled)} disabled={toggling !== null}
              >{toggling === "zigbee"
                  ? (zigbeeProfile.enabled ? t("runtime.stopping") : t("runtime.starting"))
                  : (zigbeeProfile.enabled ? t("runtime.stop") : t("runtime.start"))}</Button>
            </span>
          </div>
          {#if zigbeeConsole}
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <Button size="small" onclick={() => window.open(zigbeeConsole, "_blank", "noopener,noreferrer")}
              >{t("zigbee.console")}</Button>
              <Button size="small" onclick={copyZigbeeToken}>{t("zigbee.token")}</Button>
              {#if zigbeeToken}
                <input readonly value={zigbeeToken} onfocus={(e) => e.currentTarget.select()}
                  class="w-72 font-mono text-s" />
              {/if}
              {#if zigbeeTokenMsg}<span class="text-s text-dida-text-muted">{zigbeeTokenMsg}</span>{/if}
            </div>
          {/if}
          {#if zigbeeProfile.enabled}<ZigbeeTools />{/if}
        </div>
      {/if}
      {#if c.adapter === "frigate"}
        {@render frigateLocations(c)}
      {:else if c.adapter === "baba"}
        {@render babaLocations(c)}
      {:else if adapterDevices(c.adapter).length}
        <!-- Device cards go straight in (no "Fields" heading), same as esphome nodes -->
        <div class="mt-3 flex flex-col gap-2" class:border-t={c.fields.length} class:border-dida-border={c.fields.length} class:pt-3={c.fields.length}>
            {#each adapterDevices(c.adapter) as grp (grp.key)}
              {@render deviceRow(c, grp)}
            {/each}
        </div>
      {/if}
    {/if}
    </div>
    {/if}
  </div>
{/snippet}

{#if auth.isAdmin}
  <!-- Search bar — the same shape as the Devices page's filter panel -->
  <div class="mb-4 mt-6 flex flex-wrap items-center gap-2 rounded-lg border border-dida-border bg-dida-panel p-3">
    <input
      bind:value={adapterFilter}
      placeholder={t("adapters.filter")}
      class="min-w-[10rem] flex-1"
    />
    <span class="text-m text-dida-text-faint">{shownVisible} / {shownTotal}</span>
    {#if fq}
      <Button size="small" onclick={() => (adapterFilter = "")}>{t("filter.clear")}</Button>
    {/if}
  </div>

  <!-- 1) Find devices: one sweep across every discoverable adapter -->
  <div class="mb-4 rounded-lg border border-dida-border bg-dida-panel p-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <div class="text-m font-semibold">{t("discover.findTitle")}</div>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button tone="primary" onclick={scanAll} disabled={scanningAll || scannedAll}
        >{scanningAll ? t("discover.scanning") : t("discover.scan")}</Button>
        {#if scannedAll && !scanningAll}
          <Button size="small" onclick={scanAll}>{t("discover.rescan")}</Button>
        {/if}
      </div>
    </div>
    {#if scannedAll && !scanningAll}
      <p class="mt-2 text-s text-dida-text-muted">{foundCount ? `${t("discover.found")}: ${foundCount}` : t("discover.noneAll")}</p>
    {/if}
    {#if scannedAll && !scanningAll}
      {#each Object.entries(failedAll) as [adapter, reason] (adapter)}
        <p class="mt-1 text-s text-dida-danger">{t("discover.failed", { name: friendlyName(adapter) })}: {reason}</p>
      {/each}
    {/if}
    {#if scanAllMsg}<p class="mt-2 text-s text-dida-text-muted">{scanAllMsg}</p>{/if}
  </div>

  <!-- Grouped by KIND; each row carries its own running/stopped state + toggle. -->
  <!-- 2) Network devices — LAN device adapters, running first then stopped -->
  {#if devicesLive.length || deviceOffF.length}
    <h2 class="mb-2 mt-6 text-m font-semibold">{t("adapters.devicesTitle")}</h2>
    <div class="flex flex-col gap-3">
      {#each devicesLive as c (c.adapter)}{@render adapterCard(c)}{/each}
      {#each deviceOffF as c (c.adapter)}{@render offAdapterRow(c)}{/each}
    </div>
  {/if}

  <!-- 3) Accounts & cloud — credential adapters, running first then stopped -->
  {#if accountLive.length || accountOffF.length}
    <h2 class="mb-2 mt-6 text-m font-semibold">{t("adapters.accountsTitle")}</h2>
    <div class="flex flex-col gap-3">
      {#each accountLive as c (c.adapter)}{@render adapterCard(c)}{/each}
      {#each accountOffF as c (c.adapter)}{@render offAdapterRow(c)}{/each}
    </div>
  {/if}

  <!-- 4) Services — infrastructure profiles, each with its own on/off state -->
  {#if servicesF.length}
    <h2 class="mb-2 mt-6 text-m font-semibold">{t("adapters.servicesTitle")}</h2>
    <div class="flex flex-col gap-3">
      {#each servicesF as [name, p] (name)}{@render serviceRow(name, p)}{/each}
    </div>
  {/if}

  {#if fq && !devicesLive.length && !deviceOffF.length && !accountLive.length && !accountOffF.length && !servicesF.length}
    <p class="py-6 text-center text-m text-dida-text-faint">{t("adapters.filterNone")}</p>
  {/if}

{/if}

