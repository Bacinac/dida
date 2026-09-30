// House replay. The floor plan is a projection of the device store, so replaying
// the house substitutes the projection's INPUT instead of building a second
// renderer: one bundle of past values, a cursor, and the store driven frame by
// frame. Everything derived from `devices` — markers, glows, room labels, the
// night overlay — then shows the house as it stood, with no changes of its own.

import { api, type ReplayBundle, type Scalar } from "$lib/api";
import { devices } from "$lib/store.svelte";
import { errMsg } from "$lib/errors";
import { t } from "$lib/i18n";

// Sampled step tracks carry momentary events: a vision zone goes occupied and
// clear inside one second. At a day-wide window a cursor step spans minutes, so
// such a pulse falls between two frames and the house looks empty all evening.
// A pulse is therefore held for the frame it belongs to — the plan says "motion
// fired in this step", which is the honest reading of a sparse event at coarse
// zoom. HOLD_STEPS sets how fine that is (window / 600 ≈ one pixel of timeline).
const HOLD_STEPS = 600;
const FRAME_MS = 50;        // cap the store writes at 20/s — smooth enough for a house
const DEFAULT_HOURS = 24;

export const SPEEDS = [60, 300, 1800] as const; // virtual seconds per real second

interface Track {
  e: string;
  c: string;
  step: boolean;
  at: Scalar | null;
  ts: number[];
  vals: (Scalar | null)[];
}

class Replay {
  active = $state(false);
  loading = $state(false);
  error = $state<string | null>(null);
  truncated = $state(false);
  frm = $state(0);
  to = $state(0);
  cursor = $state(0);
  playing = $state(false);
  speed = $state<number>(SPEEDS[1]);
  hours = $state(DEFAULT_HOURS);

  #tracks: Track[] = [];
  #applied = new Map<string, Scalar | null>();  // last value pushed per track — write only changes
  #entities: string[] = [];              // the set the caller renders — kept for reopen()
  #raf: number | null = null;
  #lastTick = 0;
  #lastFrame = 0;

  get hold(): number {
    return Math.max(1000, (this.to - this.frm) / HOLD_STEPS);
  }

  /** Open a window ending now over the entities the caller renders. */
  async open(entities: string[], hours = DEFAULT_HOURS): Promise<void> {
    this.pause();
    this.loading = true;
    this.error = null;
    this.#entities = entities;
    const to = Date.now();
    const frm = to - hours * 3600 * 1000;
    try {
      const bundle = await api.replayBundle(frm, to, entities);
      // Nothing recorded for this plan (a fresh install, or the public demo, which
      // carries one instant and no window). Say so and stay live rather than
      // entering a replay of nothing — #ingest would blank the store first.
      if (bundle.tracks.length === 0) {
        this.error = t("replay.noHistory");
        this.active = false;
        return;
      }
      this.#ingest(bundle, entities);
      this.hours = hours;
      this.active = true;
      this.cursor = this.frm;
      this.#push(true);
    } catch (e) {
      this.error = errMsg(e);
      this.active = false;
      if (devices.replaying) await devices.exitReplay(); // only if the store was already blanked
    } finally {
      this.loading = false;
    }
  }

  /** Same entities, a different window — the range buttons. */
  async reopen(hours: number): Promise<void> {
    if (this.#entities.length) await this.open(this.#entities, hours);
  }

  #ingest(bundle: ReplayBundle, requested: string[]): void {
    this.frm = bundle.frm;
    this.to = bundle.to;
    this.truncated = bundle.truncated;
    this.#applied.clear();
    const covered: Record<string, string[]> = {};
    this.#tracks = bundle.tracks.map((t) => {
      (covered[t.e] ??= []).push(t.c);
      const ts: number[] = [];
      const vals: (Scalar | null)[] = [];
      for (const [ms, v] of t.p) { ts.push(ms); vals.push(v); }
      return { e: t.e, c: t.c, step: t.k === "s", at: t.at, ts, vals };
    });
    // The store must not keep a live value for a replayed entity: a capability
    // with no history in the window has to read as absent, not as "now".
    devices.enterReplay(requested, covered);
  }

  /** The value a track held at `at`, or its window-opening value if it had none. */
  #valueAt(t: Track, at: number): Scalar | null {
    let lo = 0;
    let hi = t.ts.length - 1;
    let idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (t.ts[mid] <= at) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (idx < 0) return t.at;
    // Hold a momentary pulse for its frame (see HOLD_STEPS): scan back over the
    // step for an active edge the exact-value lookup would have stepped over.
    if (t.step) {
      const floor = at - this.hold;
      for (let i = idx; i >= 0 && t.ts[i] > floor; i--) {
        if (t.vals[i] === true || t.vals[i] === 1) return t.vals[i];
      }
    }
    return t.vals[idx];
  }

  #push(force = false): void {
    const at = this.cursor;
    const changed: Record<string, Record<string, Scalar | null>> = {};
    for (const t of this.#tracks) {
      const v = this.#valueAt(t, at);
      const key = `${t.e}|${t.c}`;
      if (!force && this.#applied.get(key) === v) continue;
      this.#applied.set(key, v);
      (changed[t.e] ??= {})[t.c] = v;
    }
    devices.applyReplay(at, changed);
  }

  seek(at: number): void {
    this.cursor = Math.max(this.frm, Math.min(this.to, at));
    this.#push();
  }

  play(): void {
    if (this.playing || !this.active) return;
    if (this.cursor >= this.to) this.cursor = this.frm;
    this.playing = true;
    this.#lastTick = performance.now();
    this.#lastFrame = 0;
    this.#raf = requestAnimationFrame(this.#tick);
  }

  pause(): void {
    this.playing = false;
    if (this.#raf !== null) cancelAnimationFrame(this.#raf);
    this.#raf = null;
  }

  #tick = (now: number): void => {
    if (!this.playing) return;
    const dt = now - this.#lastTick;
    this.#lastTick = now;
    this.cursor = Math.min(this.to, this.cursor + dt * this.speed);
    if (now - this.#lastFrame >= FRAME_MS) {
      this.#lastFrame = now;
      this.#push();
    }
    if (this.cursor >= this.to) { this.#push(); this.pause(); return; }
    this.#raf = requestAnimationFrame(this.#tick);
  };

  /** Leave replay and hand the surfaces back to the live stream. */
  async close(): Promise<void> {
    this.pause();
    this.active = false;
    this.#tracks = [];
    this.#applied.clear();
    this.error = null;
    await devices.exitReplay();
  }
}

export const replay = new Replay();
