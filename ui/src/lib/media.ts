// Media-player projection over the canonical capability group. A DLNA renderer
// is one device whose caps include `media_transport`; this module reads those
// caps into a friendly now-playing shape and wraps the command calls. The UI
// recomposes the scalar caps into a single player — the core stays generic.

import { api } from "$lib/api";
import { t, type MessageKey } from "$lib/i18n";
import type { Device } from "$lib/store.svelte";

// Caps that belong to the media group — DeviceCard renders these via the
// MediaPlayer component instead of the generic capability list.
export const MEDIA_CAPS = new Set([
  "media_transport", "volume", "mute", "media_title", "media_artist",
  "media_album", "media_art", "media_duration", "media_position",
  // Static "has a screen" marker owned by the media grouping — not a reading, so
  // the card must not surface it as a stray diagnostic (it drives the panel picker).
  "media_display",
]);

export function isMediaPlayer(d: Device): boolean {
  return "media_transport" in d.caps;
}

export type Transport = "playing" | "paused" | "stopped" | "idle" | "buffering";

function str(d: Device, cap: string): string | null {
  const v = d.caps[cap]?.value;
  return typeof v === "string" && v.trim() ? v : null;
}
function num(d: Device, cap: string): number | null {
  const v = d.caps[cap]?.value;
  return typeof v === "number" ? v : null;
}

export function transport(d: Device): Transport {
  const v = d.caps["media_transport"]?.value;
  return (typeof v === "string" ? v : "idle") as Transport;
}
export const isPlaying = (d: Device): boolean => transport(d) === "playing";
export const isActive = (d: Device): boolean => ["playing", "paused", "buffering"].includes(transport(d));

export const mediaTitle = (d: Device): string | null => str(d, "media_title");
export const mediaArtist = (d: Device): string | null => str(d, "media_artist");
export const mediaAlbum = (d: Device): string | null => str(d, "media_album");
export const mediaArt = (d: Device): string | null => str(d, "media_art");
export const mediaQuality = (d: Device): string | null => str(d, "media_quality");
export const mediaSource = (d: Device): string | null => str(d, "media_source");
// The play queue the adapter exposes — universal across sources.
export interface QueueItem {
  title: string;
  artist: string;
  art: string;
}
export const mediaQueue = (d: Device): { items: QueueItem[]; current: number } => {
  const raw = str(d, "media_queue");
  if (!raw) return { items: [], current: -1 };
  try {
    const q = JSON.parse(raw);
    return {
      items: Array.isArray(q.items) ? q.items : [],
      current: typeof q.current === "number" ? q.current : -1,
    };
  } catch {
    return { items: [], current: -1 };
  }
};
export const mediaVolume = (d: Device): number | null => num(d, "volume");
export const mediaMuted = (d: Device): boolean => d.caps["mute"]?.value === true;
export const mediaDuration = (d: Device): number => num(d, "media_duration") ?? 0;

/** Position baseline the UI extrapolates from: the last published position and
 *  the moment it was published. While playing, displayed pos = base + elapsed. */
export function positionBase(d: Device): { pos: number; at: number } | null {
  const cs = d.caps["media_position"];
  if (!cs || typeof cs.value !== "number") return null;
  return { pos: cs.value, at: cs.updatedAt };
}

/** Localised transport-state label. */
export function transportLabel(state: Transport): string {
  return t(`media.${state}` as MessageKey);
}

/** mm:ss (or h:mm:ss past an hour). */
export function fmtTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(s).padStart(2, "0")}`;
}

/** Deterministic gradient from a seed string — station tiles & art fallback,
 *  so the UI never depends on (and never breaks on) an external image. */
export function gradient(seed: string): string {
  let h = 0;
  for (const c of seed) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const a = h % 360;
  const b = (a + 40 + ((h >> 8) % 80)) % 360;
  return `linear-gradient(135deg, hsl(${a} 58% 44%), hsl(${b} 62% 26%))`;
}

// --- commands ----------------------------------------------------------------

const send = (entity_id: string, capability: string, command: string, args?: Record<string, string | number>) =>
  api.sendCommand({ entity_id, capability, command, args });

// AV-receiver input source (denon adapter). `source` is the current input;
// `source_options` is a JSON list of selectable inputs.
export function sourceValue(d: Device): string | null {
  const v = d.caps["source"]?.value;
  return typeof v === "string" ? v : null;
}
export function sourceOptions(d: Device): string[] {
  const v = d.caps["source_options"]?.value;
  if (typeof v !== "string") return [];
  try {
    const arr = JSON.parse(v);
    return Array.isArray(arr) ? arr.map(String) : [];
  } catch {
    return [];
  }
}
export const setSource = (id: string, value: string) =>
  api.sendCommand({ entity_id: id, capability: "source", command: "set_source", args: { value } });

// AVR zone = controllable but not a player (the denon zones).
export const isAvrZone = (d: Device): boolean =>
  !("media_transport" in d.caps) && ("on_off" in d.caps || "source" in d.caps || "volume" in d.caps);

export const mediaCmd = {
  play: (id: string) => send(id, "media_transport", "play"),
  pause: (id: string) => send(id, "media_transport", "pause"),
  stop: (id: string) => send(id, "media_transport", "stop"),
  next: (id: string) => send(id, "media_transport", "next"),
  previous: (id: string) => send(id, "media_transport", "previous"),
  playIndex: (id: string, index: number) => send(id, "media_transport", "play_index", { index }),
  playPause: (d: Device) => send(d.entityId, "media_transport", isPlaying(d) ? "pause" : "play"),
  playMedia: (id: string, url: string, title = "", art = "") =>
    send(id, "media_transport", "play_media", { uri: url, title, art }),
  setVolume: (id: string, value: number) => send(id, "volume", "set_volume", { value }),
  toggleMute: (id: string) => send(id, "mute", "toggle"),
};

// Radio-aware skip: on a live radio stream, "next/previous" means the neighbouring
// DIDA station (radio:tuner cycles the station list on its target player) — a plain
// media_transport next the renderer would ignore. On a queue it's a track skip.
export const nextTrack = (d: Device): Promise<unknown> =>
  mediaSource(d) === "radio"
    ? send("radio:tuner", "media_transport", "next")
    : send(d.entityId, "media_transport", "next");
export const prevTrack = (d: Device): Promise<unknown> =>
  mediaSource(d) === "radio"
    ? send("radio:tuner", "media_transport", "previous")
    : send(d.entityId, "media_transport", "previous");
