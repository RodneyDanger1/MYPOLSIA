import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NOT_MEASURED } from "@/components/dashboard/format";
import AdsPage from "@/app/ads/page";

/**
 * V22/L3 — the ads metrics seam.
 *
 * `GET /api/v1/ads/campaigns/{id}/metrics` is a served route
 * (`backend/app/api/v1/ads.py::ads_campaign_metrics`) whose docstring names the frontend /ads page
 * as its consumer, but the page never issued the read: the stored per-day measurements the ads
 * agent writes had no product surface. The smallest honest consumer is a per-campaign expander
 * that reads ONLY when opened (nothing polled, nothing prefetched) and renders the real wire rows
 * (`AdMetricResponse`) with the app-wide null law.
 *
 * Pinned here:
 *  - the control exists and issues the REAL path with the bounded limit;
 *  - collapsed means zero requests (a closed expander is not a read);
 *  - the real fields render (date, counts, money, ratios);
 *  - null is the marker, a measured 0 is a reading;
 *  - empty and failed reads are stated in their own words, never as "no data".
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

/** Answer the list read and the per-campaign metrics read by path. */
function wire(metrics: unknown, campaigns: unknown = [campaign()]): void {
  mockApiGet.mockImplementation((path: string) => {
    if (path.startsWith("/ads/campaigns/7/metrics")) {
      return Promise.resolve(metrics);
    }
    if (path.startsWith("/ads/campaigns")) {
      return Promise.resolve(campaigns);
    }
    return Promise.reject(new Error(`unexpected path: ${path}`));
  });
}

function metricsCalls(): string[] {
  return mockApiGet.mock.calls
    .map((call) => String(call[0]))
    .filter((path) => path.includes("/metrics"));
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("Ads — stored campaign metrics have a real consumer", () => {
  it("renders a Metrics control per campaign and reads nothing until it is opened", async () => {
    wire([metric()]);

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");

    expect(screen.getByTestId("campaign-metrics-toggle-7")).toBeInTheDocument();
    // Closed is not a read: no metrics request was issued by mounting the page.
    expect(metricsCalls()).toHaveLength(0);
  });

  it("opening the expander issues GET /ads/campaigns/7/metrics with the bounded limit", async () => {
    wire([metric()]);

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");

    await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));

    await screen.findByTestId("campaign-metrics-7");
    expect(metricsCalls()).toContain("/ads/campaigns/7/metrics?limit=90");
  });

  it("renders the real wire fields: date, counts, money and ratios", async () => {
    wire([metric()]);

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");
    await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));

    const panel = await screen.findByTestId("campaign-metrics-7");
    expect(panel).toHaveTextContent("2026-10-18");
    expect(panel).toHaveTextContent("1,000");
    expect(panel).toHaveTextContent("50");
    expect(panel).toHaveTextContent("5");
    expect(panel).toHaveTextContent("$12.50");
    expect(panel).toHaveTextContent("$40.00");
    expect(panel).toHaveTextContent("5.00%");
    expect(panel).toHaveTextContent("$0.25");
    expect(panel).toHaveTextContent("3.20×");
  });

  it("null stays the marker and a measured 0 stays a reading (null-vs-zero law)", async () => {
    wire([
      metric({ impressions: null, clicks: null, conversions: null, spend_usd: null, revenue_usd: null, ctr: null, cpc: null, roas: null }),
      metric({ id: 2, date: "2026-10-17", impressions: 0, clicks: 0, conversions: 0, spend_usd: 0, revenue_usd: 0, ctr: 0, cpc: 0, roas: 0 }),
    ]);

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");
    await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));

    const panel = await screen.findByTestId("campaign-metrics-7");
    expect(panel).toHaveTextContent(NOT_MEASURED);
    // The measured-zero row prints real zero readings, not the marker for every cell.
    expect(panel).toHaveTextContent("0.00%");
    expect(panel).toHaveTextContent("$0.00");
  });

  it("an empty read says the producer writes these rows, never 'no data'", async () => {
    wire([]);

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");
    await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));

    const panel = await screen.findByTestId("campaign-metrics-7");
    expect(panel).toHaveTextContent(/No stored metric days/i);
    expect(panel.textContent ?? "").not.toContain("no data");
  });

  it("a failed read names the failure (ApiError.display), never an empty table", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith("/ads/campaigns/7/metrics")) {
        return Promise.reject(new ApiError(404, "unknown campaign", "E_NOT_FOUND", "check the id"));
      }
      return Promise.resolve([campaign()]);
    });

    render(<AdsPage />);
    await screen.findByTestId("campaign-row");
    await userEvent.setup().click(screen.getByTestId("campaign-metrics-toggle-7"));

    const panel = await screen.findByTestId("campaign-metrics-7");
    await waitFor(() => {
      expect(panel).toHaveTextContent("E_NOT_FOUND: unknown campaign — check the id");
    });
    expect(panel).not.toHaveTextContent(/No stored metric days/i);
  });
});
