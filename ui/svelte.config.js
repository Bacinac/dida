import nodeAdapter from "@sveltejs/adapter-node";
import staticAdapter from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// DIDA_DEMO=1 builds the public, backend-less demo: the same app, emitted as a
// static SPA (every route falls back to index.html and boots client-side) with
// the demo network shim injected. Normal builds are untouched — adapter-node,
// no shim.
const demo = process.env.DIDA_DEMO === "1";

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    adapter: demo ? staticAdapter({ fallback: "index.html", strict: false }) : nodeAdapter(),
    // The demo builds from a GENERATED template (build-demo.sh writes it from
    // app.html with the shim filled in). Editing app.html in place instead means
    // that a build which dies — or two deploys overlapping on one checkout —
    // leaves the shim in a tracked source file, where the next commit would ship
    // it to production.
    files: { appTemplate: demo ? "src/app.demo.html" : "src/app.html" },
  },
};
