import nodeAdapter from "@sveltejs/adapter-node";
import staticAdapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const publicHosts = [process.env.DIDA_APP_URL ?? "", ...(process.env.DIDA_CORS_ORIGINS ?? "").split(",")]
  .map((origin) => URL.parse(origin.trim())?.hostname)
  .filter((host): host is string => !!host);

// DIDA_DEMO=1 builds the public, backend-less demo: the same app, emitted as a
// static SPA (every route falls back to index.html and boots client-side) with
// the demo network shim injected. Normal builds are untouched — adapter-node,
// no shim.
const demo = process.env.DIDA_DEMO === "1";

// The browser always talks to SvelteKit; /api/* is proxied to the FastAPI
// service (dev) so there is no CORS dance and the WebSocket upgrade works.
export default defineConfig({
  plugins: [
    tailwindcss(),
    sveltekit({
      preprocess: vitePreprocess(),
      adapter: demo ? staticAdapter({ fallback: "index.html", strict: false }) : nodeAdapter(),
      // The demo builds from a GENERATED template (build-demo.sh writes it from
      // app.html with the shim filled in). Editing app.html in place instead means
      // that a build which dies — or two deploys overlapping on one checkout —
      // leaves the shim in a tracked source file, where the next commit would ship
      // it to production.
      files: { appTemplate: demo ? "src/app.demo.html" : "src/app.html" },
      // push.svelte.ts is the one registrar. Kit's own registration is type
      // module while ours is classic in production, and two registrations of one
      // script with different types replace each other on every load — the
      // worker's activate then reloads every open tab, forever.
      serviceWorker: { register: false },
    }),
  ],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // The source (./ui/src) is a plain Docker bind of a local ext4 path on this
    // LXC, so native inotify events propagate — no polling needed (that myth is
    // only true for virtiofs/9p VM shares or NFS). Native = instant HMR + no
    // idle poll load, and one fewer thing to wedge the dev server under load.
    watch: { ignored: ["**/.svelte-kit/**", "**/.git/**", "**/node_modules/**"] },
    allowedHosts: [...publicHosts, "localhost", "127.0.0.1"],
    proxy: {
      "/api": {
        target: process.env.DIDA_API_URL || "http://api:8090",
        changeOrigin: true,
        // Forward the browser's address so the API's per-IP login rate limiter
        // buckets each client separately instead of collapsing everyone onto
        // this proxy's IP (which would let 5 bad attempts lock out the house).
        xfwd: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        ws: true,
        configure: (proxy) =>
          proxy.on("proxyReqWs", (proxyReq, req) => proxyReq.setHeader("x-forwarded-host", req.headers.host ?? "")),
      },
    },
  },
});
