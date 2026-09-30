// Production web server. In dev, Vite's dev server serves the SPA and proxies
// /api → the FastAPI service (see vite.config.ts). In production we ship a real
// build (SvelteKit adapter-node) and this thin Node server takes over BOTH jobs
// with exact parity to the Vite proxy:
//   * everything → the built adapter-node handler (the pre-bundled SPA + assets)
//   * /api/*      → the API service (DIDA_API_URL), rewriting off the /api prefix,
//                   forwarding the client IP (xfwd), and upgrading WebSockets.
//
// The app is a pure SPA (ssr:false) with no +server routes, so the handler just
// serves static files + the shell; all data rides /api. The ONE proxy rule
// covers REST, the live-events WebSocket (/api/ws) and the camera stream proxy
// (/api/camera/* — the go2rtc/Frigate basic-auth injection is server-side in the
// API, so it needs no special handling here). Mirrors vite.config.ts:server.proxy.
import http from "node:http";
import { readFileSync } from "node:fs";
import httpProxy from "http-proxy";
import { handler } from "./build/handler.js";

// Android App Links verification (the companion app claims our https URLs, so
// the setup QR opens the app when installed). Served explicitly: static-file
// servers commonly skip dotfile paths, and a silently missing assetlinks.json
// would demote every QR scan to the browser with no error anywhere. Loaded at
// boot — a broken build should fail loud here, not 404 quietly forever.
const ASSETLINKS = readFileSync(
  new URL("./build/client/.well-known/assetlinks.json", import.meta.url)
);

const API_TARGET = process.env.DIDA_API_URL || "http://api:8090";
const PORT = Number(process.env.PORT) || 5173;

// xfwd:true forwards X-Forwarded-For so the API's per-IP login rate limiter
// buckets each real client, not this proxy's address (same reason vite sets it).
const proxy = httpProxy.createProxyServer({
  target: API_TARGET,
  changeOrigin: true,
  xfwd: true,
  ws: true,
});

// A dead/booting API must not crash the web server — answer the one request and
// let the browser retry, exactly as the dev proxy degrades.
proxy.on("error", (err, _req, res) => {
  console.error(`[ui] /api proxy error: ${err.message}`);
  if (res && "writeHead" in res && !res.headersSent) {
    res.writeHead(502, { "content-type": "text/plain" });
    res.end("api unavailable");
  } else if (res && "destroy" in res) {
    res.destroy(); // a socket (WebSocket upgrade) — just drop it
  }
});

// Strip the /api prefix before proxying: /api/ws → /ws, /api/camera/x → /camera/x
// (vite.config.ts does the same rewrite). Bare /api → / .
const stripApi = (url) => url.replace(/^\/api(?=\/|$)/, "") || "/";

const server = http.createServer((req, res) => {
  if (req.url === "/api" || req.url.startsWith("/api/")) {
    req.url = stripApi(req.url);
    proxy.web(req, res);
  } else if (req.url === "/.well-known/assetlinks.json") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(ASSETLINKS);
  } else {
    // Never let the browser cache the SPA shell (or other non-hashed responses):
    // a cached shell keeps referencing the PREVIOUS build's JS, so a new deploy
    // stays invisible until a hard reload. Hashed assets under /_app/immutable/
    // keep the adapter's own long-lived immutable caching (it sets that itself).
    if (!req.url.startsWith("/_app/immutable/")) res.setHeader("cache-control", "no-cache");
    handler(req, res);
  }
});

// WebSocket upgrades (the live engine-events socket at /api/ws) go through the
// same proxy; anything else upgrading is not ours, so close it.
server.on("upgrade", (req, socket, head) => {
  if (req.url === "/api" || req.url.startsWith("/api/")) {
    req.url = stripApi(req.url);
    // changeOrigin rewrites Host, and http-proxy forwards the browser's only on plain
    // requests; the API checks the socket's Origin against it (vite.config.ts too).
    req.headers["x-forwarded-host"] = req.headers.host;
    proxy.ws(req, socket, head);
  } else {
    socket.destroy();
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`dida ui (prod) listening on :${PORT} — /api → ${API_TARGET}`);
});
