import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/** Mirrors the page's own formatCents (en-US, 2 decimals, prefixed with $). */
function fmtUsd(cents: number): string {
  return "$" + (cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

test.describe("Finance page", () => {
  test("the six metric cards show the values GET /api/v1/finance/summary reports", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/finance/summary`);
    expect(api.status()).toBe(200);
    const s = await api.json();

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    const expectCard = async (title: string, value: string) => {
      const card = page
        .locator("main div.bg-gray-800")
        .filter({ has: page.getByText(title, { exact: true }) });
      await expect(card, `card "${title}" must exist exactly once`).toHaveCount(1);
      // Value is computed from the API response above — a page that renders a stale snapshot (or an
      // em-dash because its fetch failed) fails here.
      await expect(card.getByText(value, { exact: true })).toBeVisible();
    };

    await expectCard("MRR", fmtUsd(s.mrr_cents));
    await expectCard("ARR", fmtUsd(s.arr_cents));
    await expectCard("Active Subscribers", String(s.active_subscribers));
    await expectCard("Stripe Balance", fmtUsd(s.stripe_balance_cents));
    await expectCard("Total Ad Spend", `$${s.total_ad_spend_usd.toFixed(2)}`);
    await expectCard("Expenses (Month)", fmtUsd(s.total_expenses_month_cents));
  });

  test("snapshot line reflects summary.last_snapshot_date (present iff the API has one)", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/finance/summary`);
    expect(api.status()).toBe(200);
    const s = await api.json();

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    if (s.last_snapshot_date) {
      await expect(page.getByText(`Last snapshot: ${s.last_snapshot_date}`)).toBeVisible();
    } else {
      await expect(page.getByText("Last snapshot:", { exact: false })).toHaveCount(0);
    }
  });
});
