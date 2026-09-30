// Web Push opt-in for THIS browser/device: register the service worker,
// subscribe against the server's VAPID key and hand the subscription to the
// API (tied to the logged-in user). The notify adapter then delivers `notify`
// commands here as system notifications — arriving even when the app is closed.
//
// Opt-in is per-device, expressed by the subscription itself (no localStorage
// flag to drift out of sync): enabled = the browser's subscription is stored for
// the logged-in user. A browser holds one subscription whoever is logged in, so
// one left by another user reads "foreign", not "active". Requires a secure
// context (HTTPS / localhost); on iOS the app must be installed to the home screen.

import { dev } from "$app/environment";
import { api } from "$lib/api";

export type PushStatus =
  | "off" | "pending" | "active" | "foreign" | "denied" | "insecure" | "unsupported" | "error";

function b64ToU8(b64url: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (b64url.length % 4)) % 4);
  const raw = atob((b64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

class PushOptIn {
  status = $state<PushStatus>("off");
  busy = $state(false);

  #reg: ServiceWorkerRegistration | null = null;

  /** Register the SW + read back the current subscription. Idempotent; called
   *  on app start so an earlier opt-in is reflected (and the SW stays fresh). */
  async init(): Promise<void> {
    if (typeof window === "undefined") return;
    if (!window.isSecureContext) {
      this.status = "insecure"; // plain-HTTP LAN access — no SW, no push
      return;
    }
    if (!("serviceWorker" in navigator) || !("Notification" in window)) {
      this.status = "unsupported";
      return;
    }
    try {
      // Dev serves the worker as an ES module; the production build is classic.
      this.#reg = await navigator.serviceWorker.register("/service-worker.js", {
        type: dev ? "module" : "classic",
      });
      if (!this.#reg.pushManager) {
        this.status = "unsupported"; // e.g. iOS Safari tab (not installed as PWA)
        return;
      }
      const sub = await this.#reg.pushManager.getSubscription();
      const owner = sub ? (await api.pushOwner(sub.endpoint)).owner : "none";
      if (owner === "self") this.status = "active";
      else if (owner === "other") this.status = "foreign";
      else this.status = Notification.permission === "denied" ? "denied" : "off";
    } catch {
      this.status = "error";
    }
  }

  async enable(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.status = "pending";
    try {
      if (!this.#reg) await this.init();
      const reg = this.#reg;
      if (!reg?.pushManager) return; // init already set the status
      // Must run inside the user's toggle gesture (iOS requirement).
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        this.status = "denied";
        return;
      }
      const { key } = await api.pushVapidKey();
      const opts = { userVisibleOnly: true, applicationServerKey: b64ToU8(key) };
      let sub = await reg.pushManager.subscribe(opts);
      if ((await this.#store(sub)) === "foreign") {
        // The browser still holds the previous user's endpoint, which stays theirs
        // on the server; only a fresh subscription can be stored for this user.
        await sub.unsubscribe();
        sub = await reg.pushManager.subscribe(opts);
        if ((await this.#store(sub)) === "foreign") throw new Error("fresh endpoint held by another user");
      }
      this.status = "active";
    } catch {
      this.status = "error";
    } finally {
      this.busy = false;
    }
  }

  async #store(sub: PushSubscription): Promise<"stored" | "foreign"> {
    const j = sub.toJSON();
    if (!j.keys?.p256dh || !j.keys?.auth) throw new Error("subscription without keys");
    return api.pushSubscribe({ endpoint: sub.endpoint, keys: { p256dh: j.keys.p256dh, auth: j.keys.auth } });
  }

  async disable(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const sub = await this.#reg?.pushManager.getSubscription();
      if (sub) {
        // Server first: if the DELETE fails we haven't half-unsubscribed the
        // browser while the server keeps pushing at a live endpoint.
        await api.pushUnsubscribe(sub.endpoint);
        await sub.unsubscribe();
      }
      this.status = "off";
    } catch {
      this.status = "error";
    } finally {
      this.busy = false;
    }
  }

  /** Send a real test notification through the full bus → adapter path. */
  async test(message: string): Promise<void> {
    await api.pushTest(message);
  }
}

export const push = new PushOptIn();
