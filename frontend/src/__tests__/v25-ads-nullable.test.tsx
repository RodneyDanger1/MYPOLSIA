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
 * V25/L4 — the frontend half of the ads nullability truth (migration 0014).
 *
 * The wire now carries THREE states the card must keep apart:
 *
 *   revenue_usd: null  -> never measured          -> em dash (NOT_MEASURED)
 *   revenue_usd: 0     -> measured zero           -> "$0.00"
 *   revenue_usd: 20    -> measured value          -> "$20.00"
 *   roas:        null  -> unmeasurable            -> em dash
 *   roas:        0     -> measured zero over spend-> "0.00×"
 *
 * and the campaign strip must disclose partial coverage with the exact copy
 * "ROAS covers N of M days with both revenue and spend measured." so a ratio is never
 * silently averaged over days that lack an input.
 *
 * RED-first on the pre-V25 tree: the API served every stored zero as `null`, so the
 * "measured zero" case below rendered a dash (the documented V24/L4 deviation).  These tests
 * pin the DISPLAY law against the wire the API now serves; they do not re-test the fetcher.
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

describe("V25/L4 — ads revenue/ROAS render null and measured zero distinctly", () => {
  it("renders the em dash ONLY for null, never for a measured zero", async () => {
    wire([
      // measured zero: both are READINGS and must print as zeros
      metric({ id: 1, spend_usd: 5, revenue_usd: 0, roas: 0 }),
      // unmeasured: both are ABSENT and must dash
      metric({ id: 2, date: "2026-10-17", spend_usd: 4, revenue_usd: null, roas: null }),
    ]);

    const panel = await openMetrics();

    // The card sums the measured days: 0 + (nothing) = a measured $0.00, and ROAS covers the
    // one day with both inputs: 0 / 5 = 0.00×.
    const revenue = screen.getByTestId("campaign-revenue-7");
    const roas = screen.getByTestId("campaign-roas-7");
    expect(revenue).toHaveTextContent("$0.00");
    expect(roas).toHaveTextContent("0.00×");
    expect(revenue.textContent).not.toContain(NOT_MEASURED);
    expect(roas.textContent).not.toContain(NOT_MEASURED);

    // …and the per-day table keeps the same distinction on the unmeasured row.
    expect(panel).toHaveTextContent(NOT_MEASURED);
  });

  it("dashes both summary numbers when NO day carries a measured revenue", async () => {
    wire([
      metric({ id: 1, revenue_usd: null, roas: null }),
      metric({ id: 2, date: "2026-10-17", revenue_usd: null, roas: null }),
    ]);

    await openMetrics();

    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent(NOT_MEASURED);
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent(NOT_MEASURED);
    expect(screen.getByTestId("campaign-revenue-7").textContent).not.toContain("$0.00");
    expect(screen.getByTestId("campaign-roas-7").textContent).not.toContain("0.00×");
  });

  it("discloses ROAS coverage with the exact copy when some days are unmeasured", async () => {
    wire([
      metric({ id: 1, spend_usd: 8, revenue_usd: 20 }),
      metric({ id: 2, date: "2026-10-17", spend_usd: 4, revenue_usd: null }),
      metric({ id: 3, date: "2026-10-16", spend_usd: 2, revenue_usd: 6 }),
    ]);

    await openMetrics();

    const coverage = screen.getByTestId("campaign-roas-coverage-7");
    expect(coverage).toHaveTextContent(
      "ROAS covers 2 of 3 days with both revenue and spend measured.",
    );
    // 26 revenue / 10 spend over the two paired days — the unmeasured day never dilutes it.
    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent("$26.00");
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent("2.60×");
  });

  it("omits the coverage note when every loaded day is fully measured", async () => {
    wire([
      metric({ id: 1, spend_usd: 8, revenue_usd: 20 }),
      metric({ id: 2, date: "2026-10-17", spend_usd: 12, revenue_usd: 0 }),
    ]);

    const panel = await openMetrics();

    expect(panel.querySelector("[data-testid='campaign-roas-coverage-7']")).toBeNull();
    expect(screen.getByTestId("campaign-revenue-7")).toHaveTextContent("$20.00");
    // 20 / 20 — the measured-zero day is INCLUDED (it has both inputs), it just adds nothing.
    expect(screen.getByTestId("campaign-roas-7")).toHaveTextContent("1.00×");
  });
});
