<script lang="ts">
  import { browser } from "$app/environment";
  import { devices } from "$lib/store.svelte";
  import { api } from "$lib/api";
  import { Button, Card, Notice, i18n, keep, recall } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dateTime } from "$lib/dt";
  import { auth } from "$lib/auth.svelte";
  import type { Area, Floor, RemovedDevice } from "$lib/api";
  import { DEVICE_TYPES, typeLabel, adapterLabel, capMeta, capRank, formatValue, hasPresenceCap, presenceOccupied, stateColor } from "$lib/capabilities";
  import { groupDevices, areaRequired, areaExempt, type DeviceUnit } from "$lib/devices";
  import DeviceCard from "$lib/DeviceCard.svelte";
  import DeviceRow from "$lib/DeviceRow.svelte";
  import { roomLabelOff, roomSensorsByArea } from "$lib/floorplanItems";
  import { primaryCap, roomAggregate } from "$lib/roomSensors";
  import DeviceDetail from "$lib/DeviceDetail.svelte";
  import DeviceFilters from "$lib/DeviceFilters.svelte";
  import PresencePanel from "$lib/PresencePanel.svelte";
  import { SECTION_TITLE_CLASS } from "$lib/ui";

  const list = $derived(devices.list);
  let detailId = $state<string | null>(null);   // device-detail panel target

  // Sparklines: ONE request for the whole page, not one per card. Each unit
  // contributes its main NUMERIC reading (a sparkline of a text state is a flat
  // line that says nothing); the API answers them all in a single query.
  let sparks = $state<Record<string, number[]>>({});
  const SPARKABLE = new Set(["sensor", "number", "slider"]);
  function sparkCap(u: DeviceUnit): [string, string] | null {
    const cands: { id: string; cap: string }[] = [];
    for (const m of u.members) {
      for (const cap of Object.keys(m.caps)) {
        if (m.hiddenCaps.includes(cap)) continue;
        if (SPARKABLE.has(capMeta(cap).control) && typeof m.caps[cap]?.value === "number") {
          cands.push({ id: m.entityId, cap });
        }
      }
    }
    if (!cands.length) return null;
    cands.sort((a, b) => capRank(a.cap) - capRank(b.cap));
    return [cands[0].id, cands[0].cap];
  }
  // Keyed by entity_id, which is what the API returns — the card looks up its own.
  const sparkKeyFor = (u: DeviceUnit) => sparkCap(u)?.[0] ?? "";

  $effect(() => {
    const pairs = units.map(sparkCap).filter((p): p is [string, string] => p !== null);
    if (!pairs.length) { sparks = {}; return; }
    let live = true;
    api.sparklines(pairs).then((r) => { if (live) sparks = r; }).catch(() => { /* a card without a line is fine */ });
    return () => { live = false; };
  });
  const areas = $derived(devices.sortedAreas);
  // exposed === false → the user hid this field in adapter setup; keep it out of
  // Devices entirely (it still lives in Settings → Adapters for re-enabling).
  // Diagnostics that the user opted back in stay in — they render inside their
  // own device's card (collapsed), not in a disconnected global bucket.
  const curated = $derived(list.filter((d) => d.exposed));

  // Media/AV devices (the Marantz AVR, the iFi streamer) live on the Media page,
  // not here — they're media, not ordinary devices.
  function isAv(u: DeviceUnit): boolean {
    return u.type === "media" || u.members.some((m) => m.adapter === "heos" || m.adapter === "denon");
  }
  const units = $derived(groupDevices(curated).filter((u) => !isAv(u)));
  // Physical devices sitting in no room — they never show on the floor plan and
  // don't group by area, so surface a nudge (with the auto-assign fix) when any
  // exist. Non-spatial entities (presence/calendar/notify/astro) and optional
  // ones (derived/virtual) are area-exempt — see areaRequired — so they never nag.
  const unassignedCount = $derived(units.filter((u) => u.areaId === null && areaRequired(u)).length);

  // ── filter state (persisted, minus the transient search box) ──
  const KEY = "dida.devicefilters";
  function saved(): Record<string, unknown> {
    if (!browser) return {};
    try {
      return JSON.parse(localStorage.getItem(KEY) || "{}");
    } catch {
      return {};
    }
  }
  const init = saved();
  let q = $state("");
  let room = $state<string>((init.room as string) ?? "all");
  let type = $state<string>((init.type as string) ?? "all");
  let adapter = $state<string>((init.adapter as string) ?? "all");
  let onlyOn = $state<boolean>((init.onlyOn as boolean) ?? false);
  let groupBy = $state<string>((init.groupBy as string) ?? "room");

  $effect(() => {
    if (browser) {
      localStorage.setItem(KEY, JSON.stringify({ room, type, adapter, onlyOn, groupBy }));
    }
  });

  // Options come from the full set, so a filter never hides its own option.
  const availTypes = $derived(
    DEVICE_TYPES.filter((ty) => units.some((u) => u.type === ty)),
  );
  const availAdapters = $derived([...new Set(list.map((d) => d.adapter))].sort());

  function unitMatches(u: DeviceUnit, on: boolean): boolean {
    if (q) {
      const s = q.toLowerCase();
      if (
        !u.name.toLowerCase().includes(s) &&
        !u.members.some((m) => m.name.toLowerCase().includes(s) || m.entityId.toLowerCase().includes(s))
      )
        return false;
    }
    if (room !== "all") {
      if (room === "none" ? u.areaId !== null : u.areaId !== Number(room)) return false;
    }
    if (type !== "all" && u.type !== type) return false;
    if (adapter !== "all" && !u.adapters.includes(adapter)) return false;
    if (on && !u.members.some((m) => m.caps["on_off"]?.value === true)) return false;
    return true;
  }

  const shown = $derived(units.filter((u) => unitMatches(u, onlyOn)));

  // The page is folded: every device a one-line row that opens into its full card,
  // one at a time. By area the rooms stand under their floor, in the floor plan's
  // order; UNASSIGNED holds only devices that need a room (the set the nudge
  // counts); a device at another installation stands under that site; and what has
  // no room at all is sorted by what it is.
  type Section = { id: string; title: string; units: DeviceUnit[]; area?: Area; open: boolean };
  type Block = { id: string; title: string | null; sections: Section[] };

  let floors = $state<Floor[]>([]);
  $effect(() => { api.listFloors().then((f) => (floors = f)).catch(() => { floors = []; }); });

  const PEOPLE = new Set(["notify", "presence", "unifi"]);
  const HELPERS = new Set(["helper", "virtual", "derived", "astro"]);
  const CALENDARS = new Set(["calendar", "contacts"]);
  function elsewhere(u: DeviceUnit): string {
    if (u.type === "presence" || u.adapters.every((a) => PEOPLE.has(a))) return "people";
    if (u.adapters.every((a) => HELPERS.has(a))) return "helpers";
    if (u.adapters.every((a) => CALENDARS.has(a))) return "calendars";
    return "other";
  }
  const ELSEWHERE = ["people", "helpers", "calendars", "other"] as const;

  const typeRank = (u: DeviceUnit) => DEVICE_TYPES.indexOf(u.type);
  const inOrder = (us: DeviceUnit[]) =>
    [...us].sort((a, b) => typeRank(a) - typeRank(b) || a.name.localeCompare(b.name, "hr"));
  const byLabel = (a: Section, b: Section) => a.title.localeCompare(b.title, "hr");

  // What the person folded or unfolded, remembered per browser; a search unfolds
  // every section it finds something in.
  const OPEN_KEY = "dida.devices.open";
  let chosen = $state<Record<string, boolean>>((() => {
    try { return JSON.parse(recall(OPEN_KEY) ?? "{}"); } catch { return {}; }
  })());
  function choose(id: string, open: boolean) {
    chosen = { ...chosen, [id]: open };
    keep(OPEN_KEY, JSON.stringify(chosen));
  }
  const isOpen = (id: string, dflt: boolean) => !!q || (chosen[id] ?? dflt);

  let openKey = $state<string | null>(null);

  function section(id: string, title: string, us: DeviceUnit[], dflt: boolean, area?: Area): Section {
    return { id, title, units: inOrder(us), area, open: isOpen(id, dflt) };
  }

  const blocks = $derived.by<Block[]>(() => {
    const bucket = (key: (u: DeviceUnit) => string) => {
      const m = new Map<string, DeviceUnit[]>();
      for (const u of shown) { const k = key(u); const arr = m.get(k); if (arr) arr.push(u); else m.set(k, [u]); }
      return m;
    };
    if (groupBy === "type" || groupBy === "adapter") {
      const m = bucket((u) => (groupBy === "type" ? u.type : u.adapters[0]));
      const label = (k: string) => (groupBy === "type" ? typeLabel(k) : adapterLabel(k));
      return [{ id: groupBy, title: null, sections: [...m.entries()]
        .map(([k, us]) => section(`${groupBy}:${k}`, label(k), us, true)).sort(byLabel) }];
    }
    const placed = new Map<number, DeviceUnit[]>();
    const unassigned: DeviceUnit[] = [];
    const remote = new Map<string, DeviceUnit[]>();
    const rest = new Map<string, DeviceUnit[]>();
    for (const u of shown) {
      if (u.site) (remote.get(u.site) ?? remote.set(u.site, []).get(u.site)!).push(u);
      else if (u.areaId !== null && !areaExempt(u)) (placed.get(u.areaId) ?? placed.set(u.areaId, []).get(u.areaId)!).push(u);
      else if (areaRequired(u)) unassigned.push(u);
      else { const k = elsewhere(u); (rest.get(k) ?? rest.set(k, []).get(k)!).push(u); }
    }
    const out: Block[] = [];
    if (unassigned.length) {
      out.push({ id: "unassigned", title: null,
        sections: [section("unassigned", t("room.unassigned"), unassigned, true)] });
    }
    const floorKeys = new Set(floors.map((f) => f.key));
    const rooms = (keep: (a: Area) => boolean) => devices.areas
      .filter((a) => keep(a) && placed.has(a.id))
      .map((a) => section(`area:${a.id}`, devices.roomLabel(a), placed.get(a.id)!, true, a))
      .sort(byLabel);
    for (const f of [...floors].sort((a, b) => a.sort_order - b.sort_order)) {
      const ss = rooms((a) => a.fp_floor === f.key);
      if (ss.length) out.push({ id: `floor:${f.key}`, title: f.name, sections: ss });
    }
    const loose = rooms((a) => !a.fp_floor || !floorKeys.has(a.fp_floor));
    if (loose.length) out.push({ id: "floor:none", title: floors.length ? t("devices.otherAreas") : null, sections: loose });
    if (remote.size) {
      out.push({ id: "remote", title: t("devices.remote"), sections: [...remote.entries()]
        .map(([site, us]) => section(`site:${site}`, site, us, false)).sort(byLabel) });
    }
    const other = ELSEWHERE.filter((k) => rest.has(k))
      .map((k) => section(`elsewhere:${k}`, t(`devices.elsewhere.${k}`), rest.get(k)!, false));
    if (other.length) out.push({ id: "elsewhere", title: t("devices.noRoomGroup"), sections: other });
    return out;
  });

  // A room's heading says how it is without opening it: lights on, its temperature
  // (the room label the floor plan shows), whether anyone is there.
  const roomSensors = $derived(roomSensorsByArea());
  function roomSummary(s: Section) {
    const lit = s.units.filter((u) => u.members.some((m) => m.caps["on_off"]?.value === true && !m.hiddenCaps.includes("on_off"))).length;
    const prim = s.area && !roomLabelOff(s.area)
      ? primaryCap(roomAggregate(roomSensors.get(s.area.id) ?? [], s.area.sensor_config)) : null;
    const caps = s.units.flatMap((u) => u.members.map((m) =>
      Object.fromEntries(Object.entries(m.caps).filter(([c]) => !m.hiddenCaps.includes(c)))));
    const presence = s.area && hasPresenceCap(caps) ? presenceOccupied(caps) : null;
    return { lit, prim, presence };
  }
  const readingUnit = (cap: string): string => (cap === "temperature" ? "°" : (capMeta(cap).unit ?? ""));

  let busy = $state(false);
  let newRoom = $state("");

  async function assign(entityId: string, areaId: number | null) {
    await api.setEntityArea(entityId, areaId);
    await devices.load();
  }
  async function autoAssign() {
    busy = true;
    try {
      await api.autoAssignAreas();
      await devices.load();
    } finally {
      busy = false;
    }
  }
  async function addRoom() {
    if (!newRoom.trim()) return;
    await api.createArea(newRoom.trim());
    newRoom = "";
    await devices.load();
  }

  // Removed devices are invisible everywhere else by construction — their updates
  // are dropped before they reach the registry — so a device that refuses to come
  // back reads as a broken adapter until this list says otherwise.
  let removed = $state<RemovedDevice[]>([]);
  let restoring = $state("");
  const loadRemoved = async () => { removed = auth.isAdmin ? await api.removedDevices() : []; };
  $effect(() => { void loadRemoved(); });

  async function restore(key: string) {
    restoring = key;
    try {
      await api.restoreDevice(key);
      await loadRemoved();
      await devices.load();
    } finally {
      restoring = "";
    }
  }
</script>

<svelte:head><title>{t("nav.devices")}</title></svelte:head>

{#if devices.error}
  <Notice tone="err">{t("devices.loadError")}: {devices.error}</Notice>
{/if}

<PresencePanel />

{#if list.length === 0}
  <div class="rounded-lg border border-dashed border-dida-border p-8 text-center">
    <p class="text-dida-text-muted">{t("devices.empty.title")}</p>
    <p class="mt-1 text-m text-dida-text-faint">{t("devices.empty.hint")}</p>
  </div>
{:else}
  {#if auth.isAdmin && unassignedCount > 0}
    <!-- Nudge: some devices have no room. Auto-assign derives it from the entity
         id; whatever it can't place stays for manual assignment via the card. -->
    <Notice tone="warn">
      <div class="flex flex-wrap items-center gap-2 text-m">
        <span class="mr-1">{t("devices.unassignedWarn", { n: unassignedCount })}</span>
        <Button size="small" onclick={() => { room = "none"; type = "all"; adapter = "all"; q = ""; }}>{t("devices.showUnassigned")}</Button>
        <Button size="small" onclick={autoAssign} disabled={busy}>{busy ? t("devices.autoAssigning") : t("devices.autoAssign")}</Button>
      </div>
    </Notice>
  {/if}

  {#if auth.isAdmin}
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <input
        bind:value={newRoom} placeholder={t("devices.newRoom")}
        onkeydown={(e) => e.key === "Enter" && addRoom()}
       
      />
      <Button onclick={addRoom}>{t("devices.addRoom")}</Button>
    </div>
  {/if}

  <DeviceFilters
    bind:q bind:room bind:type bind:adapter bind:onlyOn bind:groupBy
    types={availTypes} adapters={availAdapters}
    visible={shown.length} total={units.length}
  />

  {#each blocks as b (b.id)}
    <section class="mb-6 flex flex-col gap-3">
      {#if b.title}<h2 class={SECTION_TITLE_CLASS}>{b.title}</h2>{/if}
      {#each b.sections as s (s.id)}
        {@const sum = roomSummary(s)}
        <Card collapsible open={s.open} ontoggle={(o) => choose(s.id, o)}>
          {#snippet heading()}
            <span class="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
              <span class="font-semibold">{s.title} <span class="font-normal text-dida-text-faint">· {s.units.length}</span></span>
              {#if sum.lit}<span class="text-s text-dida-accent">{t("devices.litCount", { n: sum.lit })}</span>{/if}
              {#if sum.prim?.mean != null}
                <span class="text-s font-semibold tabular-nums {stateColor(sum.prim.cap, sum.prim.mean)}"
                  >{formatValue(sum.prim.cap, sum.prim.mean)}{readingUnit(sum.prim.cap)}</span>
              {/if}
              {#if sum.presence !== null}
                <span class="text-s {sum.presence ? 'text-dida-ok' : 'text-dida-text-faint'}"
                  >{t(sum.presence ? "cap.occupancy.on" : "cap.occupancy.off")}</span>
              {/if}
            </span>
          {/snippet}
          <div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {#each s.units as u (u.key)}
              {#if openKey === u.key}
                <div class="col-span-full">
                  <DeviceCard members={u.members} name={u.name} deviceKey={u.key} type={u.type} {areas} onassign={assign}
                    ondetail={(id) => (detailId = id)} oncollapse={() => (openKey = null)} spark={sparks[sparkKeyFor(u)]} />
                </div>
              {:else}
                <DeviceRow members={u.members} name={u.name} type={u.type} onopen={() => (openKey = u.key)} />
              {/if}
            {/each}
          </div>
        </Card>
      {/each}
    </section>
  {:else}
    <p class="rounded-lg border border-dashed border-dida-border p-6 text-center text-m text-dida-text-faint">
      {t("filter.noMatch")}
    </p>
  {/each}
{/if}

{#if removed.length > 0}
  <section class="mb-6">
    <h2 class="mb-1 {SECTION_TITLE_CLASS}">
      {t("devices.removed.title")} <span class="font-normal text-dida-text-faint">· {removed.length}</span>
    </h2>
    <p class="mb-2 text-m text-dida-text-faint">{t("devices.removed.hint")}</p>
    <div class="divide-y divide-dida-border rounded-lg border border-dida-border">
      {#each removed as r (r.key)}
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-m">
          <span class="font-mono">{r.key}</span>
          {#if r.adapter}<span class="text-dida-text-faint">{adapterLabel(r.adapter)}</span>{/if}
          <span class="ml-auto text-s text-dida-text-faint">
            {dateTime(r.removed_at)}
          </span>
          <Button size="small" disabled={restoring === r.key} onclick={() => restore(r.key)}>
            {restoring === r.key ? t("devices.removed.restoring") : t("devices.removed.restore")}
          </Button>
        </div>
      {/each}
    </div>
  </section>
{/if}

<DeviceDetail entityId={detailId} onclose={() => (detailId = null)} />
