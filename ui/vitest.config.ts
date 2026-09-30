import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

// Test config, deliberately NOT the sveltekit() plugin: these are unit tests over
// the runes stores ($state in *.svelte.ts), so they need Svelte compilation but
// none of kit's SSR/routing machinery. `$lib` is aliased by hand for the same
// reason (kit normally provides it).
export default defineConfig({
  plugins: [svelte({ compilerOptions: { dev: true } })],
  resolve: {
    alias: { $lib: fileURLToPath(new URL("./src/lib", import.meta.url)) },
    // The runes stores are imported as `$lib/store.svelte` — resolve that to the
    // .svelte.ts source (vite.config deliberately omits this for the app build).
    extensions: [".svelte.ts", ".ts", ".js"],
    conditions: ["browser"],
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    restoreMocks: true,
  },
});
