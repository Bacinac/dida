// Universal-remote projection over the canonical capability group. A Harmony hub
// is ONE device whose head entity carries a `source` picker (the activities — the
// one real, readable state) plus sibling `press` button entities (D-pad /
// transport / volume). The UI's Remote widget recomposes them into a remote; the
// core stays generic. A device with `media_transport` is a full media player and
// is rendered by MediaPlayer instead — never here.

import { api } from "$lib/api";
import type { Device } from "$lib/store.svelte";

// Canonical remote key suffixes — mirrored from the adapter's mapping.py
// REMOTE_KEYS. The button entity id is `<head>_<suffix>`; a suffix the device
// doesn't expose simply isn't laid out.
export type RemoteKey =
  | "up" | "down" | "left" | "right" | "ok"
  | "back" | "home" | "menu"
  | "play" | "pause" | "stop" | "skip_back" | "skip_fwd"
  | "vol_down" | "vol_up" | "mute";

// Detection keys off `capabilities` (the declared set), NOT `caps` (live state):
// a `press` button is write-only, so it never carries state and would be invisible
// to a caps-based check. `source`/`source_options` do carry state (read on the head
// via caps in the widget), but membership is still decided by capabilities.

/** The head entity of a remote (the one carrying the `source` picker). */
export function remoteHead(members: Device[]): Device | null {
  return members.find((m) => m.capabilities.includes("source")) ?? null;
}

/** True for a remote device: a `source` head + ≥1 sibling `press` button, and no
 *  `media_transport` (that would be a full media player). */
export function isRemote(members: Device[]): boolean {
  if (members.some((m) => m.capabilities.includes("media_transport"))) return false;
  const head = remoteHead(members);
  if (!head) return false;
  const prefix = `${head.entityId}_`;
  return members.some((m) => m.capabilities.includes("press") && m.entityId.startsWith(prefix));
}

/** suffix → button entity_id, for every press sibling of the head. */
export function remoteButtons(members: Device[]): Partial<Record<RemoteKey, string>> {
  const head = remoteHead(members);
  if (!head) return {};
  const prefix = `${head.entityId}_`;
  const out: Partial<Record<RemoteKey, string>> = {};
  for (const m of members) {
    if (m.capabilities.includes("press") && m.entityId.startsWith(prefix)) {
      out[m.entityId.slice(prefix.length) as RemoteKey] = m.entityId;
    }
  }
  return out;
}

/** Button entity ids that the Remote hero renders — excluded from the generic
 *  field list so they don't also show as a flat "press" row list. */
export function remoteButtonIds(members: Device[]): Set<string> {
  return new Set(Object.values(remoteButtons(members)));
}

/** Fire one remote key. */
export const pressKey = (entityId: string): Promise<unknown> =>
  api.sendCommand({ entity_id: entityId, capability: "press", command: "press" });
