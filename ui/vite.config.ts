import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const publicHosts = [process.env.DIDA_PUBLIC_URL ?? "", ...(process.env.DIDA_CORS_ORIGINS ?? "").split(",")]
  .map((origin) => URL.parse(origin.trim())?.hostname)
  .filter((host): host is string => !!host);

// The browser always talks to SvelteKit; /api/* is proxied to the FastAPI
// service (dev) so there is no CORS dance and the WebSocket upgrade works.
export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  // No custom resolve.extensions (matches BABA): runes modules are imported with
  // an explicit `.svelte` (resolved to *.svelte.ts via the default .ts), and the
  // i18n dir has a plain index.ts. Adding `.svelte.ts` here would shadow any
  // directory on a bare `$lib/X` import — a footgun that broke the ui/ barrel.
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
