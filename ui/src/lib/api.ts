// Thin typed client for the DIDA API. The browser always goes through
// SvelteKit, which proxies /api/* to the FastAPI service (see vite.config.ts).

import { tr } from "$lib/translations.svelte";
import { ApiError, refusal, send, TRANSFER_MS, type Locale, type Sending, type Theme } from "$lib/kit";
import { t } from "$lib/i18n";

const API = "/api";

export type Scalar = boolean | number | string;

// Live system health (GET /system/stats) — engine throughput + adapter badges + DB.
export interface SystemStats {
  engine: {
    accepted: number;
    rejected: number;
    stale: number;
    backlog: number;
    uptime_s: number;
    history: { buffered: number; cap: number; dropped: number; connected: boolean };
  } | null;
  adapters: { name: string; state: string; detail: string }[];
  adapter_counts: Record<string, number>;
  db: { postgres: boolean; clickhouse: boolean };
}

// System-health alerts. `active` = currently firing (from the API's live evaluator);
// `history` = recent fired/resolved transitions (ClickHouse).
export interface ActiveAlert {
  key: string;
  scope: string;
  severity: "critical" | "warning";
  message: string;
  value: number;
  since: number; // epoch seconds
  silenced: boolean;
  silenced_until: string | null; // ISO; null while silenced = until it resolves
}
export interface AlertRecipients {
  recipients: string[];
  targets: { entity_id: string; name: string }[];
  routes: string[] | null; // targets that reach a device now; null = notify adapter silent
}
export interface OrphanRef {
  kind: string;   // automations | scenes | schedules | computed_helpers | areas | settings
  id: number | string;
  name: string;
  missing: string[];
}
export interface OrphansData {
  orphans: OrphanRef[];
  count: number;
  missing: string[];  // distinct ids, for "one rename broke six rules"
}
export interface AlertHistoryRow {
  ts: string;
  key: string;
  scope: string;
  severity: string;
  event: "fired" | "resolved";
  message: string;
  value: number;
}
export interface AlertsData {
  active: ActiveAlert[];
  history: AlertHistoryRow[];
}
export interface AlertRule {
  key: string;
  severity: "critical" | "warning";
  threshold: number | null;
  hold_s: number;
  enabled: boolean;
}

// App revision (GET /version). Git-derived: base MAJOR.MINOR from the VERSION
// file, patch = commit count, pinned by short SHA + branch + commit date.
export interface Revision {
  version: string;         // "0.1.249"
  base: string;            // "0.1"
  count: number;           // commit count
  sha: string;             // short commit hash
  branch: string;
  committed_at: string | null; // ISO-8601
  dirty: boolean;          // working tree had uncommitted changes at stamp time
}

// Floor-plan illumination override for one light. Radius (% of plan) around its
// marker; null → the light falls back to its area's polygon.
export interface GlowShape {
  r: number;
}
// Per-device floor-plan marker style: icon override (catalog name) + size scale
// + permanent pill tint (hex) — an ACTION marker (gate opener) stays visually
// distinct from the status markers around it in every state.
export interface MarkerStyle {
  icon?: string;
  scale?: number;
  color?: string;
  guarded?: boolean; // tap opens the control popover instead of toggling directly
                     // (a heavy switch — e.g. big-screen — shouldn't fire on a stray tap)
  effect?: string;   // marker glow style override — "laser" → beams instead of a flat room fill
}
export type FloorPoly = [number, number][]; // [[x,y],…] in % of the plan

export interface Entity {
  entity_id: string;
  name: string | null;
  label: string | null;         // user name override (null → adapter name)
  adapter: string;
  device_type: string | null;   // DIDA's canonical device kind (its own source of truth)
  area_id: number | null;
  diagnostic: boolean;
  category?: string; // "control" | "config" | "diagnostic" — device-card grouping
  exposed: boolean;
  voice_exposed: boolean | null; // explicit override; null = follow the rule
  voice_effective: boolean;      // what the bridge actually uses (rule + override)
  hidden_caps?: string[];
  device_key: string | null;
  fp_floor: string | null;
  fp_x: number | null;
  fp_y: number | null;
  fp_motion_floor: string | null; // motion marker, placed apart from the value label
  fp_motion_x: number | null;
  fp_motion_y: number | null;
  fp_glow: GlowShape | null;
  fp_style: MarkerStyle | null;
  capabilities: string[];
  last_seen: string;
  reachable?: boolean;          // device's adapter says it's there (absent on older payloads → treat as true)
  reachable_since?: string | null;
  manual_since?: string | null;  // switched on by hand: rules may not turn it off while its room is occupied
}

// A room's sensor-label config. Per-reading inclusion is DEFAULTED by device type (a
// pure ambient sensor is in; a thermostat — AC / TRV, has target_temperature — is out
// unless it's the room's only source for that cap). `excluded` / `included` are the
// user's OVERRIDES of that default; a thermostat's reading may be good ambient (an AC)
// or biased (a TRV by a radiator) and the caps can't tell them apart, so the user
// curates. Both keyed "entityId:cap".
export interface SensorConfig {
  hidden?: string[];   // capabilities not shown in the room label
  excluded?: string[]; // default-IN readings the user turned OFF
  included?: string[]; // default-OUT readings (a thermostat) the user turned ON
  order?: string[];    // custom capability order (drag & drop); the first is the collapsed value
  off?: boolean;       // suppress the whole room label (a wrongly-matched / unwanted room)
}

// One selectable media "source" = an experience the user picks in a room, mapped
// to its control mechanism. Internal AV → a Harmony activity (keeps the physical
// remote in sync); external streaming → a player routed through an AVR zone. The
// MediaHub widget renders these and hides which path it drives.
export type MediaSourceKind = "activity" | "player";
export interface MediaSource {
  key: string;                  // stable id within the area
  label: string;                // UI label
  icon?: string;                // deviceIcons key (falls back by kind)
  kind: MediaSourceKind;
  enabled?: boolean;            // false = configured but hidden from the picker (default: shown)
  // activity (internal — via Harmony):
  remote?: string;              // remote head entity_id (e.g. harmony:hub)
  activity?: string;            // Harmony activity label (the `source` value to set)
  avr?: string;                 // AVR main-zone entity to also power off when turning the activity off
                                //   (Harmony's activity power-off leaves it on — e.g. denon:marantz_main)
  avr_input?: string;           // the AVR main-zone input this activity routes (e.g. MUSIC) — lets the
                                //   hub recognise the source as live from DEVICE state when something
                                //   (an automation, the AVR knob) routed the amp without Harmony
  nowplaying?: string | null;   // entity to read now-playing/transport from (e.g. androidtv:shield)
  // player (external streaming → an AVR zone):
  player?: string;              // media-player entity_id (e.g. volumio:ifi)
  zone?: string;                // AVR zone entity_id for the route + volume (e.g. denon:marantz_zone2)
  zone_input?: string;          // AVR input the player feeds into (e.g. MUSIC)
  browse?: string[];            // browse tabs: "opus" (the OPUS shelf) | "radio"
  // Live-TV channel shortcuts (an androidtv app like Xplore): an ordered list of
  // channel names — the tune number is the 1-based position. Shown when the device's
  // current app equals tv_app.
  tv_app?: string;              // the app name that is live TV (its androidtv `source` value)
  channels?: string[];          // ordered channel names; number = index + 1
}
export interface MediaConfig {
  sources: MediaSource[];
}

// Heating. The house-wide half is one settings row; the per-room half hangs off
// the room. `enabled` is the commissioning gate — with it off the controller
// resolves and publishes everything but drives nothing.
export interface HeatingSettings {
  enabled: boolean;
  mode: string;              // auto | comfort | eco | night | away
  boiler: string;            // relay entity wired to the boiler's thermostat contact
  targets: Record<string, number>;  // the house setpoints every room runs
  schedule: HeatingSlot[];          // the house weekly schedule
  min_calling: number;              // rooms that must call before the burner starts
  hysteresis: number;        // °C below target before a room calls for heat
  deadband: number;          // °C above target before its call ends
  min_on_s: number;          // anti-cycling: shortest burn
  min_off_s: number;         // anti-cycling: shortest rest
  outdoor: string;
  summer_cutoff: number;
  away_helper: string;       // boolean entity: true → the away profile everywhere
  frost_target: number;
  stale_after_s: number;
  force_manual: boolean;     // hold TRVs off their own internal weekly programme
  window_detect: boolean;    // DIDA's own open-window detection, off the room thermometer
  window_drop: number;       // °C lost inside the window below = something is open
  window_minutes: number;
  window_recover: number;    // °C back up from the low = it's shut again
  window_max_pause_s: number;
  preheat: boolean;          // start early so the room ARRIVES at the scheduled temperature
  preheat_rate: number;      // minutes per °C of climb at the reference outdoor temp
  preheat_reference: number;
  preheat_cold_factor: number;
  preheat_max_minutes: number;
  frost_cold_below: number;  // outdoor °C under which the freeze guard starts rising
  frost_per_degree: number;
  frost_max: number;
  require_open_valve: boolean;
}

export interface HeatingSlot {
  days: number[];            // 0 = Mon … 6 = Sun
  at: string;                // local HH:MM
  profile: string;
}

// A room's config is an OVERLAY on the house: `offset` is how much warmer or cooler
// this room runs than everyone else, an empty schedule follows the house one, and
// sensor/valves are the room's own entities unless overridden.
export interface RoomHeatingConfig {
  enabled: boolean;
  sensor: string;
  valves: string[];
  offset: number;            // °C relative to the house setpoint
  schedule: HeatingSlot[];
  window_pause: boolean;
  can_call_boiler: boolean;  // false → takes heat when the boiler runs, never starts it
  override_target: number | null;
  override_until: number | null;  // epoch seconds
}

export interface HeatingRoom {
  area_id: number;
  name: string | null;
  kind: string | null;
  // What the room CONTAINS — the entities assigned to it, derived server-side.
  // Nothing to pick: the room already knows its thermometer and its valves.
  sensors: string[];
  valves: string[];
  config: RoomHeatingConfig;
}

export interface HeatingView {
  settings: HeatingSettings;
  profiles: string[];
  rooms: HeatingRoom[];
  orphan_valves: string[];  // heads assigned to no room — they cannot be heated
}

export interface Area {
  id: number;
  name: string | null; // optional user name; null → show translated `kind`
  kind: string | null; // canonical room type, translated in the UI
  fp_floor: string | null;
  fp_x: number | null;
  fp_y: number | null;
  fp_poly: FloorPoly | null;
  sensor_config: SensorConfig | null;
  media_config: MediaConfig | null;
}

// A level of the home. `key` is what areas/entities reference in fp_floor;
// `img_path` (null until uploaded) is the setup-time scaffold the vision service
// vectorizes — the consumer view renders pure vector, not this bitmap.
// One border piece as a rectangle [x, y, w, h] in % of the plan (the editable draft;
// the plan's walls seed it, but a patio/garden edge is a border too).
export type Border = [number, number, number, number];

export interface Floor {
  id: number;
  key: string;
  name: string;
  sort_order: number;
  img_path: string | null;
  img_w: number | null;
  img_h: number | null;
  borders: Border[] | null;
  switch_x: number | null; // floor-switch marker position (%); null → default corner
  switch_y: number | null;
  marker_scale: number | null; // home-wide marker multiplier, same on every row (null → 1)
}

export interface Zone {
  id: number;
  name: string;
  latitude: number;
  longitude: number;
  radius_m: number;
  is_home: boolean;
}

export interface StateRow {
  entity_id: string;
  capability: string;
  value: Scalar;
  unit: string | null;
  updated_at: string;
}

// One (entity, capability) across a replay window. `k` is how it was sampled:
// "s" keeps every change, "m" is one value per bucket. Points are [ms, value],
// already typed as the live store holds them; `at` is what it held when the
// window opened, and null anywhere means the capability carried nothing.
export interface ReplayTrack {
  e: string;
  c: string;
  k: "s" | "m";
  at: Scalar | null;
  p: [number, Scalar | null][];
}

export interface ReplayBundle {
  frm: number;
  to: number;
  bucket_seconds: number;
  truncated: boolean;
  tracks: ReplayTrack[];
}

/** Live delta pushed over the WebSocket (engine events stream). */
export interface StateEvent {
  entity_id: string;
  capability: string;
  value: Scalar;
  unit: string | null;
  ts_ns: number;
  device?: string | null; // device_key — groups a just-added device's entities live
}

/** How much and how often, over a period (GET /entities/{id}/stats). A capability
 *  with nothing to report is ABSENT, never zero — a zero would be a claim. */
export interface EntityStats {
  entity_id: string;
  days: number;
  counters: { capability: string; total: number; hours_seen: number }[];
  runtime: {
    capability: string; hours_on: number; cycles: number; duty: number; transitions: number;
    // Hours the answer actually covers — shorter than asked for when the entity is
    // younger than the period, or when the row cap bit (`truncated`).
    window_h: number; truncated: boolean;
  }[];
}

/** One durable application log line (GET /logs, admin only). */
export interface AppLogLine {
  ts: number;
  service: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  logger: string;
  entity_id: string;
  message: string;
  exc: string;
}

/** A device EVENT pushed over the same socket — something that happened, as
 *  opposed to a value that changed. `type` is what tells the two apart. */
export interface LiveEvent {
  type: "event";
  ms: number;
  entity_id: string;
  device_key: string;
  source: string;
  kind: string;
  severity: "debug" | "info" | "notice" | "warning" | "error";
  message: string;
}

export interface CommandIn {
  entity_id: string;
  capability: string;
  command: string;
  args?: Record<string, Scalar>;
}

// --- entry surface (/entry): access points (gates + door lock) ---
export type EntrySlotKind = "lock" | "cover" | "press" | "switch";
export interface EntrySlot {
  slot: "car" | "pedestrian" | "door";
  entity_id: string;
  name: string;
  kind: EntrySlotKind;
  // Optional live-status source (a DIFFERENT entity than the actuator): the door's
  // Zigbee contact, a gate's BABA occupancy zone, etc. `state_fallback` is used
  // only while the primary is silent (door: Tuya lock contact).
  state_entity: string | null;
  state_fallback: string | null;
}
export interface EntryConfig {
  slots: EntrySlot[];
  notify_on_open: boolean;
}
export interface EntryConfigIn {
  car: string | null;
  pedestrian: string | null;
  door: string | null;
  state_car: string | null;
  state_pedestrian: string | null;
  state_door: string | null;
  state_door_fallback: string | null;
  notify_on_open: boolean;
}

// --- automation definition (mirrors dida_core.automations) ---
export interface Trigger {
  entity_id: string;
  capability: string;
  to?: Scalar | null;
  for_seconds?: number | null; // hold: value must persist this long before firing
  id?: string; // label; a Starlark script branches on it via event["trigger_id"]
}
export interface Condition {
  entity_id: string;
  capability: string;
  op: string;
  value: Scalar;
  kind?: string; // "" = leaf; "and" | "or" | "not" = group over `conditions`
  conditions?: Condition[];
}
export interface Action {
  entity_id: string;
  capability: string;
  command: string;
  args?: Record<string, Scalar>;
  delay_ms?: number; // pause BEFORE this action (timed sequences, e.g. gate pulse)
}
export interface AutomationDef {
  triggers?: Trigger[]; // one or more triggers — any fires the rule
  conditions: Condition[];
  actions: Action[];
  script?: string; // Layer 2: non-empty selects a Starlark automation
  cooldown_seconds?: number | null; // silence repeat firings for this long after one
}
export interface Automation {
  id: number;
  name: string;
  enabled: boolean;
  definition: AutomationDef;
  last_triggered_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AutomationRun {
  id: number;
  automation_id: number;
  name: string;
  outcome: "fired" | "error" | "stale";
  detail: string | null;
  fired_at: string;
}

/** One recorded detection from a camera's archive (Frigate or BABA event).
 *  Frames are fetched on demand: Frigate via /camera/{id}/event/{id}/snapshot,
 *  BABA via /camera/{id}/thumb/{thumb}. */
export interface CameraEvent {
  id: string;
  camera: string;
  label: string;
  sub_label?: string | null;
  start_time: number;   // unix epoch seconds
  end_time?: number | null;
  has_snapshot?: boolean;
  has_clip?: boolean;
  top_score?: number | null;
  // BABA archive extras: track-thumbnail filename + the recorded window
  // (epoch seconds) a <video> can pull via /camera/{id}/clip?start&end.
  thumb?: string | null;
  clip_start?: number | null;
  clip_end?: number | null;
}

export interface Me {
  id: string;
  username: string;
  role: string;
  /** null = full consumer access; otherwise only these page keys are visible. */
  allowed_pages: string[] | null;
  /** false = view-only (cannot operate devices); scoped exceptions live server-side. */
  can_control: boolean;
  /** Saved UI preferences. null = no explicit choice → use the device default;
   * otherwise these follow the user across devices (applied on login). */
  theme: Theme | null;
  locale: Locale | null;
  /** an assistant key is set, so the assistant can answer */
  assistant: boolean;
}

/** A per-user control exception. Meaning flips on can_control: a deny when the
 * user may control by default, an allow when they are view-only. */
export interface ControlRule {
  scope: "entity" | "area" | "capability";
  ref: string;
}

/** A named loudness level (Quiet/Normal/Loud …) — a one-tap volume button. */
export interface VolumePreset {
  label: string;
  value: number;
}

/** Thrown by the client on a 401 so callers (the auth guard) can react. */
export class Unauthorized extends Error {}

// Send the session cookie on every request (same-origin via the /api proxy),
// and never serve API reads from the browser HTTP cache — a stale empty
// /entities snapshot from early in a session must not mask live data.
const FETCH_OPTS: RequestInit = { credentials: "include", cache: "no-store" };

// A language-model call is bounded only by the model's own ten-minute client
// timeout on the server, above the kit's deadline for an ordinary request.
const MODEL_MS = 10 * 60_000;

function request(path: string, init: Sending = {}): Promise<Response> {
  return send(`${API}${path}`, { ...FETCH_OPTS, ...init });
}

async function ok(r: Response): Promise<Response> {
  if (r.ok) return r;
  if (r.status === 401) throw new Unauthorized("unauthorized");
  const e = await refusal(r);
  // The backend emits English error detail; localise it through the same
  // translations dictionary as display names (fallback = the English text).
  throw new ApiError(e.status, tr(e.detail) || e.message, e.detail);
}

async function jsonOrThrow(r: Response) {
  await ok(r);
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  health: (): Promise<{ ok: boolean }> =>
    request(`/healthz`).then(jsonOrThrow),

  version: (): Promise<Revision> =>
    request(`/version`).then(jsonOrThrow),

  // --- auth ---
  me: (): Promise<Me> =>
    request(`/auth/me`).then(jsonOrThrow),

  login: (username: string, password: string): Promise<Me> =>
    request(`/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(jsonOrThrow),

  logout: (): Promise<null> =>
    request(`/auth/logout`, { method: "POST" }).then(jsonOrThrow),

  // Wall-panel auto-login: exchange the panel token (from the cast URL's ?k=)
  // for a normal session cookie for the `wallpanel` user.
  panelAuth: (k: string): Promise<Me> =>
    request(`/auth/panel`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ k }),
    }).then(jsonOrThrow),

  // Wall-panel telemetry: state transitions logged server-side (docker logs
  // dida-api) so a headless display's behaviour — tap delivery included — is
  // verifiable without standing in front of it. Fire-and-forget.
  panelEvent: (kind: "boot" | "tap" | "wake" | "saver" | "bell" | "sleep"): Promise<null> =>
    request(`/panel/event`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind }),
    }).then(jsonOrThrow),

  // Wall-panel liveness beat. Unlogged, and watched by the cast adapter: a panel
  // that stops beating gets re-cast, because a dead page keeps the Cast receiver
  // busy and so looks perfectly healthy over the protocol. Fire-and-forget.
  panelAlive: (): Promise<null> =>
    request(`/panel/alive`, { method: "POST" }).then(jsonOrThrow),

  // OPUS slideshow (wall-panel screensaver): a batch of random photo ids,
  // pixels fetched per-id via photoPreviewUrl so the next photo can preload.
  photosRandom: (count = 30): Promise<{ photos: SlideshowPhoto[] }> =>
    request(`/photos/random?count=${count}`).then(jsonOrThrow),
  photoPreviewUrl: (id: string): string => `${API}/photos/${encodeURIComponent(id)}/preview`,

  // Wall-panel display config (idle timeout, slideshow interval + effects).
  panelConfig: (): Promise<PanelConfig> =>
    request(`/panel/config`).then(jsonOrThrow),
  updatePanelConfig: (body: Partial<PanelConfig>): Promise<null> =>
    request(`/panel/config`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // Global AVR volume presets (Quiet/Normal/Loud …) shown on each zone's volume row.
  getVolumePresets: (): Promise<VolumePreset[]> =>
    request(`/media/volume-presets`).then(jsonOrThrow),
  setVolumePresets: (presets: VolumePreset[]): Promise<VolumePreset[]> =>
    request(`/media/volume-presets`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ presets }),
    }).then(jsonOrThrow),

  changePassword: (old_password: string, new_password: string): Promise<null> =>
    request(`/auth/change-password`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ old_password, new_password }),
    }).then(jsonOrThrow),

  // Persist the signed-in user's UI preferences (theme + language) to their
  // profile so a choice follows them across devices. Partial — send only what changed.
  setPrefs: (body: { theme?: Theme; locale?: Locale }): Promise<null> =>
    request(`/auth/prefs`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // Everything the plan's entities held across a past window, in one bundle —
  // the caller scrubs locally, so dragging the timeline costs no round-trip.
  replayBundle: (frm: number, to: number, entities: string[], signal?: AbortSignal): Promise<ReplayBundle> =>
    request(`/history/replay`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ frm, to, entities }),
      signal,
    }).then(jsonOrThrow),

  // --- devices ---
  listEntities: (): Promise<Entity[]> =>
    request(`/entities`).then(jsonOrThrow),

  listState: (): Promise<StateRow[]> =>
    request(`/state`).then(jsonOrThrow),

  // Everything about ONE entity in one call — registry row, live values with their
  // age, the siblings on the same physical device, the rules that act on it, and
  // (admins only) the recent command audit.
  // Thumbnail series for MANY entities at once — a page of device cards asks in one
  // round trip instead of one request per card.
  sparklines: (pairs: [string, string][], hours = 24): Promise<Record<string, number[]>> =>
    request(`/history/sparklines`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pairs, hours }),
    }).then(jsonOrThrow),

  appLogs: (p: { service?: string; level?: string; q?: string; hours?: number; limit?: number }):
    Promise<{ logs: AppLogLine[] }> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== "") qs.set(k, String(v));
    return request(`/logs?${qs}`).then(jsonOrThrow);
  },

  appLogServices: (): Promise<{ services: { service: string; count: number }[] }> =>
    request(`/logs/services`).then(jsonOrThrow),

  entityStats: (entityId: string, days = 30): Promise<EntityStats> =>
    request(`/entities/${encodeURIComponent(entityId)}/stats?days=${days}`)
      .then(jsonOrThrow),

  entityDetail: (entityId: string): Promise<EntityDetail> =>
    request(`/entities/${encodeURIComponent(entityId)}/detail`).then(jsonOrThrow),

  // The robot's own map, as a picture plus the geometry that scales it, and where
  // that picture has been laid over the floor plan.
  vacuumMap: (): Promise<VacuumMap> =>
    request(`/vacuum/map`).then(jsonOrThrow),

  vacuumLive: (): Promise<VacuumLive> =>
    request(`/vacuum/live`).then(jsonOrThrow),

  setVacuumPlacement: (body: VacuumPlacement): Promise<{ ok: boolean }> =>
    request(`/vacuum/map/placement`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  sendCommand: (body: CommandIn): Promise<{ ok: boolean }> =>
    request(`/command`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // --- entry surface (/entry) ---
  entryConfig: (): Promise<EntryConfig> =>
    request(`/entry/config`).then(jsonOrThrow),

  entryAction: (slot: EntrySlot["slot"]): Promise<{ ok: boolean }> =>
    request(`/entry/action`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slot }),
    }).then(jsonOrThrow),

  setEntryConfig: (body: EntryConfigIn): Promise<null> =>
    request(`/entry/config`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // --- camera wall layout (shared tile order + per-tile grid spans) ---
  cameraLayout: (): Promise<{ order: string[]; spans: Record<string, { c: number; r: number }> }> =>
    request(`/camera/layout`).then(jsonOrThrow),

  setCameraLayout: (order: string[], spans: Record<string, { c: number; r: number }>): Promise<null> =>
    request(`/camera/layout`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ order, spans }),
    }).then(jsonOrThrow),

  // Recent recorded detections behind a camera's live badge (newest first). A
  // camera without an events feed just returns []. `label` filters by object class.
  cameraEvents: (entityId: string, label = ""): Promise<{ events: CameraEvent[] }> =>
    request(`/camera/${encodeURIComponent(entityId)}/events?limit=8${label ? `&label=${encodeURIComponent(label)}` : ""}`)
      .then(jsonOrThrow),

  // --- automations ---
  listAutomations: (): Promise<Automation[]> =>
    request(`/automations`).then(jsonOrThrow),

  // Recent run log (newest first). Optional automationId filters to one rule.
  automationRuns: (automationId?: number, limit = 100): Promise<AutomationRun[]> =>
    request(`/automations/runs?limit=${limit}${automationId != null ? `&automation_id=${automationId}` : ""}`)
      .then(jsonOrThrow),

  // AI draft: NL description → validated (but unsaved) definition for review.
  // mode 'starlark' returns a sandbox-checked Layer-2 script instead of a typed rule.
  synthesizeAutomation: (description: string, mode: "typed" | "starlark" = "typed"): Promise<{ definition: AutomationDef }> =>
    request(`/automations/synthesize`, {
      method: "POST", deadlineMs: MODEL_MS,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description, mode }),
    }).then(jsonOrThrow),

  // AI explanation + "would it fire now / why not" diagnosis for one rule.
  explainAutomation: (id: number): Promise<{ explanation: string }> =>
    request(`/automations/${id}/explain`, { method: "POST", deadlineMs: MODEL_MS }).then(jsonOrThrow),

  // Compile + dry-run a Starlark script before saving: {ok, error?, commands?}.
  checkScript: (script: string, trigger?: { entity_id: string; capability: string }): Promise<{ ok: boolean; error?: string; commands?: string[] }> =>
    request(`/automations/check-script`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ script, trigger }),
    }).then(jsonOrThrow),

  // Force-run a rule's actions now (skips trigger + conditions). Drives devices.
  runAutomation: (id: number): Promise<{ ok: boolean }> =>
    request(`/automations/${id}/run`, { method: "POST" }).then(jsonOrThrow),

  createAutomation: (name: string, definition: AutomationDef, enabled = true): Promise<Automation> =>
    request(`/automations`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, definition, enabled }),
    }).then(jsonOrThrow),

  updateAutomation: (id: number, name: string, definition: AutomationDef, enabled: boolean): Promise<Automation> =>
    request(`/automations/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, definition, enabled }),
    }).then(jsonOrThrow),

  setAutomationEnabled: (id: number, enabled: boolean): Promise<Automation> =>
    request(`/automations/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(jsonOrThrow),

  deleteAutomation: (id: number): Promise<null> =>
    request(`/automations/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- areas / rooms ---
  listAreas: (): Promise<Area[]> =>
    request(`/areas`).then(jsonOrThrow),

  createArea: (name: string): Promise<Area> =>
    request(`/areas`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(jsonOrThrow),

  // Rename (name) and/or retype (kind) a room. null clears the field.
  updateArea: (id: number, patch: { name?: string | null; kind?: string | null }): Promise<Area> =>
    request(`/areas/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then(jsonOrThrow),

  deleteArea: (id: number): Promise<null> =>
    request(`/areas/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- heating ---
  getHeating: (): Promise<HeatingView> =>
    request(`/heating`).then(jsonOrThrow),

  setHeatingSettings: (settings: HeatingSettings): Promise<HeatingSettings> =>
    request(`/heating/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(settings),
    }).then(jsonOrThrow),

  setHeatingRoom: (areaId: number, config: RoomHeatingConfig): Promise<{ config: RoomHeatingConfig }> =>
    request(`/heating/rooms/${areaId}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(config),
    }).then(jsonOrThrow),

  deleteHeatingRoom: (areaId: number): Promise<null> =>
    request(`/heating/rooms/${areaId}`, { method: "DELETE" }).then(jsonOrThrow),

  // Hold a room at a temperature for a while; the schedule takes over after that.
  boostHeatingRoom: (areaId: number, target: number, minutes: number): Promise<{ config: RoomHeatingConfig }> =>
    request(`/heating/rooms/${areaId}/boost`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ target, minutes }),
    }).then(jsonOrThrow),

  clearHeatingBoost: (areaId: number): Promise<{ config: RoomHeatingConfig }> =>
    request(`/heating/rooms/${areaId}/boost`, { method: "DELETE" }).then(jsonOrThrow),

  placeArea: (
    id: number, fp_floor: string | null, fp_x: number | null, fp_y: number | null,
  ): Promise<Area> =>
    request(`/areas/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_floor, fp_x, fp_y }),
    }).then(jsonOrThrow),

  // Trace (or clear, with null) a room's illumination polygon on the floor plan.
  setAreaPoly: (id: number, fp_poly: FloorPoly | null): Promise<Area> =>
    request(`/areas/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_poly }),
    }).then(jsonOrThrow),

  // Persist a room's aggregated sensor-label config (hidden caps, excluded means, primary).
  setAreaSensorConfig: (id: number, sensor_config: SensorConfig | null): Promise<Area> =>
    request(`/areas/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sensor_config }),
    }).then(jsonOrThrow),

  // Persist a room's media-source registry (the MediaHub picker's sources).
  setAreaMediaConfig: (id: number, media_config: MediaConfig | null): Promise<Area> =>
    request(`/areas/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ media_config }),
    }).then(jsonOrThrow),

  setEntityArea: (entityId: string, area_id: number | null): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ area_id }),
    }).then(jsonOrThrow),

  // Override ONE entity's (gang's) type. null → clear back to auto.
  // Set ONE entity's (gang's) canonical type. DIDA's truth from here on.
  setEntityDeviceType: (entityId: string, device_type: string): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ device_type }),
    }).then(jsonOrThrow),

  // Rename ONE entity (gang). Empty/null clears back to the adapter's own name.
  renameEntity: (entityId: string, label: string | null): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ label }),
    }).then(jsonOrThrow),

  setEntityExposed: (entityId: string, exposed: boolean): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ exposed }),
    }).then(jsonOrThrow),

  // Push this entity to the voice assistant (Google/Matter) — a separate axis
  // from `exposed` (which is DIDA-app visibility). The Matter bridge filters on it.
  setEntityVoiceExposed: (entityId: string, voice_exposed: boolean): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ voice_exposed }),
    }).then(jsonOrThrow),

  // Hide/show individual capabilities of a device; `exposed` (whole-entity) is
  // derived by the caller (false when every cap is hidden → adapter can stop reading).
  setEntityHidden: (entityId: string, hidden_caps: string[], exposed: boolean): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ hidden_caps, exposed }),
    }).then(jsonOrThrow),

  autoAssignAreas: (): Promise<{ assigned: number; rooms: number }> =>
    request(`/areas/auto-assign`, { method: "POST" }).then(jsonOrThrow),

  // --- floors (home levels + their scaffold plan image) ---
  listFloors: (): Promise<Floor[]> =>
    request(`/floors`).then(jsonOrThrow),

  createFloor: (name: string): Promise<Floor> =>
    request(`/floors`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(jsonOrThrow),

  updateFloor: (id: number, patch: { name?: string; sort_order?: number; switch_x?: number; switch_y?: number; marker_scale?: number }): Promise<Floor> =>
    request(`/floors/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then(jsonOrThrow),

  deleteFloor: (id: number): Promise<null> =>
    request(`/floors/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // Upload/replace a floor's scaffold image. Raw bytes; w/h are the natural px
  // dims (the browser already has them from decoding for preview).
  uploadFloorImage: (id: number, file: Blob, w: number, h: number): Promise<Floor> =>
    request(`/floors/${id}/image?w=${w}&h=${h}`, {
      method: "PUT",
      headers: { "content-type": file.type },
      body: file,
    }).then(jsonOrThrow),

  // Auto-detect the border skeleton as editable [x,y,w,h] % rects (not persisted).
  detectBorders: (key: string): Promise<{ borders: Border[] }> =>
    request(`/floors/${encodeURIComponent(key)}/detect-borders`, { method: "POST" }).then(jsonOrThrow),

  // Persist the user's confirmed/edited border set.
  saveFloorBorders: (id: number, borders: Border[]): Promise<Floor> =>
    request(`/floors/${id}/borders`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ borders }),
    }).then(jsonOrThrow),

  // Enclosed room polygons from a border set (the current edit, or the saved one).
  roomsFromBorders: (key: string, borders?: Border[]): Promise<{ rooms: FloorPoly[] }> =>
    request(`/floors/${encodeURIComponent(key)}/rooms-from-borders`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(borders ? { borders } : {}),
    }).then(jsonOrThrow),

  // Merge two adjacent areas by ERASING the border between them (borders are the source
  // of truth). Returns the thinned set + the erased pieces for the red preview;
  // removed == [] means the two areas are not adjacent.
  eraseBorder: (key: string, borders: Border[], polys: FloorPoly[]): Promise<{ borders: Border[]; removed: Border[] }> =>
    request(`/floors/${encodeURIComponent(key)}/erase-border`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ borders, polys }),
    }).then(jsonOrThrow),

  // --- zones (presence geofence regions) ---
  listZones: (): Promise<Zone[]> =>
    request(`/zones`).then(jsonOrThrow),

  createZone: (z: Omit<Zone, "id">): Promise<Zone> =>
    request(`/zones`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(z),
    }).then(jsonOrThrow),

  updateZone: (id: number, patch: Partial<Omit<Zone, "id">>): Promise<Zone> =>
    request(`/zones/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then(jsonOrThrow),

  deleteZone: (id: number): Promise<null> =>
    request(`/zones/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- presence (this device reports its own location while the app is open) ---
  reportPresence: (
    latitude: number, longitude: number, tst: number, accuracy?: number, battery?: number,
  ): Promise<{ accepted: boolean; zone?: string; reason?: string }> =>
    request(`/presence/report`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ latitude, longitude, accuracy, battery, tst }),
    }).then(jsonOrThrow),

  // Last known real zone (not the network "away") + when, per presence entity —
  // for showing "was at Island House · 2h ago" once the live signal lapses.
  presenceLastLocations: (): Promise<Record<string, { zone: string; ts: number }>> =>
    request(`/presence/last-locations`).then(jsonOrThrow),

  // Which presence people are curated OUT of the panel (global display choice).
  presenceHidden: (): Promise<{ hidden: string[] }> =>
    request(`/presence/hidden`).then(jsonOrThrow),

  // Admin: set the hidden-from-presence list (presence:* entity_ids).
  setPresenceHidden: (hidden: string[]): Promise<{ hidden: string[] }> =>
    request(`/presence/hidden`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    }).then(jsonOrThrow),

  // --- web push (this device receives notifications while the app is closed) ---
  pushVapidKey: (): Promise<{ key: string }> =>
    request(`/push/vapid-key`).then(jsonOrThrow),

  // "foreign": the endpoint is already stored for another user (409), not an error.
  pushSubscribe: (sub: { endpoint: string; keys: { p256dh: string; auth: string } }): Promise<"stored" | "foreign"> =>
    request(`/push/subscribe`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(sub),
    }).then(async (r) => (r.status === 409 ? "foreign" : (await ok(r), "stored"))),

  pushOwner: (endpoint: string): Promise<{ owner: "self" | "other" | "none" }> =>
    request(`/push/owner`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ endpoint }),
    }).then(jsonOrThrow),

  pushUnsubscribe: (endpoint: string): Promise<null> =>
    request(`/push/unsubscribe`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ endpoint }),
    }).then(jsonOrThrow),

  pushTest: (message?: string): Promise<{ ok: boolean; subscriptions: number }> =>
    request(`/push/test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message }),
    }).then(jsonOrThrow),

  // Notification targets for the automations builder — notify:* entities are
  // write-only (never in /state), so this directory is how the UI learns them.
  notifyTargets: (): Promise<{ entity_id: string; label: string }[]> =>
    request(`/notify/targets`).then(jsonOrThrow),

  // --- floorplan placement ---
  placeEntity: (
    entityId: string, fp_floor: string | null, fp_x: number | null, fp_y: number | null,
  ): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_floor, fp_x, fp_y }),
    }).then(jsonOrThrow),

  // Place (or clear) a sensor's MOTION marker — its own slot, apart from the value label.
  placeEntityMotion: (
    entityId: string, fp_motion_floor: string | null, fp_motion_x: number | null, fp_motion_y: number | null,
  ): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_motion_floor, fp_motion_x, fp_motion_y }),
    }).then(jsonOrThrow),

  // Set (or clear, with null) a light's illumination radius override on the plan.
  setEntityGlow: (entityId: string, fp_glow: GlowShape | null): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_glow }),
    }).then(jsonOrThrow),

  // Set (or clear, with null) a device's floor-plan marker style (icon + size).
  setEntityStyle: (entityId: string, fp_style: MarkerStyle | null): Promise<unknown> =>
    request(`/entities/${encodeURIComponent(entityId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fp_style }),
    }).then(jsonOrThrow),

  // --- history (ClickHouse firehose + rollups) ---
  // Multi-series: one entity, N capabilities, one time window. The tier
  // (raw / hourly / daily) is chosen server-side by range.
  history: (entityId: string, capabilities: string[], hours: number): Promise<HistoryResponse> =>
    request(
      `/history?entity_id=${encodeURIComponent(entityId)}` +
        `&capabilities=${encodeURIComponent(capabilities.join(","))}&hours=${hours}`,
    ).then(jsonOrThrow),

  // The last value a capability HELD and when it stopped — for a reading whose
  // empty state says nothing (a parking place with no car standing in it).
  lastNonEmpty: (entityId: string, capability: string): Promise<LastNonEmpty> =>
    request(
      `/history/last-nonempty?entity_id=${encodeURIComponent(entityId)}` +
        `&capability=${encodeURIComponent(capability)}`,
    ).then(jsonOrThrow),

  // Cumulative house-energy balance over the last `days` days (History dashboard).
  energyHistory: (days: number): Promise<EnergyResponse> =>
    request(`/history/energy?days=${days}`).then(jsonOrThrow),

  // Per-hour energy balance for one day (unix-second [frm,to) local-day window).
  energyHourly: (frm: number, to: number): Promise<EnergyHourly> =>
    request(`/history/energy/hourly?frm=${frm}&to=${to}`).then(jsonOrThrow),

  // Command audit trail (admin): who told which entity what, newest first.
  commandHistory: (opts: { entityId?: string; source?: string; hours?: number; limit?: number } = {}): Promise<{ commands: CommandLogEntry[] }> => {
    const q = new URLSearchParams();
    if (opts.entityId) q.set("entity_id", opts.entityId);
    if (opts.source) q.set("source", opts.source);
    if (opts.hours) q.set("hours", String(opts.hours));
    if (opts.limit) q.set("limit", String(opts.limit));
    return request(`/history/commands?${q}`).then(jsonOrThrow);
  },

  // Energy-meter role config: every metering entity + its assigned/suggested role.
  energyConfig: (): Promise<EnergyConfig> =>
    request(`/history/energy/config`).then(jsonOrThrow),
  saveEnergyConfig: (roles: Record<string, string>): Promise<null> =>
    request(`/history/energy/config`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ roles }),
    }).then(jsonOrThrow),

  // --- retention policy (admin) ---
  getRetention: (): Promise<RetentionPolicy> =>
    request(`/retention`).then(jsonOrThrow),

  saveRetention: (body: {
    classes: RetentionClass[];
    capabilities: Record<string, string>;
    overrides: RetentionOverride[];
  }): Promise<{ ok: boolean }> =>
    request(`/retention`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // --- OPUS · Player: the shelf and the stations, read through the house's door ---
  opusStatus: (): Promise<OpusStatus> =>
    request(`/opus/status`).then(jsonOrThrow),
  opusArtists: (): Promise<OpusArtist[]> =>
    request(`/opus/artists`).then(jsonOrThrow),
  opusArtist: (id: number): Promise<OpusArtistDetail> =>
    request(`/opus/artist/${id}`).then(jsonOrThrow),
  opusRelease: (id: number): Promise<OpusRelease> =>
    request(`/opus/release/${id}`).then(jsonOrThrow),
  opusStations: (): Promise<OpusStation[]> =>
    request(`/opus/radio/stations`).then(jsonOrThrow),
  // A picture through the player's own picture route (the browser never meets OPUS).
  opusArt: (u: string | null | undefined, w = 320): string | null =>
    u ? `${API}/opus/art?u=${encodeURIComponent(u)}&w=${w}` : null,
  // The OPUS app on the television: its places, what is in them, and opening one.
  opusTvLinks: (): Promise<OpusTvLink[]> =>
    request(`/opus/tv/links`).then(jsonOrThrow),
  opusTvItems: (key: string, lang: string): Promise<OpusTvItem[]> =>
    request(`/opus/tv/items/${encodeURIComponent(key)}?lang=${lang}`).then(jsonOrThrow),
  opusTvOpen: (body: { link: string } | { kind: string; id: number; lang: string }): Promise<{ ok: boolean }> =>
    request(`/opus/tv/open`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  // Put a record, a song or a station on a device in the house.
  opusPlay: (body: { player_id: string; kind: "release" | "track" | "station"; id: number; start?: number }): Promise<{ ok: boolean; title: string }> =>
    request(`/opus/play`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // Audio signal-path for a renderer (source → speaker, measured per stage).
  mediaPipeline: (entityId: string): Promise<Pipeline> =>
    request(`/media/pipeline/${encodeURIComponent(entityId)}`).then(jsonOrThrow),

  // Lyrics for whatever a renderer is playing — uniform; the API dispatches by source.
  mediaLyrics: (entityId: string): Promise<{ text: string | null; subtitles: string | null }> =>
    request(`/media/lyrics/${encodeURIComponent(entityId)}`).then(jsonOrThrow),

  // --- assistant (Claude) ---
  // Server-sent events, not JSON: a turn that touches several tools runs for tens of
  // seconds, and `onTool` is what distinguishes "working" from "wedged". Resolves
  // with the terminal `done` event. Non-2xx still arrives as a normal status (the
  // endpoint's gate checks run before the stream opens), so jsonOrThrow's error
  // handling — including 401 -> Unauthorized — is reused verbatim.
  // No locale is sent: the assistant answers in the language of the MESSAGE, not of
  // the app, so a Croatian question in an English UI comes back in Croatian — and
  // the prompt stays byte-identical for everyone, which is what lets it be cached.
  askAssistant: async (
    message: string, history: AssistantTurn[] = [],
    onTool?: (name: string) => void,
    signal?: AbortSignal,
  ): Promise<AssistantReply> => {
    const r = await request(`/assistant`, {
      method: "POST", deadlineMs: MODEL_MS, signal,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message, history }),
    });
    signal?.throwIfAborted();
    if (!r.body) return jsonOrThrow(r);
    await ok(r);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done: AssistantReply | null = null;
    for (;;) {
      const { value, done: finished } = await reader.read().catch(() => {
        signal?.throwIfAborted();
        throw new Error(t("assistant.cutOff"));
      });
      signal?.throwIfAborted();
      if (finished) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line; keep the trailing partial.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5).trim());
        if (event.type === "tool") onTool?.(event.name);
        else if (event.type === "done") done = { reply: event.reply, actions: event.actions };
        else if (event.type === "error") throw new Error(tr(event.detail ?? "") || t("assistant.failed"));
      }
    }
    if (!done) throw new Error(t("assistant.cutOff"));
    return done;
  },

  // --- settings (admin) ---
  getTranslations: (lang: string): Promise<Record<string, string>> =>
    request(`/translations?lang=${encodeURIComponent(lang)}`).then(jsonOrThrow),

  listTranslatableKeys: (): Promise<{ key: string; langs: Record<string, string> }[]> =>
    request(`/translations/keys`).then(jsonOrThrow),

  putTranslation: (key: string, lang: string, value: string): Promise<{ ok: boolean }> =>
    request(`/translations`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key, lang, value }),
    }).then(jsonOrThrow),

  // Machine-translate every still-missing descriptor for `lang` via the assistant
  // LLM. Fills only empty rows; curated rows are left untouched.
  autofillTranslations: (lang: string): Promise<{ requested: number; filled: number; errors: number }> =>
    request(`/translations/autofill?lang=${encodeURIComponent(lang)}`, {
      method: "POST", deadlineMs: MODEL_MS,
    }).then(jsonOrThrow),

  // Asked for only when the admin presses "copy token" — a secret has no business
  // riding in the settings payload every page fetches.
  zigbeeToken: (): Promise<{ token: string }> =>
    request(`/settings/zigbee-token`).then(jsonOrThrow),
  getSettings: (): Promise<Settings> =>
    request(`/settings`).then(jsonOrThrow),

  updateSettings: (body: { anthropic_api_key?: string; openai_api_key?: string; owntracks_secret?: string; announce_lang?: string; lan_ip?: string; opus_url?: string; opus_token?: string; radio_player?: string; app_url?: string; net_parent?: string; discovery_subnets?: string; managed_vlans?: string; vlan_config?: Record<string, { mac?: string; hostname?: string }> }): Promise<null> =>
    request(`/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  testSettings: (provider: "anthropic" | "openai" | "opus" | "owntracks", api_key?: string, url?: string): Promise<{ ok: boolean; detail: string }> =>
    request(`/settings/test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider, api_key, url }),
    }).then(jsonOrThrow),

  // --- adapter config (UI-editable, secrets encrypted server-side) ---
  listAdapters: (): Promise<AdapterCfg[]> =>
    request(`/adapters`).then(jsonOrThrow),

  adapterRuntime: (): Promise<RuntimeState> =>
    request(`/adapters/runtime`).then(jsonOrThrow),

  setAdapterRuntime: (profile: string, enabled: boolean): Promise<{ ok: boolean; applying?: boolean }> =>
    request(`/adapters/runtime/${profile}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(jsonOrThrow),

  updateAdapterConfig: (adapter: string, values: Record<string, string>): Promise<null> =>
    request(`/adapters/${adapter}/config`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ values }),
    }).then(jsonOrThrow),

  setAdapterName: (adapter: string, label: string): Promise<null> =>
    request(`/adapters/${adapter}/label`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ label }),
    }).then(jsonOrThrow),

  // --- HomeKit pairing (interactive: discover on the LAN, pair by setup code) ---
  homekitDiscovered: (): Promise<{ devices: HomekitDevice[] }> =>
    request(`/adapters/homekit/discovered`).then(jsonOrThrow),

  homekitPair: (device_id: string, pin: string, name: string): Promise<{ ok: boolean; alias: string; name: string }> =>
    request(`/adapters/homekit/pair`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ device_id, pin, name }),
    }).then(jsonOrThrow),

  // Scan the LAN for an adapter's devices (Shelly/Broadlink/ESPHome/Harmony).
  discoverAdapter: (adapter: string): Promise<{ devices: DiscoveredDevice[]; error?: string }> =>
    request(`/adapters/${adapter}/discover`).then(jsonOrThrow),

  // First-run sweep: scan every enabled discoverable adapter at once → devices per
  // adapter, and why each adapter whose scan did not complete failed.
  discoverAll: (): Promise<{ found: Record<string, DiscoveredDevice[]>; failed: Record<string, string>; subnets: string[] }> =>
    request(`/adapters/discover-all`).then(jsonOrThrow),

  // Apply one found device to its adapter's config — server-side append, so many
  // scanned devices accumulate into an encrypted field without clobbering.
  applyDiscovered: (adapter: string, d: DiscoveredDevice): Promise<{ ok: boolean }> =>
    request(`/adapters/${adapter}/discovered`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ set: d.set ?? {}, appendCsv: d.appendCsv ?? {}, appendJson: d.appendJson ?? {} }),
    }).then(jsonOrThrow),

  // ESPHome nodes as cards (secrets masked) — the UI edits the node list here.
  esphomeNodes: (): Promise<{ nodes: EsphomeNode[]; adapter_up: boolean }> =>
    request(`/adapters/esphome/nodes`).then(jsonOrThrow),
  esphomeEditNode: (key: string, body: NodeEdit): Promise<{ ok: boolean; key: string }> =>
    request(`/adapters/esphome/nodes/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  esphomeRemoveNode: (key: string): Promise<{ ok: boolean; removed: number }> =>
    request(`/adapters/esphome/nodes/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // Frigate locations (one adapter, many NVRs) as cards — password masked. Each
  // site's cameras are grouped under it in the UI by descriptor `site`.
  frigateSites: (): Promise<{ sites: FrigateSite[] }> =>
    request(`/adapters/frigate/sites`).then(jsonOrThrow),
  frigateSaveSite: (key: string, body: FrigateSiteEdit): Promise<{ ok: boolean; key: string }> =>
    request(`/adapters/frigate/sites/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  frigateRemoveSite: (key: string): Promise<{ ok: boolean; removed_sites: number; removed_cameras: number }> =>
    request(`/adapters/frigate/sites/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // BABA installs, same card-per-location shape. Removing one drops its cameras —
  // it is the only path that does, since the adapter's roster prune never crosses
  // locations.
  babaSites: (): Promise<{ sites: BabaSite[] }> =>
    request(`/adapters/baba/sites`).then(jsonOrThrow),
  babaSaveSite: (key: string, body: BabaSiteEdit): Promise<{ ok: boolean; key: string }> =>
    request(`/adapters/baba/sites/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  babaRemoveSite: (key: string): Promise<{ ok: boolean; removed_sites: number; removed_cameras: number }> =>
    request(`/adapters/baba/sites/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // Cloudflare tunnel ingress: the locally-managed config.yml is the source of
  // truth; every write is validated + backed up + hot-reloaded adapter-side.
  cloudflareRoutes: (): Promise<{ routes: CloudflareRoute[]; source_path: string; apply: CloudflareApply }> =>
    request(`/adapters/cloudflare/routes`).then(jsonOrThrow),
  cloudflareAddRoute: (body: CloudflareRouteEdit): Promise<{ ok: boolean; hostname: string; applied?: boolean; error?: string; steps?: string[] }> =>
    request(`/adapters/cloudflare/routes`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  cloudflareEditRoute: (hostname: string, body: CloudflareRouteEdit): Promise<{ ok: boolean; hostname: string; applied?: boolean; error?: string; steps?: string[] }> =>
    request(`/adapters/cloudflare/routes/${encodeURIComponent(hostname)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),
  cloudflareRemoveRoute: (hostname: string): Promise<{ ok: boolean; hostname: string; applied?: boolean; error?: string; steps?: string[] }> =>
    request(`/adapters/cloudflare/routes/${encodeURIComponent(hostname)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // Tuya cloud onboarding: pull devices (+ local keys) from the Tuya cloud and
  // locate them on the LAN; add the chosen ones. Keys never reach the browser.
  tuyaCloudDevices: (): Promise<{ devices: TuyaCloudDevice[] }> =>
    request(`/adapters/tuya/cloud-devices`).then(jsonOrThrow),
  tuyaCloudAdd: (ids: string[]): Promise<{ ok: boolean; added: number }> =>
    request(`/adapters/tuya/cloud-add`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids }),
    }).then(jsonOrThrow),
  tuyaRemoveDevice: (key: string): Promise<{ ok: boolean; removed: number }> =>
    request(`/adapters/tuya/devices/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // Zigbee onboarding + mesh diagnostics, driven through the MQTT adapter's
  // zigbee2mqtt bridge — pair/map/rename/remove without the z2m console.
  zigbeePermitJoin: (on: boolean, seconds = 254): Promise<{ status?: string }> =>
    request(`/adapters/zigbee/permit-join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on, seconds }),
    }).then(jsonOrThrow),
  zigbeeMap: (): Promise<ZigbeeMap> =>
    request(`/adapters/zigbee/network-map`).then(jsonOrThrow),
  zigbeeScan: (): Promise<ZigbeeMap> =>
    request(`/adapters/zigbee/network-map`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ routes: false }),
    }).then(jsonOrThrow),
  cloudflareTunnelState: (): Promise<CfTunnelState> =>
    request(`/adapters/cloudflare/tunnel`).then(jsonOrThrow),
  cloudflareTunnelCreate: (name: string): Promise<{ ok?: boolean; tunnel_id?: string }> =>
    request(`/adapters/cloudflare/tunnel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(jsonOrThrow),
  zigbeeAvailability: (): Promise<{ enabled: boolean | null }> =>
    request(`/adapters/zigbee/availability`).then(jsonOrThrow),
  zigbeeSetAvailability: (enabled: boolean): Promise<{ status?: string }> =>
    request(`/adapters/zigbee/availability`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).then(jsonOrThrow),
  zigbeeRename: (from: string, to: string): Promise<{ status?: string }> =>
    request(`/adapters/zigbee/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from, to }),
    }).then(jsonOrThrow),
  zigbeeRemove: (id: string, force: boolean): Promise<{ status?: string }> =>
    request(`/adapters/zigbee/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, force }),
    }).then(jsonOrThrow),

  // SmartThings OAuth: connect the house account (Samsung appliances/AV). login
  // returns the authorize URL to open; the cloud redirects back to /callback.
  smartthingsStatus: (): Promise<{ configured: boolean; connected: boolean }> =>
    request(`/smartthings/status`).then(jsonOrThrow),
  smartthingsLocations: (): Promise<{ locations: { id: string; name: string }[] }> =>
    request(`/smartthings/locations`).then(jsonOrThrow),
  smartthingsLogin: (): Promise<{ url: string }> =>
    request(`/smartthings/login`).then(jsonOrThrow),
  smartthingsDisconnect: (): Promise<{ ok: boolean }> =>
    request(`/smartthings/disconnect`, { method: "POST" }).then(jsonOrThrow),

  // The household address book. DIDA is the house's one reader of it, so the
  // consent screen and the roster both live here — OPUS Library asks DIDA over
  // HTTP rather than holding a second Google grant.
  contactsStatus: (): Promise<{
    configured: boolean; connected: boolean; redirect_uri: string;
    people: number; announced: number;
    last_sync: { at?: string; read: number; added: number; updated: number; removed: number;
                 no_year: { name: string; says: string }[] } | null;
  }> => request(`/contacts/status`).then(jsonOrThrow),
  contactsLogin: (): Promise<{ url: string }> =>
    request(`/contacts/login`).then(jsonOrThrow),
  contactsDisconnect: (): Promise<{ ok: boolean }> =>
    request(`/contacts/disconnect`, { method: "POST" }).then(jsonOrThrow),
  contactsPeople: (): Promise<{ id: number; name: string; born_on: string; contact_id: string;
                                source: string; announce: boolean }[]> =>
    request(`/contacts/people`).then(jsonOrThrow),
  contactsSetAnnounce: (id: number, announce: boolean): Promise<{ ok: boolean }> =>
    request(`/contacts/people/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ announce }),
    }).then(jsonOrThrow),
  // Address-book upkeep: propose, confirm, write back. The only place DIDA writes
  // into somebody else's system, so nothing here happens without a click.
  upkeepSurvey: (): Promise<{
    contacts: number; labels: Record<string, number>; unlabelled: number;
    without_diacritics: number; without_birthday: number; empty: number; duplicates: number;
  }> => request(`/contacts/upkeep/survey`).then(jsonOrThrow),
  upkeepProposals: (): Promise<{
    contacts: number; assistant: boolean; suggested: number;
    people: {
      id: string; name: string; given: string; family: string;
      born: string; born_raw: string; labels: string[]; context: string;
      empty: boolean; extra: string;
      suggest: { given: string; family: string; sure: boolean; why: string } | null;
    }[];
    duplicates: { id: string; who: string; detail: string }[];
  }> => request(`/contacts/upkeep/proposals`, { method: "POST", deadlineMs: MODEL_MS }).then(jsonOrThrow),
  upkeepApply: (
    items: { id: string; given: string; family: string; born: string; target: string; remove: boolean }[],
  ): Promise<{ applied: number; failed: { id: string; error: string }[] }> =>
    request(`/contacts/upkeep/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    }).then(jsonOrThrow),

  contactsImport: (text: string): Promise<{ read: number; added: number; updated: number;
                                            no_year: { name: string; says: string }[] }> =>
    request(`/contacts/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }).then(jsonOrThrow),

  // Android TV over ADB: connect kicks the one-time "Allow debugging?" prompt on
  // the TV; status reports whether it's connected + how many apps were pulled.
  androidtvStatus: (): Promise<{ connected: boolean; host: string; apps: number }> =>
    request(`/adapters/androidtv/status`).then(jsonOrThrow),
  androidtvConnect: (): Promise<{ ok: boolean }> =>
    request(`/adapters/androidtv/connect`, { method: "POST" }).then(jsonOrThrow),

  // Devices: one row per physical device — its auto name + user label.
  listDevices: (): Promise<DeviceMeta[]> =>
    request(`/devices`).then(jsonOrThrow),
  // Rename a device (sets its label). Empty → falls back to the auto name.
  renameDevice: (key: string, name: string | null): Promise<unknown> =>
    request(`/devices/${encodeURIComponent(key)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(jsonOrThrow),

  // Remove a device from any adapter (engine blocks re-adds; entities deleted).
  deleteDevice: (key: string): Promise<void> =>
    request(`/devices/${encodeURIComponent(key)}`, { method: "DELETE" })
      .then(ok).then(() => undefined),

  // The block list behind deleteDevice, and lifting an entry from it. Restoring
  // re-creates nothing: the adapter announces the device again on its next poll.
  removedDevices: (): Promise<RemovedDevice[]> =>
    request(`/devices/removed`).then(jsonOrThrow),

  restoreDevice: (key: string): Promise<void> =>
    request(`/devices/removed/${encodeURIComponent(key)}`, { method: "DELETE" })
      .then(ok).then(() => undefined),

  // Assign a whole device (all its entities) to a room. null → clear.
  setDeviceArea: (key: string, area_id: number | null): Promise<unknown> =>
    request(`/devices/${encodeURIComponent(key)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ area_id }),
    }).then(jsonOrThrow),

  // Set a whole device's canonical type (all its gangs). DIDA's truth from here on.
  setDeviceType: (key: string, device_type: string): Promise<unknown> =>
    request(`/devices/${encodeURIComponent(key)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ device_type }),
    }).then(jsonOrThrow),

  // --- users (admin) ---
  listUsers: (): Promise<User[]> =>
    request(`/users`).then(jsonOrThrow),

  createUser: (username: string, password: string, role: string): Promise<User> =>
    request(`/users`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    }).then(jsonOrThrow),

  updateUser: (
    id: string,
    body: {
      role?: string;
      password?: string;
      allowed_pages?: string[] | null;
      can_control?: boolean;
      control_rules?: ControlRule[];
      view_hides?: ControlRule[];
    },
  ): Promise<null> =>
    request(`/users/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  deleteUser: (id: string): Promise<null> =>
    request(`/users/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // OwnTracks GPS provisioning: GET creates-on-open, POST rotates, DELETE revokes.
  // Self-service location config for the signed-in user — the /onboard page turns
  // inline_url into a one-tap owntracks:// import (no admin hand-off needed).
  myOwntracks: (): Promise<{ inline_url: string; reachable: boolean; encrypted: boolean }> =>
    request(`/me/owntracks`).then(jsonOrThrow),

  // Admin revokes a person's location sharing (nulls their OwnTracks token).
  revokeOwntracks: (id: string): Promise<null> =>
    request(`/users/${id}/owntracks`, { method: "DELETE" }).then(jsonOrThrow),

  // Android companion app: served-APK metadata (404 when no artifact is mounted)
  // and the browser→app handoff link (fresh one-time auto-login URL for self).
  apkMeta: (): Promise<{ versionCode: number; versionName: string }> =>
    request(`/app/apk.json`).then(jsonOrThrow),
  // Car apps (DIDA Auto / DIDA Music): artifact metadata gates the account-page
  // download card the same way apkMeta gates /onboard.
  carAppMeta: (app: "auto" | "music"): Promise<{ versionCode: number; versionName: string }> =>
    request(`/app/${app}.json`).then(jsonOrThrow),
  myAppLink: (): Promise<{ url: string }> =>
    request(`/me/app-link`, { method: "POST" }).then(jsonOrThrow),

  // Phone setup: GET creates-on-open the onboarding QR, POST rotates, DELETE revokes.
  accessSetup: (id: string): Promise<AccessSetup> =>
    request(`/users/${id}/access`).then(jsonOrThrow),

  rotateAccess: (id: string): Promise<AccessSetup> =>
    request(`/users/${id}/access`, { method: "POST" }).then(jsonOrThrow),

  revokeAccess: (id: string): Promise<null> =>
    request(`/users/${id}/access`, { method: "DELETE" }).then(jsonOrThrow),

  // --- virtual entities / helpers (admin) ---
  listVirtual: (): Promise<VirtualEntity[]> =>
    request(`/virtual`).then(jsonOrThrow),

  createVirtual: (name: string, capability: string, options: string[] | null,
                  category = "control", range?: { min: number; max: number; step: number }): Promise<VirtualEntity> =>
    request(`/virtual`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, capability, options, category, ...(range ?? {}) }),
    }).then(jsonOrThrow),

  deleteVirtual: (entityId: string): Promise<null> =>
    request(`/virtual/${encodeURIComponent(entityId)}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- computed helpers (pure derivation layer; admin CRUD) ---
  listComputed: (): Promise<ComputedHelper[]> =>
    request(`/computed-helpers`).then(jsonOrThrow),

  createComputed: (name: string, capability: string, definition: HelperDef): Promise<ComputedHelper> =>
    request(`/computed-helpers`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, capability, definition }),
    }).then(jsonOrThrow),

  updateComputed: (id: number, name: string, capability: string, definition: HelperDef, enabled: boolean): Promise<ComputedHelper> =>
    request(`/computed-helpers/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, capability, definition, enabled }),
    }).then(jsonOrThrow),

  deleteComputed: (id: number): Promise<null> =>
    request(`/computed-helpers/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- schedules / "beats" (admin) ---
  listSchedules: (): Promise<Schedule[]> =>
    request(`/schedules`).then(jsonOrThrow),

  previewSchedules: (days: string[], signal?: AbortSignal): Promise<{ id: number; days: string[] }[]> =>
    request("/schedules/preview", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ days }), signal }).then(jsonOrThrow),

  createSchedule: (name: string, kind: string, params: Record<string, unknown>): Promise<Schedule> =>
    request(`/schedules`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, kind, params }),
    }).then(jsonOrThrow),

  patchSchedule: (id: number, patch: { enabled?: boolean; params?: Record<string, unknown>; name?: string }): Promise<Schedule> =>
    request(`/schedules/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then(jsonOrThrow),

  deleteSchedule: (id: number): Promise<null> =>
    request(`/schedules/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- backup / restore (admin) ---
  // Returns the raw Response so the caller can stream the .dump to a download.
  backup: (): Promise<Response> =>
    request(`/system/backup`, { deadlineMs: TRANSFER_MS }).then(ok),
  // Restore posts the archive as the raw request body (no multipart).
  restore: (file: File): Promise<{ ok: boolean; bytes: number; message: string }> =>
    request(`/system/restore`, {
      method: "POST", deadlineMs: TRANSFER_MS,
      headers: { "content-type": "application/octet-stream" },
      body: file,
    }).then(jsonOrThrow),

  // Live system health for the System page (engine throughput, adapter badges, DB).
  systemStats: (): Promise<SystemStats> =>
    request(`/system/stats`).then(jsonOrThrow),

  // Rules pointing at entities that no longer exist. Nothing enforces these
  // references, so a rename leaves them dangling and silently dead.
  systemOrphans: (): Promise<OrphansData> =>
    request(`/system/orphans`).then(jsonOrThrow),

  // System-health alerts: currently-firing (live evaluator) + recent history (CH).
  systemAlerts: (): Promise<AlertsData> =>
    request(`/system/alerts`).then(jsonOrThrow),
  alertRules: (): Promise<AlertRule[]> =>
    request(`/system/alert-rules`).then(jsonOrThrow),
  updateAlertRule: (
    key: string,
    patch: { threshold?: number | null; hold_s?: number; enabled?: boolean },
  ): Promise<void> =>
    request(`/system/alert-rules/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }).then(ok).then(() => undefined),

  silenceAlert: (key: string, scope: string, hours: number | null): Promise<void> =>
    request(`/system/alerts/silence`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(hours === null ? { key, scope } : { key, scope, hours }),
    }).then(ok).then(() => undefined),
  unsilenceAlert: (key: string, scope: string): Promise<void> =>
    request(`/system/alerts/silence?${new URLSearchParams({ key, scope })}`, {
      method: "DELETE",
    }).then(ok).then(() => undefined),
  alertRecipients: (): Promise<AlertRecipients> =>
    request(`/system/alert-recipients`).then(jsonOrThrow),
  setAlertRecipients: (recipients: string[]): Promise<void> =>
    request(`/system/alert-recipients`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ recipients }),
    }).then(ok).then(() => undefined),

  // ClickHouse history backup (separate from the config backup — bulk time-series).
  historyInfo: (): Promise<{ rows: number; bytes: number; recovery_required: boolean }> =>
    request(`/system/history/info`).then(jsonOrThrow),
  backupHistory: (): Promise<Response> =>
    request(`/system/backup/history`, { deadlineMs: TRANSFER_MS }).then(ok),
  restoreHistory: (file: File): Promise<{ ok: boolean; bytes: number; message: string }> =>
    request(`/system/restore/history`, {
      method: "POST", deadlineMs: TRANSFER_MS,
      headers: { "content-type": "application/octet-stream" },
      body: file,
    }).then(jsonOrThrow),
  recoverHistory: (): Promise<{ ok: boolean }> =>
    request(`/system/restore/history/recover`, { method: "POST", deadlineMs: TRANSFER_MS }).then(jsonOrThrow),

  // --- scenes (named state snapshots; create/delete admin, recall permission-checked) ---
  scenes: (): Promise<Scene[]> =>
    request(`/scenes`).then(jsonOrThrow),
  scene: (id: number): Promise<SceneDetail> =>
    request(`/scenes/${id}`).then(jsonOrThrow),
  updateScene: (id: number, name: string, states: SceneState[]): Promise<Scene> =>
    request(`/scenes/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, states }),
    }).then(jsonOrThrow),
  // entityIds empty = capture every control entity with settable state.
  createScene: (name: string, entityIds: string[] = []): Promise<Scene> =>
    request(`/scenes`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, entity_ids: entityIds }),
    }).then(jsonOrThrow),
  recallScene: (id: number): Promise<{ applied: number; skipped: number }> =>
    request(`/scenes/${id}/recall`, { method: "POST" }).then(jsonOrThrow),
  deleteScene: (id: number): Promise<null> =>
    request(`/scenes/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  // Scheduled (automatic) backups — two independent jobs (config + history).
  getSchedule: (): Promise<BackupSchedule> =>
    request(`/system/backup/schedule`).then(jsonOrThrow),
  putSchedule: (s: {
    config: Omit<BackupJob, "next_run">;
    history: Omit<BackupJob, "next_run">;
  }): Promise<BackupSchedule> =>
    request(`/system/backup/schedule`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(s),
    }).then(jsonOrThrow),
  listBackups: (): Promise<BackupFile[]> =>
    request(`/system/backup/list`).then(jsonOrThrow),
  runBackup: (): Promise<{ created: string[] }> =>
    request(`/system/backup/run`, { method: "POST", deadlineMs: TRANSFER_MS }).then(jsonOrThrow),
  backupFile: (name: string): Promise<Response> =>
    request(`/system/backup/file/${encodeURIComponent(name)}`, { deadlineMs: TRANSFER_MS }).then(ok),
  deleteBackupFile: (name: string): Promise<null> =>
    request(`/system/backup/file/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),
};

export interface Scene {
  id: number;
  name: string;
  count: number;
  created_at: string;
}

/** One captured value: recall re-issues the command that sets it back. */
export interface SceneState {
  entity_id: string;
  capability: string;
  value: boolean | number | string;
}

export interface SceneDetail {
  id: number;
  name: string;
  created_at: string;
  states: SceneState[];
}

export interface BackupJob {
  enabled: boolean;
  freq: "daily" | "weekly";
  time: string;
  weekday: number;
  keep: number;
  next_run?: string | null;
}

export interface BackupSchedule {
  config: BackupJob;
  history: BackupJob;
}

export interface BackupFile {
  name: string;
  kind: string;
  bytes: number;
  mtime: string;
}

export type ScheduleKind = "calendar" | "solar" | "lunar" | "season";

export interface Schedule {
  id: number;
  name: string;
  kind: ScheduleKind;
  params: Record<string, unknown>;
  enabled: boolean;
  created_at?: string;
}

export interface VirtualEntity {
  entity_id: string;
  name: string;
  capability: string;
  // enum: the choices; number: the slider range {min,max,step}; else null.
  options: string[] | { min: number; max: number; step: number } | null;
  category?: string; // "control" | "config" — config helpers hide from device views
  created_at?: string;
}

export interface HelperBranch { conditions: Condition[]; value: Scalar }
export interface HelperDef {
  branches?: HelperBranch[]; // typed: first branch whose conditions hold wins
  default?: Scalar;          // value when no branch matches
  script?: string;           // advanced: Starlark `value = …` (overrides branches)
}
export interface ComputedHelper {
  id: number;
  entity_id: string; // helper:<slug>
  name: string;
  capability: string; // value type (text/boolean/number/enum/time/…)
  definition: HelperDef;
  enabled: boolean;
  last_error: string | null;
}

export interface User {
  id: string;
  username: string;
  role: string;
  last_login_at: string | null;
  /** null = full consumer access; otherwise only these page keys are visible. */
  allowed_pages: string[] | null;
  /** false = view-only; scoped exceptions in control_rules flip per-scope. */
  can_control: boolean;
  control_rules: ControlRule[];
  /** entities/areas/capabilities hidden from this user entirely (view boundary). */
  view_hides: ControlRule[];
}

export interface AccessSetup {
  username: string;
  /** `{public}/api/auth/link?k=<token>` — the LOGIN QR: straight into the web,
   * signed in (guests, iPhone). Reusable until rotated. */
  url: string;
  /** Same link pinned to /onboard — the SETUP QR (Android app): one guided
   * button that installs the app or opens it signed in. */
  setup_url: string;
  public_url: string;
  /** false ⇒ DIDA_PUBLIC_URL is loopback; a phone off-LAN can't open it. */
  reachable: boolean;
  /** inline SVG of the login QR. */
  qr_svg: string;
  /** inline SVG of the setup QR. */
  qr_svg_setup: string;
}

export interface LanInterface {
  iface: string;
  address: string;
  mac: string;
  lan: boolean;
  default_route: boolean;
}

export interface LanStatus {
  address?: string;
  hostname?: string;
  interfaces?: LanInterface[];
}

export interface Settings {
  anthropic_configured: boolean;
  anthropic_hint: string | null;
  openai_configured: boolean;
  openai_hint: string | null;
  owntracks_configured: boolean; // payload-encryption key for the OwnTracks receiver
  announce_lang: string; // TTS default language code
  // Host values — the DB is the source, .env only what install.sh seeded.
  lan_ip: string; // THIS host's address on the LAN the AV devices are on
  opus_url: string; // where OPUS · Player answers, as the devices reach it
  opus_configured: boolean; // url + token both set (the media page has a shelf)
  radio_player: string; // the player radio:tuner plays the OPUS stations on ('' = none)
  zigbee_console_url: string; // where the Zigbee2MQTT console answers ('' when unknown)
  app_url: string; // the origin people open DIDA on (the tunnel)
  net_parent: string; // host NIC the VLAN sub-links hang off
  discovery_subnets: string; // CSV of IoT subnets to scan for devices
  lan_status: LanStatus; // this host's own interfaces (lanprobe)
  managed_vlans: string; // CSV of VLAN ids DIDA brings up a sub-interface for
  vlan_status: Record<string, { iface: string; address: string; mac?: string }>; // netmgr-obtained
  vlan_config: Record<string, { mac?: string; hostname?: string }>; // user MAC/hostname overrides
}

export interface PanelConfig {
  idle_s: number;          // no touch for this long → screensaver
  photo_interval_s: number; // screensaver: seconds per photo
  transition: "kenburns" | "fade" | "none"; // photo transition style
  photo_fit: "blur" | "contain" | "cover"; // how a photo fills the screen
  presence_entity: string; // sleep the display when this entity is empty ("" → always on)
  room_area_id?: number | null; // read-only: the panel's own area (scopes the presence picker)
}

export interface SlideshowPhoto {
  id: string;
  taken_at: string | null; // ISO datetime the photo was taken (EXIF), or null
  place: string | null;    // where it was taken, or null
  country: string | null;  // ISO 3166 code of that place, or null
}

export interface AdapterField {
  key: string;
  label: string;
  type: string; // text | number | password | bool | select | entity
  secret: boolean;
  placeholder: string;
  help: string;
  options: string[];
  entity_cap: string; // for type "entity": restrict the picker to this capability ("" = any)
  group: string;      // optional section heading; contiguous same-group fields render under one subheading
  value: string;      // plain value ("" for secrets)
  configured: boolean;
}
export interface AdapterStatus {
  state: "idle" | "connecting" | "ok" | "error";
  detail: string;
  since: number;
}
export interface AdapterCfg {
  adapter: string;
  fields: AdapterField[];
  discoverable: boolean;
  category: "discover" | "auto" | "account";
  status: AdapterStatus | null; // live connection status (fail-loud badge)
  label: string;                // human-friendly display name (empty → use the name)
}
// What this installation actually runs. One profile per adapter, plus the few
// services that are switched on the same way (the zigbee bridge, the ESPHome
// build dashboard, the Matter bridge).
export interface RuntimeProfile {
  services: string[];
  enabled: boolean;
  running: number;
  total: number;
}
export interface RuntimeState {
  profiles: Record<string, RuntimeProfile>;
  applying: string | null;   // a profile is being started/stopped right now
  error: string | null;      // last apply failure, if any
}
export interface DiscoveredDevice {
  label: string;
  set?: Record<string, string>;
  appendCsv?: Record<string, string>;
  appendJson?: Record<string, Record<string, unknown>>;
}
export interface EsphomeNode {
  name: string | null;
  host: string;
  key: string;        // device_key the adapter uses (slug of name-or-host)
  has_psk: boolean;
  has_password: boolean;
  // Live connection status from the adapter (null if the adapter isn't answering).
  state?: "connecting" | "online" | "offline" | "error" | null;
  code?: "encryption" | "unresolved" | "unreachable" | "error" | null; // error reason code
  reason?: string | null;   // raw adapter message (shown on hover)
  entities?: number | null; // entity count when online
}
export interface NodeEdit {
  name?: string;
  host?: string;
  noise_psk?: string;
  password?: string;
}
export interface CfTunnelState {
  mode: "source" | "tunnel" | "none";
  zone: string;
  has_token: boolean;
  tunnel_id: string | null;
}
export interface MusicSource {
  type: "local" | "nfs";
  server: string;
  export: string;
  applying: string | null;
  error: string | null;
}
export interface ZigbeeNode {
  ieee: string;
  name: string;
  type: string;          // coordinator / router / enddevice
  slug: string | null;   // our device slug when the ieee is one we know
  lastSeen?: number | null;
}
export interface ZigbeeLink {
  source: string;        // ieee
  target: string;        // ieee
  lqi: number | null;    // link quality, the number a person reads off a mesh
  relationship?: number | null;
  depth?: number | null;
}
export interface ZigbeeMap {
  scanning: boolean;
  ts_ns: number;
  error: string;
  map: { nodes: ZigbeeNode[]; links: ZigbeeLink[] } | null;
}
export interface FrigateSite {
  key: string;          // slug of the location name (stable id for edit/remove)
  name: string;
  url: string;
  user: string;
  go2rtc: string;
  has_password: boolean;
}
export interface FrigateSiteEdit {
  name: string;
  url: string;
  user: string;
  password?: string;    // blank on edit keeps the stored one
  go2rtc: string;
}
export interface BabaSite {
  key: string;          // slug of the location name (stable id for edit/remove)
  name: string;
  nats_url: string;     // the state plane — a location cannot exist without it
  nats_user: string;
  go2rtc: string;       // media plane
  go2rtc_user: string;
  api_url: string;      // archive plane (events + clips); blank = live view only
  has_nats_password: boolean;
  has_go2rtc_password: boolean;
  has_peer_key: boolean;
}
export interface BabaSiteEdit {
  name: string;
  nats_url: string;
  nats_user: string;
  nats_password?: string;    // blank on edit keeps the stored one
  go2rtc: string;
  go2rtc_user: string;
  go2rtc_password?: string;  // blank on edit keeps the stored one
  api_url: string;
  peer_key?: string;
}
export type CloudflareServe = "both" | "lan" | "wan";
export interface CloudflareRoute {
  id: string;
  hostname: string;
  service: string;
  serve: CloudflareServe;
  opts: string[];
  section: string | null;   // Homepage section; null = no tile
  icon: string | null;
}
export interface CloudflareRouteEdit {
  hostname: string;
  service: string;
  serve: CloudflareServe;
  opts: string[];
  section: string | null;
  icon: string | null;
}
// What the host applier reported after the last run of apply-ingress.sh.
export interface CloudflareApply {
  ok: boolean | null;
  error: string | null;
  steps: string[];
  ts: number | null;
}
export interface TuyaCloudDevice {
  id: string;
  name: string;
  ip: string | null;      // located on the LAN (null = not found → not addable)
  version: number;
  online: boolean;
  caps: string[];         // DIDA capabilities auto-mapped from the device's DPs
  unmapped: string[];     // Tuya DP codes DIDA can't map yet (device still adds)
  has_key: boolean;
  already: boolean;       // already in the tuya config
  cloud?: boolean;        // Zigbee sub-device read via the cloud (no LAN ip)
}
export interface DeviceMeta {
  device_key: string;
  adapter: string;
  name: string | null;   // adapter-detected friendly name
  label: string | null;  // user override
  site: string | null;   // the installation it stands at when not this house (Cabin, Seaside)
}
export interface RemovedDevice {
  key: string;
  adapter: string | null;
  removed_at: string;
}
export interface HomekitDevice {
  device_id: string;
  name: string;
  model: string;
  address: string;
  pairable: boolean;
}

export interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}
export interface AssistantReply {
  reply: string;
  actions: { type: string; [k: string]: unknown }[];
}

// A numeric history point (bucketed): avg + min/max band + last-in-bucket.
export interface HistoryNumPoint {
  ts: number; // epoch ms
  v: number;
  min: number;
  max: number;
  last: number;
}
// A non-numeric (enum/text) change point.
export interface HistoryStrPoint {
  ts: number;
  s: string;
}
// One device, fully described (GET /entities/{id}/detail).
export interface EntityDetail extends Entity {
  // fp_style (the icon the user picked for the plan marker — the panel must show
  // the SAME one, or it reads as a different device) comes with Entity.
  state: {
    capability: string; value: Scalar | null; unit: string | null;
    updated_at: string; changed_at: string | null; age_s: number;
  }[];
  siblings: {
    entity_id: string; name: string | null; label: string | null; category: string; diagnostic: boolean;
    // A device is usually several entities — the panel shows the whole group's
    // values, not just the one that happened to be clicked.
    state: { capability: string; value: Scalar | null; unit: string | null; updated_at: string; age_s: number }[];
  }[];
  automations: { id: number; name: string; enabled: boolean; last_triggered_at: string | null }[];
  // What happened TO it — the journal (adapter online/offline, a rejected reading,
  // the rule that fired). Visible to everyone: these name services and rules, not
  // people. Scoped to the whole physical device, siblings included.
  events: {
    ms: number; entity_id: string; source: string; kind: string;
    severity: "debug" | "info" | "notice" | "warning" | "error"; message: string; data: string;
  }[];
  // Absent entirely for a non-admin — `source` names who acted.
  commands?: { ms: number; capability: string; command: string; source: string; args: string }[];
}

export interface HistorySeries {
  capability: string;
  numeric: boolean;
  bucket_seconds: number;
  points: (HistoryNumPoint | HistoryStrPoint)[];
}
export interface HistoryResponse {
  entity_id: string;
  hours: number;
  series: HistorySeries[];
}

// The last value a capability carried. `until` is null while it still carries it
// (the caller is then looking at live state, not a memory); both are epoch ms.
export interface LastNonEmpty {
  value: string | null;
  ts: number | null;
  until: number | null;
}

// Cumulative house-energy balance (History → Energija). Per-day kWh; `consumption`
// and `self_consumed` are null on days before the grid meter existed.
export interface EnergyDay {
  date: string;
  import: number;
  export: number;
  production: number;
  self_consumed: number | null;
  consumption: number | null;
}
export interface EnergyTotals {
  import: number;
  export: number;
  production: number;
  consumption: number;
  self_consumed: number;
  self_sufficiency: number | null;
  balance_from?: string | null;
  metered_days?: number;
}
export interface EnergyHour {
  ts: number;
  import: number;
  export: number;
  production: number;
  self_consumed: number | null;
  consumption: number | null;
}
export interface EnergyHourly {
  hours: EnergyHour[];
  totals: EnergyTotals;
  submeters: { device: string; kwh: number }[];
}
export interface EnergyResponse {
  days: EnergyDay[];
  totals: EnergyTotals;
  submeters: { device: string; kwh: number }[];
  meters: { import: string[]; export: string[]; solar: string[]; submeters: string[] };
}
export interface EnergyMeter {
  entity_id: string;
  suggested: string;
  role: string;
  exposed: boolean;
}
// One command-audit row (GET /history/commands, admin): who told which entity what.
export interface CommandLogEntry {
  ts: number;          // unix ms
  entity_id: string;
  capability: string;
  command: string;
  source: string;      // "user:alex" | "automation:78:LIGHT-LivingRoom" | "matter" | …
  args: string;        // JSON-encoded args ("" when none)
}

export interface EnergyConfig {
  meters: EnergyMeter[];
  roles: string[];
}

// Retention matrix (Settings → Retencija). days = 0 means "don't keep that tier".
export interface RetentionClass {
  name: string;
  label: string;
  days_raw: number;
  days_1h: number;
  days_1d: number;
  sort_order: number;
}
export interface RetentionOverride {
  entity_id: string;
  capability: string;
  class_name: string;
}
export interface RetentionPolicy {
  classes: RetentionClass[];
  capabilities: Record<string, string>; // capability -> class name
  overrides: RetentionOverride[];
  known_capabilities: string[];
}

// Audio signal-path: the chain the now-playing audio passes through. Neutral
// data — the UI maps kind/source/mode to labels + icons.
export interface OpusStatus {
  configured: boolean;
  ok: boolean;
  detail: string;
}
export interface OpusArtist {
  id: number;
  name: string;
  image: string | null;
  held: number;
  begin_year: number | null;
  country: string | null;
}
export interface OpusReleaseCard {
  id: number;
  title: string;
  year: string;
  cover: string | null;
  tracks: number;
}
export interface OpusArtistDetail {
  id: number;
  name: string;
  image: string | null;
  releases: OpusReleaseCard[];
  bio: string;
  country: string | null;
  begin_year: number | null;
  end_year: number | null;
}
export interface OpusTrack {
  id: number;
  position: number;
  title: string;
  artist: string;
  album: string;
  release_id: number;
  cover_url: string | null;
  duration_s: number | null;
  codec: string | null;
  channels: number | null;
}
export interface OpusRelease {
  id: number;
  title: string;
  artist: string;
  artist_id: number;
  cover_url: string | null;
  release_date: string | null;
  tracks: OpusTrack[];
}
export interface OpusTvLink {
  key: string;
  path: string;
  items: boolean;
}
export interface OpusTvItem {
  kind: string;
  id: number;
  title: string;
  subtitle: string | number | null;
}
export interface OpusStation {
  id: number;
  name: string;
  genre: string;
  url: string;
  logo: string | null;
}

export interface PipeNode {
  kind: "source" | "transform" | "transport" | "renderer" | "dac";
  measured?: boolean;
  format?: string | null;
  source?: string; // source node: library | radio | external | unknown
  mode?: string; // transform node: remux-copy | direct
  bitperfect?: boolean;
  via?: string; // transport node: http (LAN, from medialib) | internet (renderer pulls the station itself)
  from?: string | null;
  to?: string | null;
  name?: string; // renderer / dac node
  proto?: string;
  bitrate?: string | null;
  channels?: number | null; // renderer node: channel count (streams report no rate)
  rate_class?: string | null; // renderer/dac node: the DAC LED's colour tier (r48 | hires | dsd | dsd256)
  match?: boolean | null; // renderer output format == source format (no resample/truncate)
  link?: string | null; // dac node: digital link (USB / SPDIF)
}
export interface RendererOutput {
  volume: number | null;
  mixer: string | null;
  volume_control_disabled: boolean | null;
  mute: boolean | null;
  replay_gain?: string | null;   // MPD only: its replay gain mode
  crossfade?: number | null;     // MPD only: seconds of crossfade, 0 = off
}
export interface Pipeline {
  health?: {
    state: string;
    reason?: string | null;
    attempts?: number;
    checked_at?: number;
    output?: { state: string; card: string; hw_ptr: number | null } | null;
    speaker_verified: boolean;
  } | null;
  entity_id: string;
  name: string;
  playing: boolean;
  source: string | null;
  quality: string | null;
  format_match: boolean | null; // source format preserved at the renderer
  exclusive: boolean; // renderer in bit-perfect/Exclusive mode
  output: RendererOutput | null; // live volume/mixer state from the renderer
  verified: boolean; // format_match AND exclusive → bit-perfect
  nodes: PipeNode[];
}
/** Live engine-events WebSocket. One message per accepted state update. */
export function stateWs(): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/api/ws`);
}


/** Where the robot's map lies on the plan: a box in plan percent, turned, maybe
 *  mirrored. Dragging the picture into place is the whole calibration. */
export interface VacuumPlacement {
  adapter?: string;
  map_id?: string;      // which of the robot's maps, by the name it carries
  floor?: string;       // and which storey of ours it lies on
  x: number;
  y: number;
  w: number;
  rotation: number;
  mirrored: boolean;
  opacity?: number;
}

/** The robot's map: a PNG of what it has driven through, plus what a pixel is worth. */
/** One saved map: a storey the robot knows, drawn. */
export interface VacuumMapEntry {
  map_id: string;
  map_name: string;
  png: string;
  grid_mm: number;
  width: number;
  height: number;
  origin: [number, number];
  charger: [number, number];
  rooms: { id: number; name: string; m2: number; centre: [number, number] }[];
  placement: VacuumPlacement | null;
}

export interface VacuumMap {
  ok: boolean;
  current: string;                 // the map the robot is standing on
  maps: VacuumMapEntry[];
  map_id: string;
  map_name: string;
  png: string;                 // base64
  grid_mm: number;
  width: number;
  height: number;
  origin: [number, number];
  charger: [number, number];
  rooms: { id: number; name: string; m2: number; centre: [number, number] }[];
  placement: VacuumPlacement | null;
  placements: Record<string, VacuumPlacement>;
}


/** The robot while it works, in its own millimetres. */
export interface VacuumLive {
  ok: boolean;
  robot: [number, number] | null;
  heading: number;
  charger: [number, number] | null;
  areas: number[][];
  track: [number, number][];
  at_ms: number;
}
