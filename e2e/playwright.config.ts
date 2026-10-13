import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  // Suite-end fixture sweep (T12): the belt to each spec's own `finally`.  Playwright runs
  // globalTeardown after the last test — also when a spec failed or timed out and never reached its
  // own cleanup — and it adds no test to the 20-test contract the campaign tracks (a sweep spec
  // ordered last would, and a filtered/`--grep`ed run could skip it entirely).  The option takes a
  // MODULE PATH only, so the hook is `_helpers`'s default export; it spawns `_sweep-fixtures.py`
  // (venv python, tag-scoped, both stores, idempotent) and throws if the sweep cannot complete.
  globalTeardown: "./_helpers.ts",
  // R-W4-2 restart guard — runs BEFORE any test, on every test-bearing invocation (including a bare
  // `npx playwright test`, the campaign's ×3 recipe; skipped for --list/--help).  It refuses to
  // start while the sweep's restart marker is present AND a backend still answers /health, because
  // that backend's cached chroma collection is stale (measured memory-search 500s — W4 §3.9 /
  // endgame V2 run 3).  Policy text: e2e/_restart-guard.sh; the same check runs in run-suite.sh's
  // pre-flight, and the sweep writes/clears the marker it reads.
  globalSetup: "./_guard.ts",
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
