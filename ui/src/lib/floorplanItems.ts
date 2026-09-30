// Floor-plan item derivation — the single source of truth for how entities
// become plan markers, room labels and light glows. Shared by the interactive
// /floorplan page (view + edit) and the read-only wall-panel plan (/panel), so
// a marker can never exist on one surface and be missing on the other.

import { devices, type Device } from "$lib/store.svelte";
import { derivedKey } from "$lib/devices";
import { capMeta, entityType, PLAN_HIDDEN_READINGS, type DeviceType } from "$lib/capabilities";
import { hasRoomReadings, roomAggregate, roomEligible, type RoomCap } from "$lib/roomSensors";
import { pointInPolygon, polygonCentroid, toPoints, type GlowSpec } from "$lib/floorGeometry";
import type { Area, FloorPoly } from "$lib/api";

const NON_PHYSICAL = new Set(["astro", "announce", "calendar", "presence"]);
// `mower` is an actuator (start/pause/dock commands): without it here the mower
// node fell into the sensor branch and the card took the intensity helper's
// name and a waveform icon.
const PRIMARY = ["on_off", "open_close", "lock", "hvac_mode", "fan_speed", "media_transport", "mower", "vacuum"];
export const DEFAULT_R = 9; // default glow radius (% of plan) when no shape is set

export interface PlaceItem {
  key: string; members: Device[]; name: string; areaId: number | null;
  type: DeviceType; icon: string; scale: number; tint: string | null; styleId: string;
  toggle: Device | null; hasExtra: boolean;
  // A sensor's motion half is placed in its OWN slot (fpMotion*), apart from its
  // value label (fp*); `labelShow` scopes the FloorSensorLabel to one half.
  slot?: "main" | "motion";
  labelShow?: "readings" | "motion" | "both";
}

const nodeKey = (d: Device): string => d.deviceKey || derivedKey(d.entityId);
// A capability the user HID in the adapter (hidden_caps) is treated as absent
// everywhere on the plan — consistent with the adapter's expose toggles.
const shown = (d: Device, c: string): boolean => c in d.caps && !d.hiddenCaps.includes(c);
const isPrimary = (d: Device): boolean => PRIMARY.some((c) => shown(d, c));
const NOEXTRA = new Set(["on_off", "enum_options", "effect_options", "number_options", "hvac_mode_options", "fan_mode_options", "vacuum_mode_options", "source", "source_options"]);
// Settable helper caps: a grouped non-actuator entity carrying one of these (e.g. a
// mower-intensity number helper sharing the mower's device_key) rides on the actuator's
// card. Pure sensors (temp/lux/motion) are excluded so they don't clutter it.
const SETTABLE = new Set(["number", "enum", "time", "boolean"]);
const hasExtra = (ds: Device[]): boolean => ds.some((d) =>
  // A write-only mode key (a laser's Auto/Music press) carries no state, so it's absent
  // from `caps` — count declared `press` capabilities too, or long-press finds "no extra"
  // and opens history instead of the mode popover.
  d.capabilities.includes("press") || Object.keys(d.caps).some((c) => !NOEXTRA.has(c)));

/** Every entity eligible to appear on the plan at all. */
export function planCandidates(): Device[] {
  // A write-only device (Broadlink press buttons: laser / projector / screen)
  // has capabilities but NO state, so `caps` is empty — key off the registry
  // capability set too, or such devices silently vanish from the plan.
  return devices.list.filter((d) => d.exposed && !d.diagnostic && !NON_PHYSICAL.has(d.adapter)
    && (d.capabilities.length > 0 || Object.keys(d.caps).length > 0));
}

// A scene region — BABA's operator-drawn place (a parking spot, a gate) — carries
// its own state, so it earns its own marker instead of dissolving into the camera
// node's sensor pile, exactly like a named zone does.
const isScene = (d: Device): boolean => d.entityId.includes(":scene:") && shown(d, "scene_state");
/** Group candidate entities into plan items (markers / labels / motion dots). */
export function buildPlanItems(candidates: Device[]): PlaceItem[] {
  const byNode = new Map<string, Device[]>();
  const scenes: Device[] = [];
  for (const d of candidates) {
    if (isScene(d)) { scenes.push(d); continue; }
    const arr = byNode.get(nodeKey(d));
    if (arr) arr.push(d); else byNode.set(nodeKey(d), [d]);
  }
  const out: PlaceItem[] = [];
  // Scene markers, keyed by the PLACE the region watches — its name. BABA names a
  // place across cameras (its `parked` map is keyed by that name), so the two
  // regions that both watch P1 are one spot on the plan and place as one marker:
  // both entities carry the position, so either camera's view keeps it live.
  const byPlace = new Map<string, Device[]>();
  for (const s of [...scenes].sort((a, b) => a.entityId.localeCompare(b.entityId))) {
    const arr = byPlace.get(s.name);
    if (arr) arr.push(s); else byPlace.set(s.name, [s]);
  }
  for (const [place, ents] of byPlace) {
    const rep = ents[0];
    out.push({
      key: `scene:${place}`, members: ents, name: place,
      areaId: ents.find((e) => e.areaId != null)?.areaId ?? null,
      // No glyph by default: the place's name IS the marker, and a car drawn beside
      // "P1" only said the same thing twice. The editor's icon picker still applies.
      type: "sensor", icon: rep.fpStyle?.icon ?? "", scale: rep.fpStyle?.scale ?? 1,
      tint: rep.fpStyle?.color ?? null, styleId: rep.entityId, toggle: null, hasExtra: true, slot: "main",
    });
  }
  for (const [nk, ents] of byNode) {
    // "Now playing" media sensor (fp_style.effect="media"): a placeable enum marker whose
    // icon + tint follow its value — a green music note for audio, a blue screen for video,
    // a neutral media glyph when idle. One per room, dragged wherever the user wants.
    const mediaEnt = ents.find((e) => e.fpStyle?.effect === "media");
    if (mediaEnt) {
      const val = mediaEnt.caps["enum"]?.value;
      const icon = mediaEnt.fpStyle?.icon
        ?? (val === "audio" ? "music" : val === "video" ? "video" : "media");
      out.push({
        key: nk, members: [mediaEnt], name: devices.deviceLabel(nk) ?? mediaEnt.name,
        areaId: mediaEnt.areaId, type: "sensor", icon, scale: mediaEnt.fpStyle?.scale ?? 1,
        tint: val === "audio" ? "#34d399" : val === "video" ? "#38bdf8" : null,
        styleId: mediaEnt.entityId, toggle: null, hasExtra: false, slot: "main",
      });
      continue;
    }
    const acts = ents.filter(isPrimary);
    if (acts.length) {
      // A single-actuator node folds in its grouped virtual HELPERS (a settable
      // virtual:… entity deliberately grouped under this device_key — e.g. the mower's
      // intensity) so they share the actuator's card. Scoped to virtual: on purpose:
      // a device's own auto-grouped config sub-entities must not clutter the card.
      // Multi-actuator nodes keep per-actuator markers (helper ownership is ambiguous).
      const extras = acts.length === 1
        ? ents.filter((e) => !isPrimary(e) && (
            (e.entityId.startsWith("virtual:") && Object.keys(e.caps).some((c) => SETTABLE.has(c)))
            // Sibling mode keys (a laser's Music/Auto press buttons) ride the actuator's
            // card — the "On" actuator carries power, the modes live in its popover.
            || e.capabilities.includes("press")))
        : [];
      for (const a of acts) {
        const mem = [a, ...extras];
        const ty = entityType(a.deviceType, a.caps);
        const auto = ty === "light" && "color_rgb" in a.caps ? "strip" : ty;
        out.push({
          // A single-actuator device shows its (renameable) device label; a
          // multi-actuator node keeps per-entity names so its markers stay distinct.
          key: a.entityId, members: mem,
          name: (acts.length === 1 ? devices.deviceLabel(nk) : null) ?? a.name,
          areaId: a.areaId, type: ty,
          icon: a.fpStyle?.icon ?? auto, scale: a.fpStyle?.scale ?? 1, tint: a.fpStyle?.color ?? null, styleId: a.entityId,
          toggle: shown(a, "on_off") ? a : null, hasExtra: hasExtra(mem),
        });
      }
    } else if (ents.some((e) => e.capabilities.includes("source"))) {
      // Universal remote (Harmony): one marker → a tap opens the D-pad popover.
      // Its `press` key siblings are stateless (excluded from candidates), so pull
      // the whole device group for the popover's Remote widget.
      const head = ents.find((e) => e.capabilities.includes("source"))!;
      const group = devices.membersOf(head.deviceKey ?? head.entityId);
      out.push({
        key: nk, members: group.length ? group : ents,
        name: devices.deviceLabel(nk) ?? head.name,
        areaId: head.areaId ?? ents.find((e) => e.areaId != null)?.areaId ?? null,
        type: "remote", icon: head.fpStyle?.icon ?? "remote", scale: head.fpStyle?.scale ?? 1, tint: head.fpStyle?.color ?? null,
        styleId: head.entityId, toggle: null, hasExtra: true, slot: "main",
      });
    } else {
      // Sensor node → markers. NUMERIC readings (temp/lux/…) are NOT markers here —
      // they roll up into the ROOM label (mean per area; see buildRoomLabels). What stays:
      //  • presence/motion → its own marker: enumerated zones each, else one
      //    aggregate dot. Invisible until it fires; own placement slot.
      //  • a NON-presence, NON-numeric sensor (contact, mower, enum state) → an icon
      //    marker, so it doesn't vanish with the value labels.
      // A zone is placeable on its own: NAMED zones (BABA/Frigate `…:zone:<name>`,
      // grouped under the camera device) always, NUMERIC zones (Aqara FP2 `…:2`) only
      // when ≥2 — a lone FP2 "zone" IS just the sensor. The camera's own motion is
      // whatever's left → one aggregate dot below.
      const localId = (e: Device): string => e.entityId.slice(e.entityId.lastIndexOf(":") + 1);
      const isPresence = (e: Device): boolean => shown(e, "occupancy") || shown(e, "motion");
      const numZones = ents.filter((e) => isPresence(e) && /\d+$/.test(localId(e)));
      const namedZones = ents.filter((e) => e.entityId.includes(":zone:") && shown(e, "occupancy"));
      const zoneSet = new Set([...(numZones.length >= 2 ? numZones : []), ...namedZones]);
      const rest = ents.filter((e) => !zoneSet.has(e));
      // Icon markers first so motion dots paint ON TOP (grabbable while co-located).
      const otherEnts = rest.filter((e) => !isPresence(e) && !hasRoomReadings(e));
      if (otherEnts.length) {
        const rep = otherEnts[0];
        const auto = otherEnts.some((e) => shown(e, "smoke")) ? "smoke"
                   : otherEnts.some((e) => shown(e, "contact")) ? "window"
                   : otherEnts.some((e) => ["power", "current", "energy"].some((c) => shown(e, c))) ? "outlet"
                   : "sensor";
        out.push({
          key: nk, members: otherEnts,
          name: devices.deviceLabel(nk) ?? rep.name,
          areaId: otherEnts.find((e) => e.areaId != null)?.areaId ?? null,
          type: "sensor", icon: rep.fpStyle?.icon ?? auto, scale: rep.fpStyle?.scale ?? 1, tint: rep.fpStyle?.color ?? null,
          styleId: rep.entityId, toggle: null, hasExtra: true, slot: "main",
        });
      }
      // Disambiguate camera zones by their camera ("Shed · Driveway" vs "West ·
      // Driveway"), matching the scene naming — but don't double up when the zone
      // name already equals the camera ("Doorbell").
      const camName = ents.find((e) => "camera" in e.caps)?.name ?? devices.deviceLabel(nk);
      for (const z of zoneSet) {
        const zName = camName && z.entityId.includes(":zone:") && z.name !== camName
          ? `${camName} · ${z.name}` : z.name;
        out.push({
          key: `${z.entityId}:motion`, members: [z], name: zName, areaId: z.areaId,
          type: "sensor", icon: z.fpStyle?.icon ?? "presence", scale: z.fpStyle?.scale ?? 1, tint: z.fpStyle?.color ?? null,
          styleId: z.entityId, toggle: null, hasExtra: true, slot: "motion", labelShow: "motion",
        });
      }
      const motionEnts = rest.filter(isPresence);
      if (motionEnts.length) {
        const rep = motionEnts[0];
        out.push({
          key: `${nk}:motion`, members: motionEnts, name: devices.deviceLabel(nk) ?? rep.name,
          areaId: motionEnts.find((e) => e.areaId != null)?.areaId ?? null,
          type: "sensor", icon: rep.fpStyle?.icon ?? "presence", scale: rep.fpStyle?.scale ?? 1, tint: rep.fpStyle?.color ?? null,
          styleId: rep.entityId, toggle: null, hasExtra: true, slot: "motion", labelShow: "motion",
        });
      }
    }
  }
  return out;
}

/** Placed on ANY floor (the sidebar's "unplaced" filter). */
export const isPlacedAny = (it: PlaceItem): boolean =>
  it.slot === "motion" ? it.members.some((d) => d.fpMotionFloor != null)
                       : it.members.some((d) => d.fpFloor != null);

/** The item's position on the GIVEN floor, or null when it isn't placed there. */
export function itemPosOn(it: PlaceItem, floor: string): { x: number; y: number } | null {
  if (it.slot === "motion") {
    const m = it.members.find((d) => d.fpMotionFloor === floor && d.fpMotionX != null && d.fpMotionY != null);
    return m ? { x: m.fpMotionX as number, y: m.fpMotionY as number } : null;
  }
  const m = it.members.find((d) => d.fpFloor === floor && d.fpX != null && d.fpY != null);
  return m ? { x: m.fpX as number, y: m.fpY as number } : null;
}

// A pure read-out node — no actuator/command cap — renders as a transparent
// value label (temp/humidity/lux + a person icon for motion) instead of an
// icon marker. A node with any actuator cap (mower, cover, climate…) stays a
// FloorMarker so its controls/badge survive.
const LABEL_META = new Set([
  "on_off", "enum_options", "effect_options", "number_options", "hvac_mode_options", "fan_mode_options",
  "vacuum_mode_options", "source", "source_options",
]);
const READOUT = new Set(["sensor", "binary", "event"]);
export function isSensorLabel(it: PlaceItem): boolean {
  if (it.toggle != null) return false;
  let signal = false; // at least one shown reading or a presence cap
  for (const m of it.members) {
    for (const c of Object.keys(m.caps)) {
      if (LABEL_META.has(c) || m.hiddenCaps.includes(c)) continue;
      if (!READOUT.has(capMeta(c).control)) return false; // actuator/command → keep the icon marker
      if (c === "occupancy" || c === "motion") signal = true;
      else if (capMeta(c).control === "sensor" && typeof m.caps[c].value === "number" && !PLAN_HIDDEN_READINGS.has(c)) signal = true;
    }
  }
  return signal;
}

// An APPLIANCE — a metering plug, a washer/dryer — drives an appliance marker: JUST
// an icon that lights while it runs; a tap reveals its readings (power / energy /
// state) so you never cut it mid-run. Anything that meters power/current/energy and
// isn't a light qualifies — even a plug whose on/off relay is hidden (a Shelly plug
// on a dryer, a Samsung washer via SmartThings). A metering LIGHT stays a light (tap
// toggles it), so lights are excluded.
export const isPlug = (it: PlaceItem): boolean =>
  it.type !== "light" &&
  it.members.some((m) => ["power", "current", "energy"].some((c) => shown(m, c) && typeof m.caps[c]?.value === "number"));

// Items whose marker tap should OPEN the control popover (with an in-popover on/off
// switch) rather than toggle on the spot: plugs (their readings are the point) and any
// entity flagged `guarded` — a heavy switch (big-screen) that must not fire on a stray tap.
export const opensControls = (it: PlaceItem): boolean =>
  isPlug(it) || it.toggle?.fpStyle?.guarded === true;

// ── ROOM sensor labels: one aggregated label per area on a floor. Its sensors
//    are the entities assigned to the area that report a numeric reading; per
//    capability we show the mean. Anchored at the area's fp_x/fp_y. ──
export interface RoomLabelItem { area: Area; sensors: Device[]; caps: RoomCap[]; x: number; y: number; off: boolean }

/** The devices feeding each area's room label, by area id. */
export function roomSensorsByArea(): Map<number, Device[]> {
  // Every device eligible to feed a room label: a pure ambient sensor, or a thermostat
  // (AC / TRV — has a setpoint, so it measures a room-ish temperature). roomAggregate
  // defaults pure sensors IN and thermostats OUT (unless a thermostat is the only source),
  // and the user curates from there — an AC's ambient temp is good, a TRV's is biased,
  // but the caps can't tell them apart. A plain relay's internal temp is never eligible.
  const pool = devices.list.filter((d) => d.exposed && !d.diagnostic && d.areaId != null && roomEligible(d));
  const byArea = new Map<number, Device[]>();
  for (const d of pool) {
    const arr = byArea.get(d.areaId as number);
    if (arr) arr.push(d); else byArea.set(d.areaId as number, [d]);
  }
  return byArea;
}

/** A staircase isn't a room — its label is off by default (but re-enableable); any
 *  other area shows by default. sensor_config.off is the explicit override. */
export const roomLabelOff = (a: Area): boolean => a.sensor_config?.off ?? a.kind === "stairs";

/** Every label-eligible area on the floor (incl. hidden ones — callers filter `off`). */
export function buildRoomLabels(floor: string): RoomLabelItem[] {
  const byArea = roomSensorsByArea();
  const out: RoomLabelItem[] = [];
  for (const a of devices.areas) {
    if (a.fp_floor !== floor) continue; // room lives on this floor
    const sensors = byArea.get(a.id);
    if (!sensors) continue;
    const caps = roomAggregate(sensors, a.sensor_config);
    if (!caps.some((c) => !c.hidden && c.mean != null)) continue; // nothing to show
    // Anchor at the area's own position; fall back to its polygon centroid.
    const pos = a.fp_x != null && a.fp_y != null ? { x: a.fp_x, y: a.fp_y }
              : a.fp_poly ? polygonCentroid(a.fp_poly) : null;
    if (!pos) continue;
    out.push({ area: a, sensors, caps, x: pos.x, y: pos.y, off: roomLabelOff(a) });
  }
  return out.sort((p, q) => devices.roomLabel(p.area).localeCompare(devices.roomLabel(q.area), "hr"));
}

// ── glow: each lit LIGHT contributes a shape (own radius, else its room polygon) ──
function litColor(m: Device): string {
  const v = m.caps["color_rgb"]?.value;
  return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#ffdca8"; // warm white
}
function litOp(m: Device): number {
  const b = m.caps["brightness"]?.value;
  const frac = typeof b === "number" ? Math.max(0, Math.min(1, b / 100)) : 1;
  return 0.18 + 0.32 * frac;
}

// --- laser ray cache ------------------------------------------------------
// The cast rays depend only on WHERE the marker sits and the polygons they are
// clipped to — never on the live light state. buildGlows, though, re-runs whenever
// any light capability changes (and 20×/s during replay), so solving the rays
// inline meant ~10 000 point-in-polygon tests per tick for as long as a laser was
// on. Cache the solved geometry and recompute only when it actually moves.
//
// Keyed by marker position + the IDENTITY of each cover polygon: the store hands
// out a NEW polygon object when a room outline is edited, so an edit misses the
// cache (correct) while a state tick hits it (fast).
const _polyIds = new WeakMap<object, number>();
let _nextPolyId = 0;
const _rayCache = new Map<string, { x2: number; y2: number; color: string }[]>();
const _RAY_CACHE_MAX = 64; // a handful of lasers × a few positions; bounded, never a leak

function polyId(poly: object): number {
  let id = _polyIds.get(poly);
  if (id === undefined) { id = ++_nextPolyId; _polyIds.set(poly, id); }
  return id;
}

function castLaserRays(
  px: number, py: number, covers: FloorPoly[], palette: string[],
): { x2: number; y2: number; color: string }[] {
  const key = `${px},${py}|${covers.map(polyId).join(",")}`;
  const hit = _rayCache.get(key);
  if (hit) return hit;
  const inCover = (x: number, y: number) => covers.some((poly) => pointInPolygon(x, y, poly));
  const rays: { x2: number; y2: number; color: string }[] = [];
  const N = 54;
  for (let i = 0; i < N; i++) {
    const ang = (i / N) * Math.PI * 2;
    const dx = Math.cos(ang), dy = Math.sin(ang);
    let last: { x: number; y: number } | null = null;
    for (let t = 1; t < 90; t += 0.9) {
      const x = px + dx * t, y = py + dy * t;
      if (inCover(x, y)) last = { x, y }; else if (last) break;
    }
    if (last) rays.push({ x2: last.x, y2: last.y, color: palette[i % palette.length] });
  }
  if (_rayCache.size >= _RAY_CACHE_MAX) _rayCache.delete(_rayCache.keys().next().value as string);
  _rayCache.set(key, rays);
  return rays;
}

export function buildGlows(
  placed: { it: PlaceItem; p: { x: number; y: number } | null }[],
  floorAreas: Area[], areaById: Map<number, Area>, floor: string,
): GlowSpec[] {
  const radial: GlowSpec[] = [];
  const beams: GlowSpec[] = [];
  const roomGlow = new Map<number, { op: number; color: string }>(); // dedupe lights sharing a room
  for (const { it, p } of placed) {
    if (it.type !== "light" || !it.toggle || !p) continue;
    if (it.toggle.caps["on_off"]?.value !== true) continue;
    const color = litColor(it.toggle), op = litOp(it.toggle);
    if (it.toggle.fpStyle?.effect === "laser") {
      // A laser projects rays, not a flat wash: cast beams from the marker across the
      // open living/dining polygons (clipped to their walls) with a speckle dot at each
      // tip. Colour is a placeholder laser-green (a real per-device tint can override).
      const covers = floorAreas.filter((a) => (a.kind === "living" || a.kind === "dining") && a.fp_poly)
        .map((a) => a.fp_poly as FloorPoly);
      const PALETTE = ["#43f08e", "#4d84ff", "#ff5566"]; // green / blue / red — a party laser
      const rays = castLaserRays(p.x, p.y, covers, PALETTE);
      if (rays.length) beams.push({ id: it.key, kind: "beams", x: p.x, y: p.y, rays,
        color: it.toggle.fpStyle?.color ?? "#43f08e", op: Math.min(0.7, op + 0.1) });
      continue;
    }
    const r = it.toggle.fpGlow?.r;
    // Room glow follows where the marker sits, not the light's administrative
    // area — a light drawn in the dining half must light the dining half, even
    // if it's assigned to the living room. Fall back to the assigned room only
    // when the marker lands outside every traced polygon on this floor.
    const here = floorAreas.find((a) => pointInPolygon(p.x, p.y, a.fp_poly as FloorPoly));
    const assigned = it.areaId != null ? areaById.get(it.areaId) : null;
    const room = here ?? (assigned?.fp_poly && assigned.fp_floor === floor ? assigned : null);
    if (r) {
      radial.push({ id: it.key, kind: "radius", x: p.x, y: p.y, r, color, op });
    } else if (room) {
      const cur = roomGlow.get(room.id);
      if (!cur || op > cur.op) roomGlow.set(room.id, { op, color }); // brightest light wins colour
    } else {
      radial.push({ id: it.key, kind: "radius", x: p.x, y: p.y, r: DEFAULT_R, color, op });
    }
  }
  const poly: GlowSpec[] = [];
  for (const [areaId, g] of roomGlow) {
    const a = areaById.get(areaId);
    if (a?.fp_poly) poly.push({ id: `area-${areaId}`, kind: "poly", points: toPoints(a.fp_poly), color: g.color, op: g.op });
  }
  return [...poly, ...radial, ...beams];
}
