import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * V22/L4 — the run-detail TOOL ACTIVITY panel, pinned end to end in a real browser.
 *
 * `GET /api/v1/runs/{id}` now serves `tools_used: string[] | null` (V22/L1: migration 0010 +
 * the write path). The whole point of the column is that its three values are THREE FACTS, and the
 * page must never merge them:
 *
 *   1. NAMES  -> one chip per tool with its stored count (`×N`); no "not recorded", no "none".
 *   2. `[]`   -> "No tools were called in this run." — a MEASURED ZERO.
 *   3. `null` -> "Tool activity: not recorded for this run." — never a fabricated `[]`.
 *      A legacy body with the field ABSENT is the same fact (the column did not exist yet).
 *
 * Plus the two laws that keep the chips honest:
 *   - the writer's truncation marker (`… (+N more; capped at 50)`) is a NOTE, not a tool;
 *   - a `tools_used` that is not an array is not a recorded list: no chip is invented from a string.
 *
 * Environment independence: the one read the page makes is stubbed at the route layer with the REAL
 * envelope — the flat row object the live route serves (verified against
 * `curl http://localhost/api/v1/runs/1` at the time of writing), field for field. No store, config
 * or lab DB is required, and only a GET is intercepted; this file issues NO POST.
 */

/** One live-shaped run row; `tools_used` is the only field under test and each test sets it. */
type RunRow = {
  id: number;
  task_id: number | null;
  task_title: string | null;
  agent_type: string;
  run_type: string | null;
  status: string | null;
  attempt: number | null;
  started_at: string | null;
  ended_at: string | null;
  duration_secs: number | null;
  tokens_input: number | null;
  tokens_output: number | null;
  tokens_used: number | null;
  cost_usd: number | null;
  cost_source: string | null;
  pricing_version: string | null;
  num_turns: number | null;
  is_error: boolean | null;
  tools_used?: unknown;
};

const BASE_ROW: RunRow = {
  id: 5,
  task_id: 42,
  task_title: "Tool-using ledger task",
  agent_type: "content",
  run_type: "task",
  status: "completed",
  attempt: 1,
  started_at: "2026-10-14T09:00:00Z",
  ended_at: "2026-10-14T09:05:00Z",
  duration_secs: 300,
  tokens_input: 120,
  tokens_output: 30,
  tokens_used: 150,
  cost_usd: 0.0123,
  cost_source: "token_estimate",
  pricing_version: "2026-10-01",
  num_turns: 3,
  is_error: false,
  tools_used: null,
};

/** Serve one run row for `/api/v1/runs/{id}`; records every read so "one read" is provable. */
async function stubRunRead(page: Page, row: RunRow, reads: string[] = []): Promise<string[]> {
  await page.route("**/api/v1/runs/*", async (route: Route) => {
    const request = route.request();
    if (request.method() !== "GET") {
      await route.continue();
      return;
    }
    const url = new URL(request.url());
    // `/runs/export` is a different surface; let it through rather than serving a run.
    if (url.pathname.endsWith("/export")) {
      await route.continue();
      return;
    }
    reads.push(request.url());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(row),
    });
  });
  return reads;
}

async function openRun(page: Page, row: RunRow): Promise<string[]> {
  const reads: string[] = [];
  await stubRunRead(page, row, reads);
  await page.goto(`/runs/${row.id}`);
  await expect(page.getByTestId("run-detail-card")).toBeVisible();
  await expect(page.getByTestId("run-tool-activity")).toBeVisible();
  return reads;
}

/** The tool panel (the card that owns every chip state). */
function panel(page: Page) {
  return page.getByTestId("run-tool-activity");
}

test.describe("V22 run detail tool chips (live FE)", () => {
  test("recorded names render one chip per tool with the stored count", async ({ page }) => {
    const reads = await openRun(page, {
      ...BASE_ROW,
      // A real stored list repeats a tool — the count is the fact, not a de-duplicated guess.
      tools_used: ["web_search", "web_search", "calculator"],
    });

    // Exactly the distinct tools, never one chip per occurrence.
    await expect(panel(page).getByTestId("run-tool-chip")).toHaveCount(2);

    const search = page.getByTestId("run-tool-chip-web_search");
    await expect(search).toBeVisible();
    await expect(search).toContainText("web_search");
    await expect(search).toContainText("×2");

    const calc = page.getByTestId("run-tool-chip-calculator");
    await expect(calc).toBeVisible();
    await expect(calc).toContainText("calculator");
    await expect(calc).toContainText("×1");

    // The other two states must be absent when names are recorded.
    await expect(page.getByTestId("run-tools-not-recorded")).toHaveCount(0);
    await expect(page.getByTestId("run-tools-none")).toHaveCount(0);
    await expect(page.getByTestId("run-tools-truncated")).toHaveCount(0);

    // The panel sits on the run the route named, read exactly once.
    await expect(page.getByTestId("run-agent")).toHaveText("content");
    expect(reads).toHaveLength(1);
    expect(new URL(reads[0]).pathname).toBe("/api/v1/runs/5");
  });

  test("a recorded empty list is the measured zero, with no chip", async ({ page }) => {
    await openRun(page, { ...BASE_ROW, tools_used: [] });

    const none = page.getByTestId("run-tools-none");
    await expect(none).toBeVisible();
    await expect(none).toContainText("No tools were called in this run.");
    // `[]` is NOT "not recorded": the two sentences must not be interchangeable.
    await expect(page.getByTestId("run-tools-not-recorded")).toHaveCount(0);
    await expect(page.getByTestId("run-tool-chip")).toHaveCount(0);
    await expect(page.getByTestId("run-tool-chips")).toHaveCount(0);
  });

  test("null and a legacy absent field both say 'not recorded' and render NO chip", async ({
    page,
  }) => {
    await openRun(page, { ...BASE_ROW, tools_used: null });

    const notRecorded = page.getByTestId("run-tools-not-recorded");
    await expect(notRecorded).toBeVisible();
    await expect(notRecorded).toContainText("Tool activity: not recorded for this run.");
    // THE brief's explicit assertion: absence of a stored list must not become a chip.
    await expect(page.getByTestId("run-tool-chip")).toHaveCount(0);
    await expect(page.getByTestId("run-tools-none")).toHaveCount(0);

    // A legacy backend body (the field did not exist) is the SAME fact — not a fabricated [].
    const { tools_used: _dropped, ...legacy } = BASE_ROW;
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await openRun(page, legacy as RunRow);
    await expect(page.getByTestId("run-tools-not-recorded")).toBeVisible();
    await expect(page.getByTestId("run-tool-chip")).toHaveCount(0);
  });

  test("the truncation marker is a stated note, never a tool chip", async ({ page }) => {
    await openRun(page, {
      ...BASE_ROW,
      tools_used: ["web_search", "… (+11 more; capped at 50)"],
    });

    // Only the real tool is a chip.
    await expect(page.getByTestId("run-tool-chip")).toHaveCount(1);
    await expect(page.getByTestId("run-tool-chip-web_search")).toHaveCount(1);
    // The cap is stated where a reader can see it, in the writer's own words.
    const note = page.getByTestId("run-tools-truncated");
    await expect(note).toBeVisible();
    await expect(note).toContainText("11 more");
    await expect(note).toContainText("capped at 50");
    await expect(page.getByTestId("run-tools-none")).toHaveCount(0);
  });

  test("a non-array tools_used never becomes a chip", async ({ page }) => {
    await openRun(page, { ...BASE_ROW, tools_used: "web_search" });

    // A string is not a recorded list: the honest fallback is the null state, not one invented chip.
    await expect(page.getByTestId("run-tool-chip")).toHaveCount(0);
    await expect(page.getByTestId("run-tools-not-recorded")).toBeVisible();
    await expect(page.getByTestId("run-tools-none")).toHaveCount(0);
  });
});
