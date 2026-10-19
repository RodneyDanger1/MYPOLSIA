import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import { NOT_MEASURED } from "@/components/dashboard/format";
import AdsPage from "@/app/ads/page";

/**
 * V24/L4 — the ads card's revenue truth.
 *
 * The stored-metrics expander already lists each day; this lane adds the CAMPAIGN-LEVEL reading
 * the card shows: Revenue (sum of the days whose revenue was measured) and ROAS (only from the
 * days where BOTH revenue and spend were measured).  The three states pinned here:
 *
 *   1. measured      -> real dollars and a real multiple;
 *   2. unmeasured    -> the honest dash for BOTH, never `$0.00` / `0.00×`;
 *   3. measured zero -> `$0.00` / `0.00×` ARE readings and must render as such.
 *
 * Plus the partial-coverage disclosure: ROAS never silently averages in a day whose revenue is
 * absent — the card says how many days it covers.
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

function campaign(): Record<string, unknown> {
  return {
    id: 7,
    platform: "meta",
    external_id: null,
    name: "Summer sale",
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

async function openMetrics(): Promise<HTMLElement> {
  render(<AdsPage />);
  await screen.findByTestId("campaign-row");
  await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));
  return screen.findByTestId("campaign-metrics-7");
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("Ads — campaign Revenue and ROAS tell the measured truth", () => {
  it("shows the measured revenue total and the ROAS multiple", async () => {
    wire([
      metric({ id: 1, spend_usd: 8, revenue_usd: 20 }),
      metric({ id: 2, date: "2026-10-17", spend_usd: 12, revenue_usd: 30 }),
    ]);

    const panel = await openMetrics();

    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent("$50.00");
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent("2.50×"); // 50 / 20
    // Fully measured: no coverage caveat.
    expect(panel.querySelector("[data-testid='campaign-roas-coverage-7']")).toBeNull();
  });

  it("renders the honest dash when revenue is unmeasured — never $0.00 or 0.00×", async () => {
    wire([
      metric({ id: 1, revenue_usd: null, roas: null }),
      metric({ id: 2, date: "2026-10-17", revenue_usd: null, spend_usd: 4, roas: null }),
    ]);

    await openMetrics();

    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent(NOT_MEASURED);
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent(NOT_MEASURED);
    expect(screen.getByTestId("campaign-roas-7").textContent).not.toContain("0.00×");
  });

  it("keeps a measured zero a reading: $0.00 revenue and 0.00× ROAS", async () => {
    wire([metric({ id: 1, spend_usd: 5, revenue_usd: 0, roas: 0 })]);

    await openMetrics();

    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent("$0.00");
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent("0.00×");
  });

  it("discloses how many days the ROAS covers when some revenue is unmeasured", async () => {
    wire([
      metric({ id: 1, spend_usd: 8, revenue_usd: 20 }),
      metric({ id: 2, date: "2026-10-17", spend_usd: 4, revenue_usd: null }),
    ]);

    const panel = await openMetrics();

    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent("$20.00");
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent("2.50×"); // 20 / 8 only
    expect(panel).toHaveTextContent(/1 of 2 days/i);
  });
});
