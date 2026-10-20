import { test, expect } from "@playwright/test";
import { API_BASE, LAB_DB_SKIP_REASON, PREFIX, labDbAvailable, sqliteRun } from "./_helpers";

async function shownCount(page: import("@playwright/test").Page): Promise<number> {
  // Wave U2 (coder F2 rebuild): the retired `Outreach (N)` heading count moved to a count line
  // that measures THIS read ("N prospects · showing the first 100" when capped — old -> new in
  // the wave receipt). The pin is the same: an integer count the page itself rendered.
  const count = page.getByTestId("outreach-count");
  await expect(count).toBeVisible();
  const match = (await count.textContent())?.match(/^(\d+)/);
  expect(match, "the count line must render an integer prospect count").toBeTruthy();
  return Number(match![1]);
}

test.describe("Outreach page", () => {
  test("heading count and table agree with GET /api/v1/outreach/prospects", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/outreach/prospects?limit=100`);
    expect(api.status()).toBe(200);
    const prospects = await api.json();

    // The page must READ — the frozen-snapshot defect rendered an empty count with NO api call at all.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/outreach/prospects")) apiCalls.push(r.url());
    });

    await page.goto("/outreach");
    const shown = await shownCount(page);
    // V27 added DELETE /api/v1/prospects/{id}, so the table can SHRINK between the API read above
    // and the page's own read. Compare against a snapshot taken AFTER the page rendered (the old
    // "rows are only ever added" premise has been false since V27; V28/L4 D2).
    const afterResponse = await request.get(`${API_BASE}/api/v1/outreach/prospects?limit=100`);
    expect(afterResponse.status(), await afterResponse.text()).toBe(200);
    const servedAfter = ((await afterResponse.json()) as unknown[]).length;
    expect(shown).toBeGreaterThanOrEqual(Math.min(prospects.length, servedAfter));

    // Wave U2 (coder F2 rebuild): rows are list items with `data-testid="prospect-row"` (was
    // tbody tr — old -> new in the wave receipt). Same pin: the page renders what the API serves.
    const rows = page.locator('[data-testid="prospect-row"]');
    if (shown === 0) {
      await expect(page.getByText("No prospects yet.")).toBeVisible();
      await expect(rows).toHaveCount(0);
    } else {
      // poll, don't sample: the page may still be fetching/rendering when the branch is chosen
      await expect
        .poll(async () => rows.count(), {
          message: `/outreach must render at least ${shown} row(s) for a heading of "${shown}"`,
        })
        .toBeGreaterThanOrEqual(shown);
      if (prospects.length > 0) {
        await expect(page.getByText(prospects[0].email, { exact: true })).toBeVisible();
      }
    }
    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/outreach/prospects — an empty/static render without " +
        "a read is the frozen-snapshot defect",
    ).toBeGreaterThan(0);
  });

  test("renders a prospect row's name, company and status (seeded fixture)", async ({ page, request }) => {
    // Fixture writes go straight into the docker-free lab's SQLite file; the CI stack is Postgres
    // (E2E_SQLITE unset, no repo-local lab DB) — skip BY NAME there, never a raw open error.
    test.skip(!labDbAvailable(), LAB_DB_SKIP_REASON);
    // No write API exists for prospects and the broker is down; seed one obviously-named row into the
    // same SQLite file the backend serves from, then assert it end-to-end through the page + API.
    const stamp = Date.now();
    const email = `e2e-fixture-${stamp}@example.test`;
    try {
      sqliteRun(
        `INSERT INTO prospects (email, first_name, last_name, company, source, status, created_at)
         VALUES (?, 'Fixture', 'Row', 'E2E Fixtures Co', 'e2e', 'contacted', strftime('%Y-%m-%d %H:%M:%S','now'))`,
        [email],
      );

      await page.goto("/outreach");
      const shown = await shownCount(page);
      expect(shown).toBeGreaterThanOrEqual(1);
      await expect(page.getByText("No prospects yet.")).toHaveCount(0);

      const row = page.locator('[data-testid="prospect-row"]').filter({ hasText: email });
      await expect(
        row,
        `GET /api/v1/outreach/prospects serves ${email} but /outreach never renders its row — ` +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toHaveCount(1);
      await expect(row).toContainText("Fixture");
      await expect(row).toContainText("E2E Fixtures Co");
      await expect(row).toContainText("contacted");

      // Cross-check through the API as well: the row is served, not just painted from memory.
      const api = await request.get(`${API_BASE}/api/v1/outreach/prospects?limit=100`);
      const prospects = await api.json();
      expect(prospects.some((p: { email: string }) => p.email === email)).toBe(true);
    } finally {
      const del = sqliteRun("DELETE FROM prospects WHERE email LIKE 'e2e-fixture-%'");
      expect(del.changes, "fixture prospect row must be removable").toBeGreaterThanOrEqual(1);
    }
  });
});
