import { defineConfig, devices } from "@playwright/test";

// `make e2e-ui` seeds a course (see scripts/e2e.py --seed-only
// --keep-running) with the backend fixed on 127.0.0.1:8730 -- exactly the
// port frontend/vite.config.ts's dev-server proxy already targets, so this
// config only has to boot the Vite dev server itself; the backend it talks
// to is someone else's problem (the shell script driving `make e2e-ui`).
//
// Chromium only, headless (Playwright's default) -- no other project is
// registered, so there's nothing else to opt out of.
//
// "localhost", not "127.0.0.1": Vite's dev server (with no --host) binds
// only the IPv6 loopback ([::1]) here, not 127.0.0.1 -- "localhost"
// resolves to whichever loopback address actually has a listener.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
