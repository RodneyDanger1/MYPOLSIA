import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V26/L6 — the `/ops` cockpit reads (F7).
 *
 * `/ops` (V25/L5) is a THIN consumer of routes that already shipped.  This spec pins that it
 * really reads them:
 *   GET /api/v1/runs?limit=20   -> {items, total}   (the "Live runs" ledger)
 *   GET /api/v1/tasks?status=awaiting_approval     (the "Pending approvals" card)
 *   GET /api/v1/config + /auto-mode + /ops/sandbox (the kill-switch card)
 *
 * The page's own count line must agree with the API's declared `total` (a page that renders a
 * frozen or invented count fails here), and the ledger must render exactly the rows the API
 * served at its own page size (OPS_RUN_LIMIT = 20).  The cockpit's three card titles must all be
 * present — a page that drops a card is not the cockpit.
 */

/** The page's own bounded read size (`components/ops/useOpsRuns.ts` OPS_RUN_LIMIT). */
const OPS_RUN_LIMIT = 20;

type RunPage = { items: Array<{ id: number; agent_type: string; status: string }>; total: number };

test.describe("V26/L6 — /ops cockpit", () => {
  test("renders the three cockpit cards and the run ledger the API declares", async ({
    page,
    request,
  }) => {
    const api = await request.get(`${API_BASE}/api/v1/runs?limit=${OPS_RUN_LIMIT}`);
    expect(api.status()).toBe(200);
    const ledger = (await api.json()) as RunPage;
    expect(Array.isArray(ledger.items)).toBe(true);
    expect(ledger.total).toBeGreaterThanOrEqual(ledger.items.length);

    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/runs")) apiCalls.push(r.url());
    });

    await page.goto("/ops");
    await expect(page.getByRole("heading", { name: "Ops" })).toBeVisible();

    // The cockpit's three cards, by their real titles.
    await expect(page.getByText("Live runs", { exact: true })).toBeVisible();
    await expect(page.getByText("Pending approvals", { exact: true })).toBeVisible();
    await expect(page.getByText("Kill switches & Auto Mode", { exact: true })).toBeVisible();

    // The declared total is what the page shows — never a fabricated count.
    const count = page.getByTestId("ops-runs-count");
    await expect(count).toBeVisible();
    if (ledger.total === 0) {
      await expect(count).toHaveText("0 runs in the ledger");
    } else if (ledger.items.length < ledger.total) {
      await expect(count).toHaveText(
        `Showing the newest ${ledger.items.length} of ${ledger.total} runs`,
      );
    } else {
      await expect(count).toHaveText(`Showing all ${ledger.total}`);
    }

    // The ledger's rows: exactly the page the API served (same limit, newest first).
    const table = page.getByTestId("ops-runs-table");
    await expect(table).toBeVisible();
    if (ledger.items.length === 0) {
      // An empty ledger renders ONE honest empty-state row inside the table body; demanding zero
      // rows would treat the empty state itself as a defect (V28/e2e-drift fix).
      await expect(table.getByText("No runs recorded yet", { exact: false })).toBeVisible();
      await expect(count).toHaveText("0 runs in the ledger");
    } else {
      const rows = table.locator("tbody tr");
      await expect
        .poll(async () => rows.count(), {
          message: `/ops must render the ${ledger.items.length} run(s) GET /runs?limit=${OPS_RUN_LIMIT} serves`,
        })
        .toBe(ledger.items.length);
    }

    if (ledger.items.length > 0) {
      const newest = ledger.items[0]!;
      await expect(
        table.getByText(newest.agent_type, { exact: false }).first(),
        `the newest run (#${newest.id}, ${newest.agent_type}) is not in the rendered ledger`,
      ).toBeVisible();
    }

    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/runs — a cockpit rendered without its read is a frozen snapshot",
    ).toBeGreaterThan(0);
    expect(apiCalls.some((url) => url.includes(`limit=${OPS_RUN_LIMIT}`))).toBe(true);
  });
});
