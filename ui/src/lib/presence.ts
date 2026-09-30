// Presence projection: turn the live device store into "people" with honest
// staleness. A presence entity reports only while its DIDA app is open (or, for
// a network source, while its adapter keeps confirming), so a last location must
// NOT be presented as current forever — past the staleness window the UI fades
// it and appends the age ("Vani · prije 20 min") instead of lying that it's live.
// It keeps the last-known ZONE though — never a bare "Unknown" or a placeless
// "away" — surfacing the last real place from history + its age. See
// presenceStatus.

import { t } from "$lib/i18n";

import { clock, shortDateTime } from "$lib/dt";
import type { Device } from "$lib/store.svelte";

// A zone is LATCHED, not sampled: OwnTracks (significant-change + pushed zone
// waypoints) fires an event-driven region ENTER/LEAVE, so a person stays at
// their zone until the phone reports leaving it — silence means "hasn't moved",
// never "gone". So we do NOT fade a zone with time; this window is only a
// dead-signal BACKSTOP: no enter/leave AND no fix for this long (phone off, no
// connectivity) → assume unknown and fade to last-known. 24 h matches the
// the network reconciler grace so network and GPS never disagree. Departures are
// the leave transition (or a fix that resolves elsewhere/"away"), not a timer.
export const PRESENCE_STALE_MS = 24 * 60 * 60_000;

export interface Person {
  entityId: string;
  name: string;
  location: string; // raw: a zone name, or "away"
  lat: number | null;
  lon: number | null;
  battery: number | null;
  updatedAt: number; // epoch ms of the location reading
  stale: boolean;
}

const num = (v: unknown): number | null => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/** Presence entities (those exposing `location`) as people, name-sorted. */
export function persons(list: Device[], now: number): Person[] {
  return list
    .filter((d) => "location" in d.caps)
    .map((d) => {
      const loc = d.caps.location;
      const updatedAt = loc?.updatedAt ?? d.lastSeen;
      return {
        entityId: d.entityId,
        name: d.name,
        location: String(loc?.value ?? ""),
        lat: "latitude" in d.caps ? num(d.caps.latitude.value) : null,
        lon: "longitude" in d.caps ? num(d.caps.longitude.value) : null,
        battery: "battery" in d.caps ? num(d.caps.battery.value) : null,
        updatedAt,
        stale: now - updatedAt > PRESENCE_STALE_MS,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Translated location label: a zone name shows as-is, "away" is translated. */
export function locationLabel(location: string): string {
  return location === "away" ? t("presence.away") : location;
}

/** Clock time of a past moment: "09:28" when it's today, "8.7. 09:28" on an
 *  earlier day — the actual time the person was last at a place, not "X ago". */
export function atTimeLabel(ms: number, now: number): string {
  const sameDay = new Date(now).toDateString() === new Date(ms).toDateString();
  return sameDay ? clock(ms) : shortDateTime(ms);
}

/** The last real zone a person was seen in (from history), plus when. Sourced
 *  from ClickHouse because current_state only holds the latest value, which the
 *  network adapters overwrite with "away". */
export interface LastZone {
  zone: string;
  ts: number; // epoch ms when last reported at this zone
}

export type PresenceTone = "present" | "lastKnown" | "unknown";

// Past this a last-known zone is history, not a location. The ember gradient has
// long flattened, so a place from weeks ago reads exactly like one from an hour
// ago — which is how a person with no tracker at all came to show a plausible
// zone forever. Same 24 h as the dead-signal backstop: once we would no longer
// trust a live reading, we no longer offer the old one as an answer either.
export const LAST_ZONE_MAX_MS = PRESENCE_STALE_MS;

export interface PresenceStatus {
  text: string;
  /** The place alone — what a narrow chip truncates. */
  place: string;
  /** Clock time of a last-known fix, "" when the place is current. */
  at: string;
  tone: PresenceTone;
  /** Inline `color:…` for the age-graded last-known amber; "" for present/away
   *  (those take their colour from presenceToneClass). */
  colorStyle: string;
}

/** A "cooling ember" for the AGE of a last-known fix: fresh = a bright gold-amber;
 *  as it ages the hue slides toward red-orange AND desaturates + dims. Hue shift
 *  makes an hour's difference obvious at a glance (saturation alone was too
 *  subtle). Cube-root → most of the travel happens in the first hours, then it
 *  flattens; past ~12 h it's a dim, washed red-brown. Stays clear of the present
 *  green and the away grey. */
export function lastKnownColor(ageMs: number): string {
  const min = Math.max(0, ageMs / 60_000);
  const t = Math.min(1, Math.cbrt(min / 720));
  const hue = Math.round(50 - 34 * t);   // 50 gold-amber (fresh) → 16 red-orange (old)
  const sat = Math.round(95 - 62 * t);   // 95% vivid → 33% washed
  const light = Math.round(62 - 14 * t); // 62% bright → 48% dim
  return `hsl(${hue} ${sat}% ${light}%)`;
}

/** What to show for a person and how to colour it — "away" is a network verdict,
 *  not a place, so it never stands in for a location when a real zone is known:
 *   • present   — live, fresh, real zone → they're there now (green). A network
 *                 "Home" counts: the association is proof they are on the house AP.
 *   • lastKnown — not currently confirmed → the last real zone + when; the amber
 *                 fades with age (lastKnownColor) so recency shows.
 *   • unknown   — nothing live and no zone recent enough to stand for one → say so.
 *                 Never "Vani": that asserts they are out, and a phone that stopped
 *                 reporting is evidence of nothing. */
export function presenceStatus(p: Person, last: LastZone | undefined, now: number): PresenceStatus {
  const atLiveZone = !p.stale && p.location !== "" && p.location !== "away";
  if (atLiveZone) {
    const place = locationLabel(p.location);
    return { text: place, place, at: "", tone: "present", colorStyle: "" };
  }
  if (last && now - last.ts <= LAST_ZONE_MAX_MS) {
    const at = atTimeLabel(last.ts, now);
    return {
      text: `${last.zone} · ${at}`,
      place: last.zone,
      at,
      tone: "lastKnown",
      colorStyle: `color:${lastKnownColor(now - last.ts)}`,
    };
  }
  const unknown = t("presence.unknown");
  return { text: unknown, place: unknown, at: "", tone: "unknown", colorStyle: "" };
}

/** Tone → text colour for the static tones; lastKnown is "" (its colour is the
 *  inline age gradient in colorStyle). Shared by the track and the map header. */
export const presenceToneClass: Record<PresenceTone, string> = {
  present: "text-dida-ok",
  lastKnown: "",
  unknown: "text-dida-text-muted",
};
