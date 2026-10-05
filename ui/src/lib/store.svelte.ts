// Live device store. One snapshot (GET /entities + /state) seeds it; the
// WebSocket then streams validated deltas off the engine events stream. The
// API already fans one bus subscription out to all browsers, so this is just a
// thin client-side projection — the same "current state = projection of events"
// shape the engine uses server-side.

import { api, stateWs, Unauthorized, type Area, type Entity, type FloorPoly, type GlowShape, type MarkerStyle, type MediaConfig, type Scalar, type LiveEvent, type SensorConfig, type StateEvent } from "$lib/api";
import { auth } from "$lib/auth.svelte";
import { toasts } from "$lib/kit";
import { t, type MessageKey } from "$lib/i18n";
import { errMsg } from "$lib/errors";
import { tr } from "$lib/translations.svelte";

export type Conn = "connecting" | "live" | "offline";

export interface CapState {
  value: Scalar;
  unit: string | null;
  updatedAt: number; // epoch ms
}

export interface Device {
  entityId: string;
  name: string;             // effective display name: user label if set, else the adapter's
  rawName: string;          // the adapter-derived name (what "clear rename" falls back to)
  // The adapter gave this entity a name of its OWN, distinct from its device's. A
  // one-relay Shelly has none (its entity carries the device's name), so it shows
  // whatever the device was renamed to; an ESPHome node's eight named entities do,
  // so they never collapse into eight copies of the node's name.
  hasOwnName: boolean;
  label: string | null;     // user name override (null → none)
  adapter: string;
  deviceType: string | null;         // DIDA's canonical device kind (its own source of truth)
  areaId: number | null;
  diagnostic: boolean;
  category: string;         // display grouping: "control" | "config" | "diagnostic"
  exposed: boolean;         // user chose to expose it (false → hidden from Devices)
  voiceExposed: boolean | null; // explicit override; null = follow the house rule
  voiceEffective: boolean;      // what the Matter bridge uses (rule + any override)
  hiddenCaps: string[];     // individual capabilities the user hid from Devices
  capabilities: string[];   // the field's capability set ([] → type DIDA can't map)
  deviceKey: string | null; // groups entities of one physical device into a card
  fpFloor: string | null;
  fpX: number | null;
  fpY: number | null;
  // A presence sensor's MOTION marker is placed apart from its value label — its own
  // slot, so a one-entity motion+lux sensor still gets two independent positions.
  fpMotionFloor: string | null;
  fpMotionX: number | null;
  fpMotionY: number | null;
  fpGlow: GlowShape | null;  // floor-plan illumination radius override (null → area fallback)
  fpStyle: MarkerStyle | null; // floor-plan marker icon/size override (null → auto)
  lastSeen: number; // epoch ms
  reachable: boolean;          // device's adapter says it's there right now (false → dim + "last seen")
  reachableSince: number;      // epoch ms the current reachable value was set (0 → unknown)
  manualSince: number;         // epoch ms it was switched on by hand and held against rules (0 → not held)
  caps: Record<string, CapState>;
}

// A hostname or an IP is an identifier, not a phrase: "dida.example.com"
// has to survive verbatim instead of becoming "Dida.Example.Com", which
// names nothing that exists.
const ADDRESS = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/i;

export function prettify(s: string): string {
  const raw = s.trim();
  if (ADDRESS.test(raw)) return raw;
  return s.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim().replace(/\b\w/g, (c) => c.toUpperCase());
}
function friendlyName(entityId: string, name: string | null): string {
  const raw = name && name.trim()
    ? name.trim()
    : (entityId.includes(":") ? entityId.split(":").slice(1).join(":") : entityId);
  // Localise the adapter descriptor for the active language (identity for
  // device/user names, which have no translation). tr() reads the reactive map,
  // so callers in reactive contexts re-run on a language switch.
  const base = tr(raw);
  // Prettify slug-like labels ("living-room-strip" → "Living Room Strip"): no
  // spaces but separators, or all-lowercase. Already-nice / translated names pass
  // through (so a Croatian "Mrežno brojilo" isn't mangled by the ASCII \b\w rule).
  if (!/\s/.test(base) && (/[-_]/.test(base) || base === base.toLowerCase())) return prettify(base);
  return base;
}

function adapterOf(entityId: string): string {
  return entityId.includes(":") ? entityId.split(":")[0] : "?";
}

// Live events kept in memory. Small on purpose: this is a nudge for an already-
// open panel, not a log — the durable record is ClickHouse, fetched on open.
const LIVE_EVENT_CAP = 200;

class DeviceStore {
  byId = $state<Record<string, Device>>({});
  areas = $state<Area[]>([]);
  // Per-device friendly name (device_key → {name auto, label user}). One source.
  deviceMeta = $state<Record<string, { name: string | null; label: string | null; site: string | null }>>({});
  conn = $state<Conn>("connecting");
  // Device events pushed live over the WS (adapter offline, a rejected reading,
  // a rule firing). A bounded ring, not a log: the durable record is ClickHouse
  // and the detail panel fetches it on open — this exists so a panel that is
  // ALREADY open sees the next one arrive. Newest first.
  liveEvents = $state<LiveEvent[]>([]);
  error = $state<string | null>(null);

  /** A device's human display name: the user label, else a human-readable name
   *  GENERATED from the adapter's raw name ("pmis-bea-room" → "Pmis Bea Room").
   *  The raw name stays available (deviceMeta[key].name) as the technical id. */
  deviceLabel(deviceKey: string | null): string | null {
    if (!deviceKey) return null;
    const m = this.deviceMeta[deviceKey];
    if (!m) return null;
    // friendlyName applies tr() + prettifies ONLY slug-like names — so a translated
    // header ("Mrežno brojilo") passes through instead of being mangled to "MrežNo".
    return m.label ?? (m.name ? friendlyName("", m.name) : null);
  }
  /** The device's raw adapter name (e.g. the ESPHome node name), for reference. */
  deviceRawName(deviceKey: string | null): string | null {
    return deviceKey ? (this.deviceMeta[deviceKey]?.name ?? null) : null;
  }
  /** The installation a device stands at when it is not this house; null is here. */
  deviceSite(deviceKey: string | null): string | null {
    return deviceKey ? (this.deviceMeta[deviceKey]?.site ?? null) : null;
  }

  /** An entity's effective display name: the user's entity label wins; otherwise it
   *  inherits the DEVICE label — a rename on the device card must show on every
   *  surface (camera wall, floorplan, pickers), not just the settings card header.
   *  Inheritance is decided by `hasOwnName` (see the Device field). */
  #effectiveName(d: Device): string {
    if (d.label?.trim()) return d.label.trim();
    const devLabel = d.hasOwnName ? null : this.deviceMeta[d.deviceKey ?? d.entityId]?.label;
    return devLabel?.trim() ? devLabel.trim() : d.rawName;
  }

  /** Display label for a room: user name if set, else translated type, else unassigned. */
  roomLabel(a: Area): string {
    if (a.name && a.name.trim()) return a.name;
    if (a.kind) return t(`room.kind.${a.kind}` as MessageKey);
    return t("room.unassigned");
  }

  areaName(id: number | null): string {
    if (id === null) return t("room.unassigned");
    const a = this.areas.find((ar) => ar.id === id);
    return a ? this.roomLabel(a) : t("room.unassigned");
  }

  /** Areas ordered by their display label (Croatian collation) — for the
   *  assignment / filter dropdowns. Raw `areas` is in DB/id order, which reads
   *  as random to the user. */
  get sortedAreas(): Area[] {
    return [...this.areas].sort((a, b) =>
      this.roomLabel(a).localeCompare(this.roomLabel(b), "hr", { sensitivity: "base" }));
  }

  /** Set the room for a whole device (all its entities' area_id). Optimistic. */
  async setDeviceArea(deviceKey: string, areaId: number | null): Promise<void> {
    const prev: Record<string, number | null> = {};
    for (const d of Object.values(this.byId)) {
      if (d.deviceKey === deviceKey) { prev[d.entityId] = d.areaId; d.areaId = areaId; }
    }
    try {
      await api.setDeviceArea(deviceKey, areaId);
    } catch (e) {
      // Guard: an entity may have been reconciled away between the optimistic
      // write and this revert (snapshot deletion), so byId[id] can be gone.
      for (const [id, v] of Object.entries(prev)) { const d = this.byId[id]; if (d) d.areaId = v; }
      throw e;
    }
  }

  /** Override ONE entity's (gang's) type. null → auto. Optimistic. For a
   *  multi-gang device where one relay drives a light and another a plain switch. */
  /** Set ONE entity's (gang's) canonical type. Permanent — DIDA's truth, never
   *  reverted by an adapter re-announce. Optimistic. */
  async setEntityType(entityId: string, deviceType: string): Promise<void> {
    const d = this.byId[entityId];
    const prev = d?.deviceType ?? null;
    if (d) d.deviceType = deviceType; // optimistic
    try {
      await api.setEntityDeviceType(entityId, deviceType);
    } catch (e) {
      if (d) d.deviceType = prev; // revert
      throw e;
    }
  }

  /** Rename ONE entity (gang). Empty/null clears back to the adapter's own name.
   *  Optimistic — updates the effective display name at once. */
  async renameEntity(entityId: string, label: string | null): Promise<void> {
    const d = this.byId[entityId];
    const clean = label && label.trim() ? label.trim() : null;
    const prevLabel = d?.label ?? null, prevName = d?.name ?? "";
    if (d) { d.label = clean; d.name = this.#effectiveName(d); } // optimistic
    try {
      await api.renameEntity(entityId, clean);
    } catch (e) {
      if (d) { d.label = prevLabel; d.name = prevName; } // revert
      throw e;
    }
  }

  /** Set a whole device's canonical type (all its gangs). Permanent — DIDA's
   *  truth. Optimistic. Matches grouped (device_key) and ungrouped (the entity
   *  IS the device) alike, mirroring the server. */
  async setDeviceType(deviceKey: string, deviceType: string): Promise<void> {
    const prev: Record<string, string | null> = {};
    for (const d of Object.values(this.byId)) {
      if (d.deviceKey === deviceKey || d.entityId === deviceKey) {
        prev[d.entityId] = d.deviceType; d.deviceType = deviceType;
      }
    }
    try {
      await api.setDeviceType(deviceKey, deviceType);
    } catch (e) {
      for (const [id, v] of Object.entries(prev)) { const d = this.byId[id]; if (d) d.deviceType = v; }
      throw e;
    }
  }

  /** Place (or, with nulls, unplace) a device on the floorplan. Optimistic. */
  async place(entityId: string, floor: string | null, x: number | null, y: number | null): Promise<void> {
    await api.placeEntity(entityId, floor, x, y);
    const d = this.byId[entityId];
    if (d) {
      d.fpFloor = floor;
      d.fpX = x;
      d.fpY = y;
    }
  }

  /** Place (or unplace) a sensor's MOTION marker — its own slot, independent of the
   *  value-label placement above. Optimistic. */
  async placeMotion(entityId: string, floor: string | null, x: number | null, y: number | null): Promise<void> {
    await api.placeEntityMotion(entityId, floor, x, y);
    const d = this.byId[entityId];
    if (d) {
      d.fpMotionFloor = floor;
      d.fpMotionX = x;
      d.fpMotionY = y;
    }
  }

  /** Set (or clear, with null) a light's illumination radius on the plan. Optimistic. */
  async setGlow(entityId: string, glow: GlowShape | null): Promise<void> {
    const d = this.byId[entityId];
    const prev = d?.fpGlow ?? null;
    if (d) d.fpGlow = glow; // optimistic
    try {
      await api.setEntityGlow(entityId, glow);
    } catch (e) {
      if (d) d.fpGlow = prev; // revert
      throw e;
    }
  }

  /** Merge a marker-style change; a null value clears that key (→ auto). Optimistic. */
  async setStyle(entityId: string, patch: { icon?: string | null; scale?: number | null; color?: string | null }): Promise<void> {
    const d = this.byId[entityId];
    if (!d) return;
    const prev = d.fpStyle;
    const merged: MarkerStyle = { ...(prev ?? {}) };
    if ("icon" in patch) { if (patch.icon) merged.icon = patch.icon; else delete merged.icon; }
    if ("scale" in patch) { if (patch.scale) merged.scale = patch.scale; else delete merged.scale; }
    if ("color" in patch) { if (patch.color) merged.color = patch.color; else delete merged.color; }
    const next: MarkerStyle | null = merged.icon || merged.scale || merged.color ? merged : null; // empty → auto
    d.fpStyle = next; // optimistic
    try {
      await api.setEntityStyle(entityId, next);
    } catch (e) {
      d.fpStyle = prev; // revert
      throw e;
    }
  }

  /** Expose or hide one entity (field). Optimistic — the toggle flips at once. */
  async setExposed(entityId: string, exposed: boolean): Promise<void> {
    const d = this.byId[entityId];
    if (d) d.exposed = exposed; // optimistic
    try {
      await api.setEntityExposed(entityId, exposed);
    } catch (e) {
      if (d) d.exposed = !exposed; // revert on failure
      throw e;
    }
  }

  /** Expose or hide one entity to the voice assistant (Google/Matter). Optimistic. */
  async setVoiceExposed(entityId: string, voiceExposed: boolean): Promise<void> {
    const d = this.byId[entityId];
    const prev = d ? { ex: d.voiceExposed, eff: d.voiceEffective } : null;
    // Optimistic on BOTH: the effective value is what the icon shows, and an
    // explicit choice always wins over the rule, so they agree immediately.
    if (d) { d.voiceExposed = voiceExposed; d.voiceEffective = voiceExposed; }
    try {
      await api.setEntityVoiceExposed(entityId, voiceExposed);
    } catch (e) {
      if (d && prev) { d.voiceExposed = prev.ex; d.voiceEffective = prev.eff; }
      throw e;
    }
  }

  /** Show/hide ONE capability of a device. `exposed` (whole-entity) is derived:
   *  false when every real cap is hidden (so a single-cap adapter can stop reading). */
  async setCapExposed(entityId: string, cap: string, exposed: boolean): Promise<void> {
    const d = this.byId[entityId];
    if (!d) return;
    const hidden = new Set(d.hiddenCaps);
    if (exposed) hidden.delete(cap); else hidden.add(cap);
    const hiddenCaps = [...hidden];
    const real = d.capabilities.filter((c) => !c.endsWith("_options"));
    const entExposed = real.some((c) => !hidden.has(c)); // false → every cap hidden
    const prevHidden = d.hiddenCaps, prevExposed = d.exposed;
    d.hiddenCaps = hiddenCaps;
    d.exposed = entExposed; // optimistic
    try {
      await api.setEntityHidden(entityId, hiddenCaps, entExposed);
    } catch (e) {
      d.hiddenCaps = prevHidden;
      d.exposed = prevExposed; // revert
      throw e;
    }
  }

  /** Set a device's label (one devices row; entity labels untouched). Optimistic.
   *  The primary entity (entity_id === device_key) DISPLAYS the device label via
   *  #effectiveName, so its name is recomputed here. */
  async renameDevice(deviceKey: string, name: string | null): Promise<void> {
    const label = name?.trim() || null;
    const m = this.deviceMeta[deviceKey] ?? { name: null, label: null, site: null };
    const prev = m.label;
    const ent = this.byId[deviceKey];
    this.deviceMeta[deviceKey] = { ...m, label }; // optimistic
    if (ent) ent.name = this.#effectiveName(ent);
    try {
      await api.renameDevice(deviceKey, label);
    } catch (e) {
      this.deviceMeta[deviceKey] = { ...m, label: prev }; // revert
      if (ent) ent.name = this.#effectiveName(ent);
      throw e;
    }
  }

  /** Place (or unplace) a ROOM on the floorplan. Optimistic. */
  async placeArea(id: number, floor: string | null, x: number | null, y: number | null): Promise<void> {
    await api.placeArea(id, floor, x, y);
    const a = this.areas.find((ar) => ar.id === id);
    if (a) {
      a.fp_floor = floor;
      a.fp_x = x;
      a.fp_y = y;
    }
  }

  /** Trace (or clear, with null) a room's illumination polygon. Optimistic. */
  async setAreaPoly(id: number, poly: FloorPoly | null): Promise<void> {
    const a = this.areas.find((ar) => ar.id === id);
    const prev = a?.fp_poly ?? null;
    if (a) a.fp_poly = poly; // optimistic
    try {
      await api.setAreaPoly(id, poly);
    } catch (e) {
      if (a) a.fp_poly = prev; // revert
      throw e;
    }
  }

  /** Persist a room's aggregated sensor-label config. Optimistic. */
  async setAreaSensorConfig(id: number, config: SensorConfig | null): Promise<void> {
    const a = this.areas.find((ar) => ar.id === id);
    const prev = a?.sensor_config ?? null;
    if (a) a.sensor_config = config; // optimistic
    try {
      await api.setAreaSensorConfig(id, config);
    } catch (e) {
      if (a) a.sensor_config = prev; // revert
      throw e;
    }
  }

  /** Persist a room's media-source registry (the MediaHub picker). Optimistic. */
  async setAreaMediaConfig(id: number, config: MediaConfig | null): Promise<void> {
    const a = this.areas.find((ar) => ar.id === id);
    const prev = a?.media_config ?? null;
    if (a) a.media_config = config; // optimistic
    try {
      await api.setAreaMediaConfig(id, config);
    } catch (e) {
      if (a) a.media_config = prev; // revert
      throw e;
    }
  }

  devicesInArea(areaId: number): Device[] {
    return this.list.filter((d) => d.areaId === areaId);
  }

  #ws: WebSocket | null = null;
  #reconnect: ReturnType<typeof setTimeout> | null = null;
  #reconnectDelay = 1000; // grows on repeated failures, resets on a good connect
  #stopped = false;
  #heartbeat: ReturnType<typeof setInterval> | null = null;
  #lastMsg = 0; // wall time of the last inbound frame (event or pong)
  // Entities whose registry metadata (name, capabilities, device_key) we have.
  // A live state event for an unknown one triggers a debounced metadata refetch,
  // so a just-added device's fields show named + typed without a page reload.
  #metaKnown = new Set<string>();
  #metaTimer: ReturnType<typeof setTimeout> | null = null;

  /** Devices sorted by friendly name (Croatian collation). Memoised: this is read
   *  from the devices page, the camera wall, the media players and the floorplan,
   *  each inside its own $derived — as a plain getter every one of those re-sorted
   *  the whole house on every state tick. */
  #sorted = $derived(Object.values(this.byId).sort((a, b) => a.name.localeCompare(b.name, "hr")));
  get list(): Device[] {
    return this.#sorted;
  }

  get adapters(): string[] {
    return [...new Set(Object.values(this.byId).map((d) => d.adapter))].sort();
  }

  /** Devices that expose the media-player capability group (DLNA renderers …). */
  get mediaPlayers(): Device[] {
    return this.list.filter((d) => "media_transport" in d.caps);
  }

  /** All entities sharing a device_key (facets of one physical device), name-sorted
   *  so the AVR Main zone precedes Zone 2 and the player. */
  membersOf(deviceKey: string): Device[] {
    return this.list.filter((d) => d.deviceKey === deviceKey);
  }

  // Returns the *proxied* device so callers' mutations stay reactive.
  #ensure(entityId: string, adapter: string): Device {
    if (!this.byId[entityId]) {
      this.byId[entityId] = {
        entityId,
        name: friendlyName(entityId, null),
        rawName: friendlyName(entityId, null),
        hasOwnName: false,
        label: null,
        adapter,
        deviceType: null,
        areaId: null,
        diagnostic: false,
        category: "control",
        exposed: true,
        voiceExposed: null,
        voiceEffective: false,
        hiddenCaps: [],
        capabilities: [],
        deviceKey: null,
        fpFloor: null,
        fpX: null,
        fpY: null,
        fpMotionFloor: null,
        fpMotionX: null,
        fpMotionY: null,
        fpGlow: null,
        fpStyle: null,
        lastSeen: 0,
        reachable: true,
        reachableSince: 0,
        manualSince: 0,
        caps: {},
      };
    }
    return this.byId[entityId];
  }

  #applyEntityMeta(e: Entity): void {
    const d = this.#ensure(e.entity_id, e.adapter);
    d.rawName = friendlyName(e.entity_id, e.name);
    d.label = e.label ?? null;
    d.deviceKey = e.device_key;
    // Decided from the RAW names, before prettifying/translating either of them —
    // comparing a displayed name against a stored one never matches, and the whole
    // inheritance silently never fired.
    const own = (e.name ?? "").trim();
    const devRaw = (this.deviceMeta[e.device_key ?? e.entity_id]?.name ?? "").trim();
    d.hasOwnName = own !== "" && own !== devRaw && own !== e.device_key;
    d.name = this.#effectiveName(d);
    d.adapter = e.adapter;
    d.deviceType = e.device_type;
    d.areaId = e.area_id;
    d.diagnostic = e.diagnostic;
    d.category = e.category ?? "control";
    d.exposed = e.exposed;
    d.voiceExposed = e.voice_exposed;
    d.voiceEffective = e.voice_effective;
    d.hiddenCaps = e.hidden_caps ?? [];
    d.capabilities = e.capabilities;
    d.fpFloor = e.fp_floor;
    d.fpX = e.fp_x;
    d.fpY = e.fp_y;
    d.fpMotionFloor = e.fp_motion_floor;
    d.fpMotionX = e.fp_motion_x;
    d.fpMotionY = e.fp_motion_y;
    d.fpGlow = e.fp_glow ?? null;
    d.fpStyle = e.fp_style ?? null;
    d.lastSeen = Date.parse(e.last_seen) || 0;
    d.reachable = e.reachable ?? true;
    d.reachableSince = e.reachable_since ? Date.parse(e.reachable_since) || 0 : 0;
    d.manualSince = e.manual_since ? Date.parse(e.manual_since) || 0 : 0;
    this.#metaKnown.add(e.entity_id);
  }

  /** Refetch entity + device metadata (not state) — fills name/capabilities/room
   *  for entities that only arrived over the WS. Debounced by #scheduleMetaRefresh. */
  async #refreshMeta(): Promise<void> {
    const revision = auth.revision;
    try {
      const [entities, devs] = await Promise.all([api.listEntities(), api.listDevices()]);
      if (auth.revision !== revision) return;
      this.deviceMeta = Object.fromEntries(devs.map((d) => [d.device_key, { name: d.name, label: d.label, site: d.site }]));
      for (const e of entities) this.#applyEntityMeta(e);
    } catch { /* transient — the next unknown event reschedules */ }
  }

  #scheduleMetaRefresh(): void {
    if (this.#metaTimer) return; // one refetch coalesces a whole burst of adds
    this.#metaTimer = setTimeout(() => { this.#metaTimer = null; void this.#refreshMeta(); }, 1200);
  }

  /** Refetch the room list alone. Rooms are edited on one page but read by every
   *  assignment dropdown, filter and floor-plan surface, so the edit has to land
   *  HERE — a page-local copy leaves every other surface on yesterday's list until
   *  the tab is reloaded. */
  async refreshAreas(): Promise<void> {
    const revision = auth.revision;
    const areas = await api.listAreas();
    if (auth.revision === revision) this.areas = areas;
  }

  #loading: Promise<void> | null = null;
  #loadingRevision: number | null = null;
  #loadingProjection: number | null = null;
  #projection = 0;

  /** Seed/refresh from the snapshot. Single-flight: the layout effects and the WS
   *  onopen all call this at login (and again on a language switch), which fired
   *  three overlapping snapshot loads — 12 parallel requests racing each other's
   *  deletion reconcile. Concurrent callers now share the one in-flight load. */
  async load(): Promise<void> {
    if (this.replaying) return;
    const revision = auth.revision;
    const projection = this.#projection;
    if (this.#loading && this.#loadingRevision === revision && this.#loadingProjection === projection) return this.#loading;
    const loading = this.#load(revision, projection).finally(() => {
      if (this.#loading === loading) this.#loading = null;
    });
    this.#loadingRevision = revision;
    this.#loadingProjection = projection;
    this.#loading = loading;
    return loading;
  }

  async #load(revision: number, projection: number): Promise<void> {
    const loadStart = Date.now();  // WS deltas after this are live newcomers, not deletions
    try {
      const [entities, state, areas, devs] = await Promise.all([
        api.listEntities(), api.listState(), api.listAreas(), api.listDevices(),
      ]);
      if (auth.revision !== revision || this.#projection !== projection) return;
      this.areas = areas;
      this.deviceMeta = Object.fromEntries(devs.map((d) => [d.device_key, { name: d.name, label: d.label, site: d.site }]));
      for (const e of entities) this.#applyEntityMeta(e);
      for (const s of state) {
        const d = this.#ensure(s.entity_id, adapterOf(s.entity_id));
        const ts = Date.parse(s.updated_at) || 0;
        // A WS delta that landed between this snapshot's fetch and now can be
        // NEWER than the snapshot row (reconnect resync races the live stream);
        // don't let the older snapshot value regress the live one backwards.
        const existing = d.caps[s.capability];
        if (existing && existing.updatedAt > ts) continue;
        d.caps[s.capability] = { value: s.value, unit: s.unit, updatedAt: ts };
      }
      // Reconcile deletions: an entity removed server-side (e.g. a de-duplicated
      // or unpaired device) must vanish from the live view too — the snapshot is
      // authoritative. Without this, a full reload would be needed to clear it.
      const present = new Set<string>([...entities.map((e) => e.entity_id), ...state.map((s) => s.entity_id)]);
      for (const id of Object.keys(this.byId)) {
        if (!present.has(id)) {
          // But keep an entity that arrived over the WS DURING this load window
          // (reconnect resync races the live stream) — it's a live newcomer the
          // snapshot predates, not a server-side deletion.
          if (this.byId[id].lastSeen > loadStart) continue;
          delete this.byId[id];
          this.#metaKnown.delete(id);
        }
      }
      this.error = null;
    } catch (e) {
      if (auth.revision !== revision || this.#projection !== projection) return;
      if (e instanceof Unauthorized) {
        auth.user = null; // session expired → layout guard redirects to /login
        return;
      }
      this.error = errMsg(e);
    }
  }

  // ── replay: the store driven from the past instead of the socket ──
  // Every surface reads `byId`, so a past instant is shown by substituting these
  // values — there is no second renderer and no second store to keep in step.
  // Live deltas are dropped while this holds; leaving re-seeds from the snapshot.
  replaying = $state(false);
  replayAt = $state<number | null>(null);
  #replayUnits: Record<string, Record<string, string | null>> | null = null;
  #replayEntities = new Set<string>();

  /** Enter replay for `requested`. A capability the window has no history for is
   *  cleared rather than left at its live value — a plan that mixes "now" into
   *  the past is worse than one that admits it doesn't know. */
  enterReplay(requested: string[], covered: Record<string, string[]>): void {
    this.#projection++;
    // Units come from the registry, not from history, and the live ones are gone
    // after the first frame — capture them once.
    if (!this.#replayUnits) {
      const units: Record<string, Record<string, string | null>> = {};
      for (const [id, d] of Object.entries(this.byId)) {
        const per: Record<string, string | null> = {};
        for (const [c, cs] of Object.entries(d.caps)) per[c] = cs.unit;
        units[id] = per;
      }
      this.#replayUnits = units;
    }
    for (const id of requested) {
      this.#replayEntities.add(id);
      const d = this.byId[id];
      if (!d) continue;
      const keep = new Set(covered[id] ?? []);
      for (const c of Object.keys(d.caps)) if (!keep.has(c)) delete d.caps[c];
    }
    this.replaying = true;
  }

  /** Push one replay frame. Only changed capabilities are passed in, so this is a
   *  small write per frame however many entities the plan carries. A null value is
   *  a capability that carried nothing at that instant — it leaves the store
   *  rather than rendering as a zero. */
  applyReplay(at: number, values: Record<string, Record<string, Scalar | null>>): void {
    this.replayAt = at;
    for (const [id, caps] of Object.entries(values)) {
      this.#replayEntities.add(id);
      const d = this.byId[id];
      if (!d) continue;
      for (const [c, v] of Object.entries(caps)) {
        if (v === null) delete d.caps[c];
        else d.caps[c] = { value: v, unit: this.#replayUnits?.[id]?.[c] ?? null, updatedAt: at };
      }
    }
  }

  async exitReplay(): Promise<void> {
    if (!this.replaying) return;
    this.#projection++;
    for (const id of this.#replayEntities) {
      const d = this.byId[id];
      if (d) d.caps = {};
    }
    this.#replayEntities.clear();
    this.replaying = false;
    this.replayAt = null;
    this.#replayUnits = null;
    await this.load();
  }

  #applyEvent(ev: LiveEvent): void {
    this.liveEvents = [ev, ...this.liveEvents].slice(0, LIVE_EVENT_CAP);
    const held = this.byId[ev.entity_id];
    if (held && ev.kind === "manual_override") held.manualSince = ev.ms;
    if (held && ev.kind === "manual_released") held.manualSince = 0;
    // A refused command must CORRECT the picture, not linger as an optimistic
    // flip until someone refreshes: re-pull the snapshot (the true state) and say
    // why out loud. The reason part carries a dictionary translation.
    if (ev.kind === "command_failed") {
      const i = (ev.message ?? "").indexOf(": ");
      const msg = i > 0 ? `${ev.message.slice(0, i)}: ${tr(ev.message.slice(i + 2))}` : tr(ev.message ?? "");
      toasts.error(msg || t("card.cmdFailed"));
      void this.load();
    }
  }

  #apply(ev: StateEvent): void {
    if (this.replaying) return; // a past instant is on screen; exit re-seeds from /state
    const d = this.#ensure(ev.entity_id, adapterOf(ev.entity_id));
    d.caps[ev.capability] = { value: ev.value, unit: ev.unit, updatedAt: ev.ts_ns / 1e6 };
    // Group a just-added device's entities live (before any reload).
    if (ev.device && !d.deviceKey) d.deviceKey = ev.device;
    d.lastSeen = Date.now();
    // Refetch registry metadata when either (a) we see an entity for the first
    // time over the WS (so it shows named/typed, not "unsupported"), or (b) a
    // KNOWN entity reports a capability not yet in its registry list — a device
    // that reports incrementally (an IKEA light that announces on_off first, then
    // brightness once it dims) would otherwise be stuck showing only its first
    // control until a reconnect. The refetch surfaces the new control live.
    if (!this.#metaKnown.has(ev.entity_id) || !d.capabilities.includes(ev.capability)) {
      this.#scheduleMetaRefresh();
    }
  }

  start(): void {
    this.#stopped = false;
    // A phone that locks/backgrounds the tab freezes timers and quietly kills
    // the socket; the moment we're visible/online again, reconnect NOW instead
    // of waiting out the backoff — this is what makes mobile feel live.
    document.addEventListener("visibilitychange", this.#onWake);
    window.addEventListener("online", this.#onWake);
    this.#connect();
  }

  stop(): void {
    this.#stopped = true;
    document.removeEventListener("visibilitychange", this.#onWake);
    window.removeEventListener("online", this.#onWake);
    if (this.#reconnect) clearTimeout(this.#reconnect);
    // Also cancel a pending debounced meta refetch, else it fires after logout
    // and issues an authenticated fetch on a dead session.
    if (this.#metaTimer) { clearTimeout(this.#metaTimer); this.#metaTimer = null; }
    this.#stopHeartbeat();
    const ws = this.#ws;
    this.#ws = null;
    ws?.close();
    this.#clear();
  }

  #clear(): void {
    this.#projection++;
    this.byId = {};
    this.areas = [];
    this.deviceMeta = {};
    this.liveEvents = [];
    this.error = null;
    this.replaying = false;
    this.replayAt = null;
    this.#replayUnits = null;
    this.#replayEntities.clear();
    this.#metaKnown.clear();
  }

  #onWake = (): void => {
    if (this.#stopped || document.visibilityState !== "visible") return;
    if (this.#ws && this.#ws.readyState === WebSocket.OPEN) {
      // The socket may only LOOK open after a sleep (dead TCP under it). Probe
      // with a ping; no reply within 5 s → close it, which triggers reconnect.
      this.#ws.send("ping");
      const probed = Date.now();
      setTimeout(() => {
        if (this.#ws?.readyState === WebSocket.OPEN && this.#lastMsg < probed) this.#ws.close();
      }, 5000);
      return;
    }
    if (this.#ws && this.#ws.readyState === WebSocket.CONNECTING) return; // already at it
    if (this.#reconnect) { clearTimeout(this.#reconnect); this.#reconnect = null; }
    this.#reconnectDelay = 1000; // a wake-up is a fresh start, not another failure
    this.#connect();
  };

  #startHeartbeat(): void {
    this.#stopHeartbeat();
    // Ping every 30 s: keeps proxies from idling the socket out AND gives a
    // liveness watchdog — >75 s (two misses) without ANY inbound frame means
    // the connection is dead even if the browser still reports OPEN.
    this.#heartbeat = setInterval(() => {
      const ws = this.#ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - this.#lastMsg > 75_000) {
        ws.close(); // watchdog: onclose schedules the reconnect
        return;
      }
      ws.send("ping");
    }, 30_000);
  }

  #stopHeartbeat(): void {
    if (this.#heartbeat !== null) clearInterval(this.#heartbeat);
    this.#heartbeat = null;
  }

  #connect(): void {
    this.conn = "connecting";
    let ws: WebSocket;
    try {
      ws = stateWs();
    } catch {
      this.#scheduleReconnect();
      return;
    }
    this.#ws = ws;
    ws.onopen = () => {
      if (this.#ws !== ws) return;
      this.conn = "live";
      this.#reconnectDelay = 1000; // healthy connection — reset the backoff
      this.#lastMsg = Date.now();
      this.#startHeartbeat();
      // Resync the full snapshot on every (re)connect — the WS only carries
      // deltas, so a tab opened before devices existed (or a quiet, deduped
      // device) would otherwise never appear. Snapshot-on-connect + deltas.
      this.load();
    };
    ws.onmessage = (m) => {
      if (this.#ws !== ws) return;
      this.#lastMsg = Date.now();
      try {
        const data = JSON.parse(m.data);
        if (data?.type === "pong") return; // heartbeat reply, not an event
        if (data?.type === "event") { this.#applyEvent(data); return; }
        this.#apply(data);
      } catch { /* ignore malformed */ }
    };
    ws.onclose = (ev) => {
      if (this.#ws !== ws) return;
      this.conn = "offline";
      this.#stopHeartbeat();
      if (ev.code === 4401) {
        // Auth rejected at WS upgrade — don't hammer reconnect; bounce to login.
        auth.user = null;
        return;
      }
      if (ev.code === 1012) {
        this.#clear();
        void auth.load().then(() => { if (auth.user && this.#ws === ws) this.#scheduleReconnect(); });
        return;
      }
      this.#scheduleReconnect();
    };
    ws.onerror = () => { ws.close(); };
  }

  #scheduleReconnect(): void {
    if (this.#stopped) return;
    if (this.#reconnect) clearTimeout(this.#reconnect);
    // Exponential backoff + jitter, capped at 30s: an API outage otherwise makes
    // every open tab hammer reconnect every 2s in lockstep.
    const jitter = this.#reconnectDelay * 0.3 * Math.random();
    this.#reconnect = setTimeout(() => this.#connect(), this.#reconnectDelay + jitter);
    this.#reconnectDelay = Math.min(this.#reconnectDelay * 2, 30000);
  }
}

export const devices = new DeviceStore();
