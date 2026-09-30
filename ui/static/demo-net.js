/* Demo mode: the real DIDA frontend with its network layer swapped out.
 *
 * Loaded before the app boots (see app.html) and active only when the build
 * was made with DIDA_DEMO=1. It patches the two seams the app uses — global
 * fetch and WebSocket — so:
 *   • GET    → answered from a recorded snapshot of a real (anonymised) house
 *   • writes → accepted, applied to the in-memory snapshot, never persisted
 *   • WS     → connects and stays quiet (no live pushes to replay)
 * Everything the UI does is therefore real: it renders, edits, drags and
 * re-reads its own writes. A reload resets the house.
 */
(function () {
  "use strict";

  const BASE = "/api";
  let db = null; // path -> recorded body (mutated by writes during the session)

  const ready = fetch("/demo-fixtures.json")
    .then((r) => r.json())
    .then((j) => (db = j))
    .catch(() => (db = {}));

  const jsonRes = (body, status = 200) =>
    new Response(JSON.stringify(body === undefined ? null : body), {
      status,
      headers: { "content-type": "application/json" },
    });

  // Fixtures are keyed by path (+query when it was part of the recording).
  // Fall back to the bare path so `?since=…`-style params still resolve.
  function lookup(path) {
    if (path in db) return db[path];
    const bare = path.split("?")[0];
    if (bare in db) return db[bare];
    // Time-ranged endpoints (energy, history) carry a window in the query, so
    // the exact key never recurs. Fall back to any recording of the same path:
    // the numbers are a day old rather than absent, which is the honest trade
    // for a snapshot demo — an empty chart would just look broken.
    for (const k of Object.keys(db)) {
      if (k.split("?")[0] === bare) return db[k];
    }
    return undefined;
  }

  // A write updates the snapshot where the shape makes that obvious (a
  // collection of objects with an id, or a settings blob), so the UI reads
  // its own change back instead of snapping to the old value.
  function applyWrite(path, method, body) {
    const bare = path.split("?")[0];
    const coll = bare.replace(/\/[^/]+$/, "");
    const idPart = bare.slice(coll.length + 1);

    if (method === "DELETE" && Array.isArray(db[coll])) {
      db[coll] = db[coll].filter((r) => String(r.id ?? r.key ?? "") !== idPart);
      return { ok: true };
    }
    if (Array.isArray(db[bare]) && method === "POST") {
      const row = { id: Date.now(), ...(body || {}) };
      db[bare] = [...db[bare], row];
      return row;
    }
    if (Array.isArray(db[coll]) && (method === "PUT" || method === "PATCH")) {
      let out = body;
      db[coll] = db[coll].map((r) => {
        if (String(r.id ?? r.key ?? "") !== idPart) return r;
        out = { ...r, ...(body || {}) };
        return out;
      });
      return out;
    }
    if (bare in db && body && typeof body === "object" && !Array.isArray(db[bare])) {
      db[bare] = { ...db[bare], ...body };
      return db[bare];
    }
    return body ?? { ok: true };
  }

  const realFetch = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input?.url ?? String(input);
    const method = (init?.method || (typeof input !== "string" && input?.method) || "GET").toUpperCase();

    if (!url.includes(BASE + "/")) return realFetch(input, init);
    await ready;

    const path = url.slice(url.indexOf(BASE) + BASE.length) || "/";

    // Identity is synthesised, never recorded: whether a recording happens to
    // capture /auth/me depends on cache timing, and without a user the app
    // renders an empty shell — too fragile to leave to chance.
    const DEMO_USER = {
      id: "1", username: "demo", role: "admin", allowed_pages: null,
      can_control: true, theme: "dark", locale: "en",
    };
    if (path.startsWith("/auth/me")) return jsonRes(lookup("/auth/me") ?? DEMO_USER);
    if (path.startsWith("/auth/login")) return jsonRes(lookup("/auth/me") ?? DEMO_USER);
    if (path.startsWith("/auth/logout")) return jsonRes({ ok: true });
    if (path.startsWith("/version")) {
      return jsonRes(lookup("/version") ?? { version: "demo", branch: "demo", sha: "demo" });
    }

    // The header's API light asks here. It is answering truthfully: the
    // demo's data layer IS up — the banner is what says there's no backend.
    if (path.startsWith("/healthz")) return jsonRes({ ok: true });

    // Binary assets (floor plans, camera stills, the APK) are copied into the
    // build as-is, so those requests go to the static server. Scope this to the
    // IMAGE paths only: a blanket /camera/ passthrough also caught JSON calls
    // like /camera/<id>/events, which then received the SPA's HTML and blew up
    // with "Unexpected token '<'".
    if (/^\/floorplan\//.test(path)
        || /^\/camera\/[^/]+\/snapshot/.test(path)
        || /^\/app\//.test(path)) {
      return realFetch(input, init);
    }

    // The camera archive is deliberately absent. Its events are detection CROPS
    // of whoever walked past — the one thing on this house that can't be
    // anonymised, so it is never recorded into the fixtures. Answer with the
    // real endpoint's shape so the dialog draws its "no events" state; the
    // generic empty-array fallback below has the wrong shape here and left the
    // filmstrip spinning forever.
    if (/^\/camera\/[^/]+\/events/.test(path)) return jsonRes({ events: [] });

    // The assistant is the one surface with nothing to fake: it needs a live house
    // and a live model. Answer in its own SSE shape and say so, rather than letting
    // the generic write path echo the request back as a broken stream — the header's
    // explain-this-page button reaches it from every page.
    if (path.startsWith("/assistant")) {
      let locale = "hr";
      try { locale = localStorage.getItem("locale") === "en" ? "en" : "hr"; } catch { /* no storage */ }
      const reply = locale === "en"
        ? "This is the public demo — a recording of a real house with no backend behind it. "
          + "The assistant needs the live system: in the real app it explains any page, reads "
          + "device states and can act on them."
        : "Ovo je javni demo — snimka prave kuće bez pozadinskog sustava. "
          + "Asistentu treba živi sustav: u pravoj aplikaciji objašnjava svaku stranicu, "
          + "čita stanja uređaja i može njima upravljati.";
      const frame = "data: " + JSON.stringify({ type: "done", reply, actions: [] }) + "\n\n";
      return new Response(frame, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }

    // Replay reads the history firehose, which the fixtures don't carry (they are
    // one recorded instant, not a window). Answer in the endpoint's own shape with
    // no tracks — the same choice as the camera archive above — so the plan draws
    // its "nothing to replay" state instead of the generic write path echoing the
    // request back as a bundle the scrubber then tries to read.
    if (path.startsWith("/history/replay")) {
      return jsonRes({ frm: 0, to: 0, bucket_seconds: 0, truncated: false, tracks: [] });
    }

    if (method === "GET") {
      const hit = lookup(path);
      if (hit === undefined) {
        // Loud in the console so a thin page is traceable to a missing
        // recording rather than looking like an app bug.
        console.warn("[demo] no fixture for", path);
        return jsonRes([], 200);
      }
      return jsonRes(hit);
    }

    let body;
    try {
      body = init?.body ? JSON.parse(init.body) : undefined;
    } catch {
      body = undefined;
    }
    return jsonRes(applyWrite(path, method, body));
  };

  // The app opens a socket for live state. Give it one that connects and then
  // says nothing — no reconnect storm, no fabricated events.
  class DemoSocket extends EventTarget {
    constructor() {
      super();
      this.readyState = 1;
      this.url = "demo://ws";
      setTimeout(() => this.dispatchEvent(new Event("open")), 0);
    }
    send() {}
    close() {
      this.readyState = 3;
      this.dispatchEvent(new Event("close"));
    }
  }
  for (const p of ["onopen", "onmessage", "onerror", "onclose"]) {
    Object.defineProperty(DemoSocket.prototype, p, {
      set(fn) {
        this.addEventListener(p.slice(2), fn);
      },
      configurable: true,
    });
  }
  DemoSocket.OPEN = 1;
  DemoSocket.CLOSED = 3;
  window.WebSocket = DemoSocket;

  // Say plainly what this is. Deleting something here looks exactly like
  // deleting it for real, and a visitor (or the author) must never have to
  // wonder whether they just changed a live house.
  function banner() {
    const el = document.createElement("div");
    el.textContent = "DEMO · no backend — edits, drags and deletes are local and vanish on reload";
    Object.assign(el.style, {
      position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "2147483647",
      padding: "6px 12px", textAlign: "center", pointerEvents: "none",
      font: "500 12px/1.4 system-ui, sans-serif", letterSpacing: ".01em",
      color: "#062c33", background: "#01bde4", opacity: "0.94",
    });
    document.body.appendChild(el);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", banner);
  } else {
    banner();
  }
})();
