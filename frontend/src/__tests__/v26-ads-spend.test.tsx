import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import { NOT_MEASURED } from "@/components/dashboard/format";
import AdsPage from "@/app/ads/page";

/**
 * V26/L6 — the frontend half of the ads SPEND nullability truth (migration 0015).
 *
 * 0015 dropped `ad_metrics.spend_usd`'s `DEFAULT '0'`, so the wire now carries three spend
 * states and the stored-metrics table must keep them apart:
 *
 *   spend_usd: null -> never measured     -> em dash (NOT_MEASURED)
 *   spend_usd: 0    -> measured zero      -> "$0.00"
 *   spend_usd: 2.5  -> measured value     -> "$2.50"
 *
 * This is a PIN, not a RED-first behaviour change: `CampaignMetrics` already routes the SPEND
 * cell through `usdText` (V25/L4's display law), so no production edit was required for
 * alignment.  The test exists so a future "helpful" `?? 0` — the exact collapse 0015 removes at
 * rest — fails here instead of shipping.
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

function campaign(): Record<string, unknown> {
  return {
    id: 7,
    platform: "meta",
    external_id: null,
    name: "Spend truth",
    goal: null,
    status: "active",
    daily_budget_usd: 12.5,
    total_spent_usd: 3.25,
    created_at: "2026-10-13T18:21:39Z",
  };
}

function metric(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    campaign_id: 7,
    date: "2026-10-18",
    impressions: 1000,
    clicks: 50,
    conversions: 5,
    spend_usd: 12.5,
    revenue_usd: 40,
    ctr: 0.05,
    cpc: 0.25,
    roas: 3.2,
    ...overrides,
  };
}

function wire(metrics: unknown): void {
  mockApiGet.mockImplementation((path: string) => {
    if (path.startsWith("/ads/campaigns/7/metrics")) return Promise.resolve(metrics);
    if (path.startsWith("/ads/campaigns")) return Promise.resolve([campaign()]);
    return Promise.reject(new Error(`unexpected path: ${path}`));
  });
}

/** The SPEND cell of the table row whose DATE cell is `day` (column order: date, impr, clicks,
 *  conv, spend, revenue, ctr, cpc, roas). */
function spendCell(day: string): HTMLElement {
  const dateCell = screen.getByText(day);
  const row = dateCell.closest("tr");
  if (row === null) throw new Error(`no table row for ${day}`);
  return within(row).getAllByRole("cell")[4]!;
}

async function openMetrics(): Promise<void> {
  render(<AdsPage />);
  await screen.findByTestId("campaign-row");
  await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));
  await screen.findByTestId("campaign-metrics-7");
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("V26/L6 — ads spend renders null and measured zero distinctly", () => {
  it("dashes an unmeasured spend and prints a measured zero as $0.00", async () => {
    wire([
      metric({ id: 1, date: "2026-10-18", spend_usd: null, revenue_usd: 40, cpc: null, roas: null }),
      metric({ id: 2, date: "2026-10-17", spend_usd: 0, revenue_usd: 0, cpc: null, roas: null }),
      metric({ id: 3, date: "2026-10-16", spend_usd: 2.5, revenue_usd: 5, cpc: 0.05, roas: 2 }),
    ]);
    await openMetrics();

    const unmeasured = spendCell("2026-10-18");
    expect(unmeasured.textContent).toBe(NOT_MEASURED);
    expect(unmeasured.textContent).not.toContain("$0.00");

    const zero = spendCell("2026-10-17");
    expect(zero.textContent).toBe("$0.00");
    expect(zero.textContent).not.toBe(NOT_MEASURED);

    const measured = spendCell("2026-10-16");
    expect(measured.textContent).toBe("$2.50");
  });
});
