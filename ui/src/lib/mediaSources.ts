// The source-abstraction layer behind the MediaHub. A "source" is an experience
// the user picks in a room, mapped to its control mechanism:
//   • activity → a Harmony activity (internal AV; keeps the physical remote in sync)
//   • player   → a streaming player, optionally routed through an AVR zone
//               (external; e.g. iFi → Marantz "MUSIC" → Zone 2 on the patio)
// This module resolves a stored (or auto-derived) registry against the live device
// list, decides which source is currently active from real state, and dispatches
// the "select this source" action through the permission-gated /command path — the
// registry (areas.media_config) stays the single source of truth for the mapping.

import { api, type MediaSource } from "$lib/api";
import { isActive } from "$lib/media";
import { devices, type Device } from "$lib/store.svelte";

/** A registry source resolved against live devices, ready for the widget. */
export interface ResolvedSource {
  src: MediaSource;
  areaId: number | null;
  areaName: string | null;
  active: boolean;             // derived from live state (reflects the physical remote too)
  player: Device | null;       // kind=player: the renderer
  zone: Device | null;         // optional AVR zone (route + volume target)
  avr: Device | null;          // AVR main zone (volume target when the source has no own zone)
  nowplaying: Device | null;   // kind=activity: where to read now-playing, if any
  remoteHead: Device | null;   // kind=activity: the remote's source head
  remoteMembers: Device[];     // kind=activity: head + press siblings, for the Remote widget
}

function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "src";
}

/** Candidate ACTIVITY sources derived from the live Harmony hub(s) — the internal
 *  AV experiences. Deliberately NOT players: a streaming source (iFi → a zone) is a
 *  deliberate, per-space choice the user adds by hand, so "Generate" doesn't dump
 *  every renderer into whatever space is open. A source names its devices explicitly,
 *  so this isn't area-scoped. */
export function deriveSources(): MediaSource[] {
  const out: MediaSource[] = [];
  for (const d of devices.list) {
    if (d.deviceType !== "remote" || !d.capabilities.includes("source")) continue;
    const raw = d.caps["source_options"]?.value;
    let acts: string[] = [];
    try {
      const arr = typeof raw === "string" ? JSON.parse(raw) : [];
      acts = Array.isArray(arr) ? arr.map(String) : [];
    } catch {
      acts = [];
    }
    for (const a of acts) {
      if (a === "PowerOff") continue; // the off state, not a source
      out.push({ key: `${slugify(d.entityId)}-${slugify(a)}`, label: a, kind: "activity", remote: d.entityId, activity: a });
    }
  }
  return out;
}

/** The saved source list for one area (space). Empty until the user configures it. */
export function areaSources(areaId: number): MediaSource[] {
  const area = devices.areas.find((a) => a.id === areaId) ?? null;
  return area?.media_config?.sources ?? [];
}

/** The media "spaces": the areas with at least one ENABLED source (for the Living
 *  Room / Outdoor selector). Ordered as the store orders areas. */
export function mediaSpaces(): { areaId: number; name: string }[] {
  return devices.areas
    .filter((a) => a.media_config?.sources?.some((s) => s.enabled !== false) ?? false)
    .map((a) => ({ areaId: a.id, name: devices.roomLabel(a) }));
}

function computeActive(s: MediaSource, zone: Device | null, player: Device | null, head: Device | null, avr: Device | null): boolean {
  // Harmony-gated sources — a plain activity, OR a player launched through an
  // activity (e.g. ZEN → the iFi) — are active when that activity is the one
  // running on the hub. A shared player that happens to be playing for another
  // space (the iFi feeding the patio zone) must NOT light this space's pill up.
  if (s.remote && s.activity) {
    // Anything that routes the amp directly — an automation, the AVR's own knob,
    // OPUS switching to the Shield for a film — bypasses Harmony, which keeps
    // reporting the activity it last started. When the source declares its AVR
    // input, the amplifier has the last word both ways: Harmony on this activity
    // with the amp on another input is silence, and the amp on this input with a
    // playing player is this experience whatever Harmony says.
    // The patio false-positive that motivated the Harmony gate can't return: the
    // patio routes zone2, this checks the source's own (main-zone) AVR entity.
    const routed = s.avr_input && avr
      ? avr.caps["on_off"]?.value === true && avr.caps["source"]?.value === s.avr_input
      : null;
    if (!!head && head.caps["source"]?.value === s.activity) return routed !== false;
    return !!routed && (!s.player || (!!player && isActive(player)));
  }
  // A player routed through a zone is "active" when the zone is on and switched to
  // its input — so the patio card lights up whenever the iFi is actually audible.
  if (s.zone && zone) {
    const inputOk = !s.zone_input || zone.caps["source"]?.value === s.zone_input;
    return zone.caps["on_off"]?.value === true && inputOk;
  }
  return !!player && isActive(player);
}

function resolve(s: MediaSource, areaId: number | null): ResolvedSource {
  const player = s.player ? devices.byId[s.player] ?? null : null;
  const zone = s.zone ? devices.byId[s.zone] ?? null : null;
  const avr = s.avr ? devices.byId[s.avr] ?? null : null;
  const nowplaying = s.nowplaying ? devices.byId[s.nowplaying] ?? null : null;
  const remoteHead = s.remote ? devices.byId[s.remote] ?? null : null;
  const remoteMembers = remoteHead
    ? (remoteHead.deviceKey ? devices.membersOf(remoteHead.deviceKey) : [remoteHead])
    : [];
  return {
    src: s,
    areaId,
    areaName: areaId == null ? null : devices.areaName(areaId),
    active: computeActive(s, zone, player, remoteHead, avr),
    player,
    zone,
    avr,
    nowplaying,
    remoteHead,
    remoteMembers,
  };
}

/** Resolved sources for the widget. Pass an areaId to scope to one space; omit it
 *  for every configured space (the media page's Living Room / Outdoor selector). */
export function resolvedSources(filterAreaId?: number): ResolvedSource[] {
  const ids = filterAreaId !== undefined
    ? [filterAreaId]
    : devices.areas.filter((a) => (a.media_config?.sources?.length ?? 0) > 0).map((a) => a.id);
  const out: ResolvedSource[] = [];
  for (const aid of ids) for (const s of areaSources(aid)) if (s.enabled !== false) out.push(resolve(s, aid));
  return out;
}

/** Fire the "make this the active source" action, hiding the mechanism. The power
 *  path is decided by what the source declares, NOT by `kind` (must match how
 *  turnOffSource/computeActive gate — a player can be Harmony-driven):
 *   • remote+activity → start the Harmony activity (Living Room: powers the Marantz
 *     MAIN zone + selects the input; the physical remote follows). SHIELD, ZEN→iFi, HEOS…
 *   • zone            → a direct AVR zone (patio — no Harmony there): power on + input.
 *   • player          → aim browse at it (the renderer to cast onto).
 *  All commands go through the permission-gated /command path. */
export async function selectSource(rs: ResolvedSource): Promise<void> {
  const s = rs.src;
  if (s.remote && s.activity) {
    await api.sendCommand({ entity_id: s.remote, capability: "source", command: "set_source", args: { value: s.activity } });
    // Harmony believing the activity already runs is no proof the amp listens to
    // it, so a declared input is routed on the AVR itself.
    if (s.avr && s.avr_input) {
      await api.sendCommand({ entity_id: s.avr, capability: "on_off", command: "turn_on" });
      await api.sendCommand({ entity_id: s.avr, capability: "source", command: "set_source", args: { value: s.avr_input } });
    }
  }
  if (s.zone) {
    await api.sendCommand({ entity_id: s.zone, capability: "on_off", command: "turn_on" });
    if (s.zone_input) {
      await api.sendCommand({ entity_id: s.zone, capability: "source", command: "set_source", args: { value: s.zone_input } });
    }
  }
}

/** Icon key (deviceIcons) for a source, with a per-kind fallback. */
export function sourceIcon(s: MediaSource): string {
  if (s.icon) return s.icon;
  return s.kind === "activity" ? "tv" : "player";
}
