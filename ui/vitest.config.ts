import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

// Test config, deliberately NOT the sveltekit() plugin: these are unit tests over
// the runes stores ($state in *.svelte.ts), so they need Svelte compilation but
// none of kit's SSR/routing machinery. `#lib/*` resolves through package.json
// `imports`, not through kit.
export default defineConfig({
  plugins: [svelte({ compilerOptions: { dev: true } })],
  resolve: {
    conditions: ["browser"],
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    restoreMocks: true,
  },
});
