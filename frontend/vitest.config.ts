import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts (dev server / build) on purpose, matching
// extension/vitest.config.ts's convention -- test-only concerns (jsdom,
// setup files) don't belong in the config that also drives `vite build`.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
