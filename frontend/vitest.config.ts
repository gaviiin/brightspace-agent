import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

// Separate from vite.config.ts (dev server / build) on purpose, matching
// extension/vitest.config.ts's convention -- test-only concerns (jsdom,
// setup files) don't belong in the config that also drives `vite build`.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // frontend/e2e/*.spec.ts are Playwright tests (see playwright.config.ts
    // + `make e2e-ui`), not vitest ones -- vitest's default include glob
    // would otherwise happily "discover" and fail to run them (they import
    // @playwright/test, not vitest).
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
