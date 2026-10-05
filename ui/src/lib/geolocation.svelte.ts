// Presence reporter: while the DIDA app is open AND the user opted in on this
// device, report this device's GPS position to the API. The server ties it to
// the logged-in user (presence:<username>) and resolves the zone.
//
// Honest limitation: browsers only provide geolocation while the page is in the
// foreground — there is no reliable background geofencing on web. So this covers
// "where am I / who's home while using the app", not locked-phone arrival/leave
// automations (that needs a native tracker → the OwnTracks presence adapter).
// Within that limit we squeeze the most out of every foreground moment:
// - a forced fresh fix the instant the app returns to the foreground / regains
//   network (unlocking the phone somewhere new registers immediately, not after
//   the throttle window),
// - a sendBeacon flush of the last position when the app is backgrounded, so
//   the server's last_seen is fresh at the moment reporting stops,
// - the server rejects fixes coarser than ~500 m (a cell-tower "position" must
//   not flip anyone to "away"); the watch simply retries with a better fix.
//
// Opt-in is per-device (localStorage), default OFF — location sharing is never
// silent. The watch is throttled (don't spam the bus when stationary).

import { api } from "$lib/api";

const STORAGE_KEY = "dida.shareLocation";
const MIN_INTERVAL_MS = 90_000; // don't report more often than this…
const MIN_MOVE_M = 50; // …unless moved at least this far (zones are 100 m+)
// Re-report the position on a heartbeat even when stationary, so the server's
// last_seen stays fresh while the app is open. Without it, a motionless phone
// stops firing watchPosition and would look "stale" though the app is open —
// staleness must mean "app not reporting", not "user sitting still".
const HEARTBEAT_MS = 5 * 60_000;

// enableHighAccuracy: TRUE engages the GPS chip. With it false the browser
// serves the coarse network/Wi-Fi provider, which on home Wi-Fi resolves to the
// access point's position in Google/Apple's DB — often hundreds of metres off,
// yet reported with a confidently-small accuracy that sails past the server's
// 500 m gate and drops the person into the wrong zone (a phone at the house read
// ~700 m away, in a neighbouring zone). Presence needs ~100 m truth, so we pay
// the battery/latency for a real fix. maximumAge 0 on the forced fixes below
// forbids reusing a stale coarse cached position; the watch keeps 60 s.
const FIX_OPTS: PositionOptions = {
  enableHighAccuracy: true,
  maximumAge: 60_000,
  timeout: 30_000,
};
// Forced fixes (foreground / online / heartbeat) must be FRESH — never a cached
// coarse position from another app — so they demand maximumAge 0.
const FORCE_FIX_OPTS: PositionOptions = { ...FIX_OPTS, maximumAge: 0 };

export type GeoStatus = "off" | "starting" | "active" | "denied" | "error" | "unsupported";
type Fix = { lat: number; lon: number; accuracy: number; t: number };

function readStored(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function distM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = 6371000;
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dp = ((lat2 - lat1) * Math.PI) / 180;
  const dl = ((lon2 - lon1) * Math.PI) / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(a));
}

class GeoReporter {
  enabled = $state(readStored());
  status = $state<GeoStatus>("off");
  lastAt = $state<number | null>(null);
  zone = $state<string | null>(null); // zone the server registered us in

  #watchId: number | null = null;
  #heartbeat: ReturnType<typeof setInterval> | null = null;
  #last: Fix | null = null;
  #attempt: Fix | null = null;
  #revision = 0;

  #onVisibility = (): void => {
    // Foregrounded: force a fresh fix NOW — unlocking the phone at a new place
    // is exactly the moment a zone change happened. Backgrounded: flush the
    // last known position so last_seen is fresh when reporting stops.
    if (document.visibilityState === "visible") this.#fix();
    else this.#flush();
  };
  #onOnline = (): void => this.#fix(); // network back — report what we have

  /** Toggle sharing on this device (persisted) and start/stop accordingly. */
  setEnabled(on: boolean): void {
    this.enabled = on;
    try {
      window.localStorage.setItem(STORAGE_KEY, on ? "1" : "0");
    } catch { /* ignore */ }
    if (on) this.start();
    else this.stop();
  }

  /** Begin watching position if opted in (called on login + on enable). */
  start(): void {
    if (this.#watchId !== null || !this.enabled) return;
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      this.status = "unsupported";
      return;
    }
    this.status = "starting";
    const revision = ++this.#revision;
    this.#watchId = navigator.geolocation.watchPosition(
      (pos) => { if (revision === this.#revision) this.#onPosition(pos); },
      (err) => {
        if (revision !== this.#revision) return;
        this.status = err.code === err.PERMISSION_DENIED ? "denied" : "error";
      },
      FIX_OPTS,
    );
    this.#heartbeat = setInterval(() => this.#fix(), HEARTBEAT_MS);
    document.addEventListener("visibilitychange", this.#onVisibility);
    window.addEventListener("online", this.#onOnline);
  }

  /** Stop watching (called on logout + on disable). */
  stop(): void {
    this.#revision++;
    if (this.#watchId !== null && typeof navigator !== "undefined" && navigator.geolocation) {
      navigator.geolocation.clearWatch(this.#watchId);
    }
    this.#watchId = null;
    if (this.#heartbeat !== null) clearInterval(this.#heartbeat);
    this.#heartbeat = null;
    this.#last = null;
    this.#attempt = null;
    this.lastAt = null;
    this.zone = null;
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.#onVisibility);
      window.removeEventListener("online", this.#onOnline);
    }
    if (this.status !== "denied" && this.status !== "unsupported") this.status = "off";
  }

  /** One forced fix → report (bypasses the throttle). A single failed fix is
   *  fine — the watch keeps trying. */
  #fix(): void {
    if (this.#watchId === null) return; // not running
    const revision = this.#revision;
    navigator.geolocation.getCurrentPosition(
      (pos) => { if (revision === this.#revision) this.#onPosition(pos, true); },
      () => { /* transient */ },
      FORCE_FIX_OPTS,
    );
  }

  /** Fire-and-forget re-send of the last reported position on backgrounding.
   *  sendBeacon survives the page freeze where a fetch would be dropped; the
   *  session cookie rides along (same-origin). Stale coordinates are worse
   *  than none, so only positions younger than the heartbeat are flushed. */
  #flush(): void {
    if (!this.#last || Date.now() - this.#last.t > HEARTBEAT_MS) return;
    if (typeof navigator.sendBeacon !== "function") return;
    const body = JSON.stringify({ latitude: this.#last.lat, longitude: this.#last.lon,
      accuracy: this.#last.accuracy, tst: this.#last.t / 1000 });
    navigator.sendBeacon("/api/presence/report", new Blob([body], { type: "application/json" }));
  }

  #onPosition(pos: GeolocationPosition, force = false): void {
    const { latitude, longitude, accuracy } = pos.coords;
    const now = Date.now();
    if (!Number.isFinite(accuracy) || accuracy < 0) {
      this.status = "error";
      return;
    }
    if (!force && this.#attempt) {
      const moved = distM(this.#attempt.lat, this.#attempt.lon, latitude, longitude);
      if (now - this.#attempt.t < MIN_INTERVAL_MS && moved < MIN_MOVE_M && accuracy >= this.#attempt.accuracy) return;
    }
    const fix = { lat: latitude, lon: longitude, accuracy, t: pos.timestamp };
    const revision = this.#revision;
    this.#attempt = fix;
    api
      .reportPresence(latitude, longitude, fix.t / 1000, accuracy)
      .then((r) => {
        if (revision !== this.#revision || (this.#last && fix.t < this.#last.t)) return;
        this.status = "active";
        // A rejected coarse fix isn't an error — the watch retries; keep the
        // last REGISTERED report time/zone so the UI reflects reality.
        if (r.accepted) {
          this.#last = fix;
          this.lastAt = fix.t;
          this.zone = r.zone ?? null;
        }
      })
      .catch(() => {
        if (revision !== this.#revision || this.#attempt !== fix) return;
        this.status = "error";
      });
  }
}

export const geo = new GeoReporter();
