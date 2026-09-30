// DIDA service worker — Web Push + freshness enforcement. Still NO offline
// precache (a stale-asset cache is a silent-failure machine): the ONLY fetch it
// touches is the app-shell NAVIGATION, which it forces fresh from the network so a
// new deploy is never masked by a browser-cached HTML pinning the old build's JS.
// Hashed assets under /_app/immutable are left to the normal (immutable) HTTP
// cache. Also relays `notify` commands as system notifications while closed.
/// <reference lib="webworker" />

const sw = self as unknown as ServiceWorkerGlobalScope;

sw.addEventListener("install", () => {
  void sw.skipWaiting();
});

sw.addEventListener("activate", (event) => {
  // Take control immediately, then reload every open window so a client stuck on a
  // pre-update (browser-cached) shell picks up the fresh one — the SW updates
  // independently of the HTML cache, so this breaks the "cached shell hides the
  // deploy" deadlock without anyone clearing their cache. SW changes are rare, so
  // this reload is infrequent.
  event.waitUntil(
    (async () => {
      await sw.clients.claim();
      for (const c of await sw.clients.matchAll({ type: "window" })) {
        // @ts-expect-error navigate() exists on WindowClient
        c.navigate(c.url).catch(() => {});
      }
    })(),
  );
});

sw.addEventListener("fetch", (event) => {
  const req = event.request;
  // Only the app shell (a top-level navigation) — never assets. Force a fresh
  // network fetch (bypass the HTTP cache) so the served HTML always references the
  // current build's hashed chunks. No offline fallback (this is a live dashboard).
  if (req.mode === "navigate") {
    event.respondWith(fetch(req, { cache: "no-store" }).catch(() => fetch(req)));
  }
});

sw.addEventListener("push", (event) => {
  // Payload from the notify adapter: {title, message, image?}. A non-JSON payload
  // (e.g. a bare test string from a push service console) still shows.
  let data: { title?: string; message?: string; image?: string } = {};
  try {
    data = event.data?.json() ?? {};
  } catch {
    data = { message: event.data?.text() ?? "" };
  }
  event.waitUntil(
    sw.registration.showNotification(data.title || "DIDA", {
      body: data.message || "",
      icon: "/icon-192.png?v=20260921",
      // Android renders the badge as a monochrome mask — it has to be the bare
      // glyph on transparency, not the tile (which comes out a grey blob).
      badge: "/badge-96.png?v=20260921",
      // A camera notification carries the frame that caused it. The phone fetches
      // this itself, with no session — the api serves it from an expiring,
      // unguessable path for exactly that reason.
      ...(data.image ? { image: data.image } : {}),
    }),
  );
});

sw.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Focus an open DIDA window if there is one, otherwise open the dashboard.
  event.waitUntil(
    sw.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const win = wins[0];
      return win ? win.focus() : sw.clients.openWindow("/");
    }),
  );
});
