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
  // Serial: every spec that seeds fixtures (social posts, prospects, ad campaigns) writes into the ONE
  // shared dev SQLite file the backend serves from, and the /tasks spec drives a write through the real
  // trigger endpoint.  With parallel workers a file's "empty state" read can race another file's
  // just-seeded row, and a 4-core host already shared with other agents makes that worse, not better.
  // One worker keeps each file's fixture lifecycle honest; the retries:1 backstop above stays.
  workers: 1,
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
