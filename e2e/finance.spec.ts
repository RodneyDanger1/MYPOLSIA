import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * Wave U3 re-aim (old -> new):
 *   old: cards located by `main div.bg-gray-800` + title text; freshness pinned as `Last snapshot: <d>`
 *        (colon); per-card presence assumed on every stack.
 *   new: the rebuilt page's own anchors — `[data-testid="finance-kpis"]` cards keyed by
 *        `data-kpi-key`, freshness `<p data-testid="finance-freshness">Last snapshot <d></p>` (NO
 *        colon — `last_snapshot_date` is a date, printed as-is), the idle notice
 *        `[data-testid="finance-idle"]`, and the three list panels `finance-list-<revenue|expenses|events>`.
 * The page's null law is unchanged and is re-implemented here to compute expectations from the SAME
 * API body the page reads: `null` (or NaN) renders `—`, a measured `0` renders `$0.00`, and on a
 * successful read where nothing was ever measured the page renders the idle notice INSTEAD of a
 * dash grid (`components/finance/summary.ts` `isIdleSummary` — mirrored below, deliberately:
 * this spec must fail if the page collapses "not started" into "measured empty", or vice versa).
 */
function usd(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function cents(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return usd(value / 100);
}
function count(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US");
}

type Sum = {
  last_snapshot_date: string | null;
  mrr_cents: number | null;
  arr_cents: number | null;
  active_subscribers: number | null;
  total_ad_spend_usd: number | null;
  total_expenses_month_cents: number | null;
  stripe_balance_cents: number | null;
  ai_spend:
    | {
        spent: number | null;
        limit: number;
        unit_economics?: {
          total_completed?: number | null;
          per_agent?: Record<string, unknown> | null;
          unmeasured_spend_agents?: string[] | null;
        } | null;
      }
    | null
    | undefined;
};

/** Mirror of the page's isIdleSummary (components/finance/summary.ts). */
function idleSummary(s: Sum): boolean {
  const snapshotAbsent =
    s.last_snapshot_date == null &&
    s.mrr_cents == null &&
    s.arr_cents == null &&
    s.active_subscribers == null &&
    s.total_ad_spend_usd == null &&
    s.total_expenses_month_cents == null &&
    s.stripe_balance_cents == null;
  if (!snapshotAbsent) return false;
  const spend = s.ai_spend;
  if (!spend) return true;
  if (spend.spent != null && spend.spent > 0) return false;
  const e = spend.unit_economics;
  if (e) {
    if ((e.total_completed ?? 0) > 0) return false;
    if (Object.keys(e.per_agent ?? {}).length > 0) return false;
    if ((e.unmeasured_spend_agents?.length ?? 0) > 0) return false;
  }
  return true;
}

test.describe("Finance page", () => {
  test("the KPI grid shows what GET /finance/summary reports — or the idle notice when nothing was ever measured", async ({
    page,
    request,
  }) => {
    const api = await request.get(`${API_BASE}/api/v1/finance/summary`);
    expect(api.status()).toBe(200);
    const s = (await api.json()) as Sum;

    // The page must do its OWN read: watch the wire, because a render-without-fetch is the
    // stale-snapshot defect wearing the honest state.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/v1/finance/summary")) apiCalls.push(r.url());
    });

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    if (idleSummary(s)) {
      // "Not started" is its own state: the idle notice renders and the dash grid does NOT.
      await expect(page.getByTestId("finance-idle")).toBeVisible();
      await expect(page.getByTestId("finance-idle")).toContainText("Not running yet");
      await expect(page.getByTestId("kpi-card")).toHaveCount(0);
    } else {
      await expect(page.getByTestId("finance-kpis")).toBeVisible();
      const expectCard = async (key: string, value: string) => {
        const card = page.locator(`[data-testid="kpi-card"][data-kpi-key="${key}"]`);
        await expect(card, `card "${key}" must exist exactly once`).toHaveCount(1);
        await expect(card.getByText(value, { exact: true })).toBeVisible();
      };

      const spent = s.ai_spend?.spent;
      const expectedAi = s.ai_spend == null || spent == null ? "—" : `${usd(spent)} of ${usd(s.ai_spend.limit)}`;
      await expectCard("ai-spend", expectedAi);

      await expectCard("mrr", cents(s.mrr_cents));
      await expectCard("arr", cents(s.arr_cents));
      await expectCard("subscribers", count(s.active_subscribers));
      await expectCard("stripe-balance", cents(s.stripe_balance_cents));
      await expectCard("ad-spend", usd(s.total_ad_spend_usd));
      await expectCard("expenses", cents(s.total_expenses_month_cents));

      const unmeasured = s.ai_spend?.unit_economics?.unmeasured_spend_agents ?? [];
      if (unmeasured.length > 0) {
        await expect(
          page.locator(`[data-testid="kpi-card"][data-kpi-key="ai-spend"]`),
        ).toContainText(`unmeasured spend: ${unmeasured.join(", ")}`);
      }
    }

    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/finance/summary — an idle or all-em-dash render " +
        "without a read is the stale-snapshot defect wearing the honest state",
    ).toBeGreaterThan(0);
  });

  test("freshness line reflects summary.last_snapshot_date (present iff the API has one and the data is not idle)", async ({
    page,
    request,
  }) => {
    const api = await request.get(`${API_BASE}/api/v1/finance/summary`);
    expect(api.status()).toBe(200);
    const s = (await api.json()) as Sum;

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    const freshness = page.getByTestId("finance-freshness");
    if (s.last_snapshot_date && !idleSummary(s)) {
      // U3 format: `Last snapshot 2026-10-13` — the date AS-IS, no colon, no derived clock.
      await expect(freshness).toBeVisible();
      await expect(freshness).toHaveText(`Last snapshot ${s.last_snapshot_date}`);
    } else {
      await expect(freshness).toHaveCount(0);
    }
  });

  test("the three list panels render what their APIs returned, with measured counts and cap disclosure", async ({
    page,
    request,
  }) => {
    const [revenue, expenses, events] = await Promise.all([
      request.get(`${API_BASE}/api/v1/finance/revenue?limit=30`),
      request.get(`${API_BASE}/api/v1/finance/expenses?limit=100`),
      request.get(`${API_BASE}/api/v1/finance/events?limit=100`),
    ]);
    expect(revenue.status()).toBe(200);
    expect(expenses.status()).toBe(200);
    expect(events.status()).toBe(200);
    const rows = {
      revenue: (await revenue.json()) as { snapshot_date: string }[],
      expenses: (await expenses.json()) as { date: string; vendor: string | null }[],
      events: (await events.json()) as { event_type: string }[],
    };

    await page.goto("/finance");
    await expect(page.getByRole("heading", { name: "Finance" })).toBeVisible();

    const check = async (
      listId: "revenue" | "expenses" | "events",
      limit: number,
      noun: string,
      firstCell: string | null,
    ) => {
      const panel = page.getByTestId(`finance-list-${listId}`);
      await expect(panel).toBeVisible();
      const n = rows[listId].length;
      // The header count is what THE READ measured — cross-checked against the API's own rows.
      await expect(page.getByTestId(`${listId}-count`)).toHaveText(`${n} ${n === 1 ? noun : noun + "s"}`);
      if (n === 0) {
        await expect(panel.getByText(/No .* yet —/)).toBeVisible();
      } else {
        await expect(panel.getByTestId(`${listId}-table`)).toBeVisible();
        if (firstCell) {
          await expect(panel.locator("tbody tr").first()).toContainText(firstCell);
        }
        if (n >= limit) {
          await expect(page.getByTestId(`${listId}-truncation`)).toBeVisible();
        }
      }
    };

    await check("revenue", 30, "snapshot", rows.revenue[0]?.snapshot_date ?? null);
    await check("expenses", 100, "expense", rows.expenses[0]?.date ?? null);
    await check("events", 100, "event", rows.events[0]?.event_type ?? null);
  });
});
