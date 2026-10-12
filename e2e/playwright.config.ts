import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  timeout: 30_000,
  retries: 1,
  // 10s for assertions, not Playwright's 5s default: the FIRST visit to a data-dependent page has to
  // build its chunks, call the API and paint, and on a cold local stack that exceeded 5s (measured:
  // first attempt 5.9s -> failed, retry 2.1s -> passed, i.e. the suite was green only via `retries`).
  // This is a harness allowance for cold start, NOT a claim that the pages are fast; the retry stays as
  // a backstop so a genuinely intermittent failure is still visible as "flaky" rather than hidden.
  expect: { timeout: 10_000 },
  reporter: [["html", { outputFolder: "playwright-report" }], ["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost",
    extraHTTPHeaders: {
      "X-API-Key": process.env.API_KEY ?? "dev-key",
    },
    screenshot: "only-on-failure",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
