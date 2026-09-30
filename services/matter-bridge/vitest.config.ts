import { defineConfig } from "vitest/config";

// Unit tests over the pure DIDA <-> Matter conversions (src/mapping.ts) and the
// broker seal (src/broker.ts). index.ts opens NATS and the Matter server at import,
// so it is deliberately not under test here — the mapping is what silently
// mis-actuates a device when it's wrong.
export default defineConfig({
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
