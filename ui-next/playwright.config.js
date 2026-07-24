import { defineConfig } from "@playwright/test";

// FE audit config. Starts the Vite dev server, runs the click-audit against it
// (mock provider = deterministic, no backend). AUDIT_BASE overrides the URL to
// audit an already-running server or a deployed build.
const useExternal = !!process.env.AUDIT_BASE;

export default defineConfig({
  testDir: "./tests",
  timeout: 120000,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: process.env.AUDIT_BASE || "http://localhost:5173",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: useExternal
    ? undefined
    : {
        command: "npm run dev",
        url: "http://localhost:5173",
        reuseExistingServer: true,
        timeout: 60000,
      },
});
