import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V7/S5 — the /usage ledger page (V7-T15): page 200, the ledger table present, the export
 * affordance present.
 *
 * V10/H2: the fixme mark is REMOVED and this spec runs against the live stack (the page and the
 * runs contract it reads both answered live in the V10 lab round; assertions re-verified).
 *
 * Every expectation is derived from the SAME API body the page reads — GET /api/v1/runs serves
 * `{items, total}` with `total` the full filtered count, and the page's declared-total footer
 * must agree with it: page 1 speaks "the first N of M" when truncated, or "all N" when the whole
 * ledger fits one page. The export's download mechanics are pinned in unit tests
 * (u7-usage.test.tsx); the lab only asserts the affordance exists, so no file lands during a run.
 */

type RunsBody = {
  items: { id: number; cost_usd: number | null }[];
  total: number;
};

test.describe("Usage ledger page", () => {
  test(
    "the ledger renders GET /runs with a truthful declared total and an export affordance (V10 lab)",
    async ({ page, request }) => {
      const api = await request.get(`${API_BASE}/api/v1/runs?limit=50&offset=0`);
      expect(api.status()).toBe(200);
      const body = (await api.json()) as RunsBody;

      // The page must do its OWN read: watch the wire, because a render-without-fetch is not a
      // ledger — it is the stale-snapshot defect wearing a table.
      const apiCalls: string[] = [];
      page.on("request", (r) => {
        if (r.url().includes("/api/v1/runs")) apiCalls.push(r.url());
      });

      const response = await page.goto("/usage");
      expect(response?.status()).toBe(200);
      await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();

      const ledger = page.getByTestId("usage-ledger");
      await expect(ledger).toBeVisible();
      await expect(ledger.locator("table")).toBeVisible();
      await expect(page.getByTestId("usage-export")).toBeVisible();

      const shown = body.items.length;
      if (body.total === 0) {
        await expect(ledger.getByText(/No runs recorded yet/)).toBeVisible();
        await expect(page.getByTestId("usage-count")).toHaveText("0 runs in the ledger");
      } else {
        await expect(ledger.locator("tbody tr")).toHaveCount(shown);
        const expected =
          shown === body.total
            ? `Showing all ${body.total}`
            : `Showing the first ${shown} of ${body.total}`;
        await expect(page.getByTestId("usage-count")).toHaveText(expected);
      }

      expect(
        apiCalls.length,
        "the page must actually CALL GET /api/v1/runs — a static render is not a ledger",
      ).toBeGreaterThan(0);
    },
  );
});
