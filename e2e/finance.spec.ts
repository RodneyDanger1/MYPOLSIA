import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/** Mirrors the page's own formatCents (en-US, 2 decimals, prefixed with $). */
function fmtUsd(cents: number): string {
  return "$" + (cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * The wire contract this spec mirrors (backend `schemas/finance.py`'s "None is not measured";
 * `frontend/src/app/finance/page.tsx`'s formatCentsOrDash): `null` renders as an em dash, a measured
 * `0` renders as `$0.00`.  Computing `$0.00` from a null (this spec's stale assumption) demands the
 * page collapse "not measured" into "measured zero" — the exact collapse the API and page keep apart.
 */
function fmtUsdOrDash(cents: number | null): string {
  return cents == null ? "—" : fmtUsd(cents);
}

test.describe("Finance page", () => {
  test("the six metric cards show the values GET /api/v1/finance/summary reports", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/finance/summary`);
    expect(api.status()).toBe(200);
    const s = await api.json();

    // The page must do its OWN read: with nullable metrics the honest no-snapshot state is all em
    // dashes, which is exactly what a render-without-fetch would also show — so watch the wire.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/v1/finance/summary")) apiCalls.push(r.url());
    });

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    const expectCard = async (title: string, value: string) => {
      const card = page
        .locator("main div.bg-gray-800")
        .filter({ has: page.getByText(title, { exact: true }) });
      await expect(card, `card "${title}" must exist exactly once`).toHaveCount(1);
      // Value is computed from the API response above under the page's own null law — a page that
      // renders a stale snapshot, or prints "$0.00" for an unmeasured null, fails here.
      await expect(card.getByText(value, { exact: true })).toBeVisible();
    };

    await expectCard("MRR", fmtUsdOrDash(s.mrr_cents));
    await expectCard("ARR", fmtUsdOrDash(s.arr_cents));
    await expectCard(
      "Active Subscribers",
      s.active_subscribers == null ? "—" : String(s.active_subscribers),
    );
    await expectCard("Stripe Balance", fmtUsdOrDash(s.stripe_balance_cents));
    await expectCard("Total Ad Spend", s.total_ad_spend_usd == null ? "—" : `$${s.total_ad_spend_usd.toFixed(2)}`);
    await expectCard("Expenses (Month)", fmtUsdOrDash(s.total_expenses_month_cents));

    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/finance/summary — an all-em-dash render without a " +
        "read is the stale-snapshot defect wearing the honest null state",
    ).toBeGreaterThan(0);
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
