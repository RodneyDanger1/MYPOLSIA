import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));
// The three client children have their own suites; this file is about the page's OWN three states.
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));
jest.mock("@/components/dashboard/AgentStatusGrid", () => ({ AgentStatusGrid: () => null }));
jest.mock("@/components/dashboard/DeepSeekKeyOnboarding", () => ({ DeepSeekKeyOnboarding: () => null }));

import { api, type DashboardSummary } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

const wireSummary: DashboardSummary = {
  tasks_today_total: 7,
  tasks_today_completed: 3,
  tasks_today_pending: 4,
  tasks_today_failed: 1,
  active_agents: ["finance"],
  kpis: { mrr_usd: 1234, active_customers: 12, churn_rate: 4.5 },
  last_report_date: null,
};

describe("DashboardPage", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a skeleton while the summary is in flight — not dashes, not zeros", () => {
    mockApiGet.mockReturnValue(new Promise(() => {})); // never settles
    const { container } = render(<DashboardPage />);

    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
    expect(screen.queryByText("Tasks Today")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the KPI values the API answered with", async () => {
    mockApiGet.mockResolvedValue(wireSummary);

    render(<DashboardPage />);

    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(screen.getByText("3 completed · 1 failed")).toBeInTheDocument();
    expect(screen.getByText("$1234")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("4.5%")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names a failed read instead of turning it into dashes — and fabricates no zeros", async () => {
    mockApiGet.mockRejectedValue(new Error("E_CONFIG: COMPANY_TIMEZONE 'Not/AZone' is not a known timezone"));

    render(<DashboardPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the dashboard summary");
    expect(alert).toHaveTextContent("E_CONFIG");
    // The cards are still on the page (the labels are what the e2e spec asserts), but nothing
    // pretends a number was measured: no "0 completed · 0 failed" line.
    expect(screen.getByText("Tasks Today", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText(/completed ·/)).toBeNull();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });
});
