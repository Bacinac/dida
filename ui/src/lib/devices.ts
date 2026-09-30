// Device grouping — collapse the entities that are facets of ONE physical device
// into a single unit, so the UI is device-centric (one card per device) rather
// than entity-centric. Shared by the Devices page and the floor plan.
//
// Grouping key: the persisted device_key when present, else a derived
// `adapter:node` (an ESPHome node's entity_id is `adapter:node:field`, so its
// fields group even before the backend backfills device_key). A 2-part id
// (`adapter:entity`) is a device of one.

import { deviceType, entityType, type DeviceType } from "$lib/capabilities";
import { devices, prettify, type CapState, type Device } from "$lib/store.svelte";

export interface DeviceUnit {
  key: string;
  members: Device[];
  name: string;
  areaId: number | null;
  type: DeviceType;
  adapters: string[];
  site: string | null;
}

export function derivedKey(entityId: string): string {
  const p = entityId.split(":");
  return p.length >= 3 ? `${p[0]}:${p[1]}` : entityId;
}

/** A device's display name: the user/adapter device label keyed on the GROUP key
 *  (device_key for grouped, or the derived `adapter:node`/entity_id for ungrouped —
 *  so a renamed cast/dlna device resolves too), else the prettified node segment
 *  of a grouped id, else — the HEAD entity's own name. The head is the member
 *  whose entity_id IS the device_key: a grouped helper (mower + its intensity
 *  slider) must never lend the card its name just because it sorted first. */
function unitName(key: string, members: Device[]): string {
  const label = devices.deviceLabel(key);
  if (label) return label;
  const head = members.find((m) => m.entityId === key) ?? members[0];
  const parts = head.entityId.split(":");
  return parts.length >= 3 ? prettify(parts[1]) : head.name;
}

export function groupDevices(devs: Device[]): DeviceUnit[] {
  const grouped = new Map<string, Device[]>();
  for (const d of devs) {
    const k = d.deviceKey || derivedKey(d.entityId);
    const arr = grouped.get(k);
    if (arr) arr.push(d);
    else grouped.set(k, [d]);
  }
  const out: DeviceUnit[] = [];
  for (const [key, members] of grouped) {
    const caps = Object.assign({}, ...members.map((m) => m.caps)) as Record<string, CapState>;
    // DIDA's canonical device_type is the source of truth when the members agree
    // on one; a mixed multi-gang device (per-gang types) falls back to the
    // capability classification for the card's summary icon. A remote is a
    // deliberate mix — a `remote` head + its `button` keys — so the head wins.
    // "other" members never veto a typed sibling (a mower grouped with its
    // 'other' intensity helper is still a mower).
    const declared = new Set(
      members.map((m) => m.deviceType).filter((t): t is string => !!t && t !== "other"),
    );
    const canonical = declared.size === 1 ? [...declared][0]
      : declared.has("remote") ? "remote"
      : null;
    out.push({
      key,
      members,
      name: unitName(key, members),
      areaId: members.find((m) => m.areaId != null)?.areaId ?? null,
      type: canonical ? entityType(canonical, caps) : deviceType(caps),
      adapters: [...new Set(members.map((m) => m.adapter))],
      site: devices.deviceSite(key),
    });
  }
  return out;
}

// ── Area eligibility ───────────────────────────────────────────────────────
// Not every entity belongs in a physical room. Presence trackers belong to a
// PERSON (already surfaced in the Presence strip), and calendar / notify / astro
// are pure plumbing — waste-collection & irrigation schedules, push targets, the
// sun clock. These are area-EXEMPT: no room picker on the card, and never counted
// as "missing a room". Derived template sensors and virtual helpers MAY feed a
// room aggregate so they keep the picker, but an unassigned one is a valid
// resting state — they don't trigger the nudge either. Everything else is a
// physical device that should live somewhere → it nags until placed.
// `helper` = computed helpers (day/night, quiet-hours, …): pure derived values with no
// physical location, like the sun clock — non-spatial, never a room, never a nag.
const NON_SPATIAL_ADAPTERS = new Set<string>(["calendar", "contacts", "notify", "astro", "helper"]);
const OPTIONAL_AREA_ADAPTERS = new Set<string>(["derived", "virtual"]);
type AreaClassifiable = { type: DeviceType; adapters: string[]; site?: string | null };

/** The room concept doesn't apply to this unit — hide its area picker. */
export function areaExempt(u: AreaClassifiable): boolean {
  return u.type === "presence"
    || u.adapters.every((a) => NON_SPATIAL_ADAPTERS.has(a))
    // A device at another installation does have a room — there. This house has
    // none to offer it, so it neither nags to be placed nor sits on this plan.
    || !!u.site;
}

/** A physical device that belongs in a room — an unassigned one triggers the
 *  nudge. Exempt units and optional-area (derived/virtual) units don't. */
export function areaRequired(u: AreaClassifiable): boolean {
  return !areaExempt(u) && !u.adapters.every((a) => OPTIONAL_AREA_ADAPTERS.has(a));
}
