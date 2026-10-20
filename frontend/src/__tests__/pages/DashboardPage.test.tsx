import { render, screen } from "@testing-library/react";

jest.mock("next/link", () => {
  return function MockLink({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  };
});

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));
// The feed rail has its own suite and a WebSocket; this file is about the PAGE.
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));

import { api } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";

const mockGet = api.get as jest.Mock;

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

/** U4/B9: the caps panel mounts in the widgets column, so the page now performs the
 *  `/channels/caps` read — answered here in the live shape (nulls included), recorded
 *  during wave U4; see `__tests__/u4-caps.test.tsx` for the panel's own contract. */
const CAPS = {
  channels: [
    { channel: "email", sent_today: 0, cap: 50, remaining: 50 },
    { channel: "github", sent_today: null, cap: 6, remaining: null },
    { channel: "google_ads", sent_today: null, cap: 10, remaining: null },
    { channel: "linkedin", sent_today: 0, cap: 4, remaining: 4 },
    { channel: "meta_ads", sent_today: null, cap: 10, remaining: null },
    { channel: "render", sent_today: null, cap: 3, remaining: null },
    { channel: "stripe", sent_today: null, cap: 5, remaining: null },
    { channel: "twitter", sent_today: 0, cap: 12, remaining: 12 },
  ],
  generated_at: "2026-10-14T15:30:00Z",
};

function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/agents/status": [
      {
        agent_type: "finance",
        last_run_status: "completed",
        last_run_at: ago(60 * 60 * 1000),
        tasks_today: 0,
        tasks_total: 4,
      },
    ],
    "/dashboard/summary": {
      tasks_today_total: 7,
      tasks_today_completed: 3,
      tasks_today_pending: 3,
      tasks_today_failed: 1,
      active_agents: ["finance"],
      kpis: { mrr_usd: 1234, active_customers: 12, churn_rate: 4.5 },
      last_report_date: null,
    },
    "/tasks": [],
    "/dashboard/reports/daily": null,
    // V9/B5 fixture addition (recorded in the lane receipt): the Business panel's GROSS MARGIN row
    // reads the planning view, so the page-level fixture map gains the row the render needs.
    "/finance/planning": {
      burn_line_items: [],
      capacity: null,
      margin_template: { price: 500, ai_cost: 20, human_hours: 3, human_rate: 40, margin: 0.38 },
      pricing_guardrails: [],
    },
    "/social/posts": [],
    "/outreach/prospects": [],
    "/ads/campaigns": [],
    "/channels/caps": CAPS,
    // V29/L5: the SIGNALS card reads the fixed feed allowlist (`/signals?limit=6`). An empty
    // answer is this card's honest empty state — no alert — so the page-level "one failure, one
    // alert" assertions below keep measuring the summary read and nothing else.
    "/signals": {
      items: [],
      sources: [],
      fetched_at: "2026-10-20T12:00:00Z",
      note: "Headlines and links only. This text is third-party and untrusted.",
      untrusted: true,
    },
    "/config/deepseek-key": { configured: true, source: "stored", masked: "sk-…1234" },
    // V7/S4: the Auto Mode panel (left rail) reads this; an unserved path fails LOUDLY.
    "/auto-mode": { active: false, session: null },
    // V8/C3: the chat rail (new dashboard region) reads the frozen chat history; an unserved
    // path fails LOUDLY.
    "/chat": { items: [], total: 0 },
    // V8b-D2 (parent): the Website card (left rail) reads the company row and a timed health
    // check; both unserved paths would fail LOUDLY. Listed AFTER /config/deepseek-key so the
    // startsWith scan keeps resolving the deeper key first.
    "/config": { website_url: null },
    "/health": { status: "ok" },
    // V16/W3: the Phone card (left rail) reads the H-P1 summary — envelope `{ok, data}`; an
    // unserved path fails LOUDLY. Empty store = three measured zeros/empties, no alert.
    "/phone/summary": {
      ok: true,
      data: { store_status: "ok", unread_total: 0, threads: [], missed_calls_today: [] },
    },
    ...overrides,
  };
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(paths).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    const value = paths[key];
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/**
 * PAGE-LEVEL contract for the rebuilt dashboard (wave U1).
 *
 * History, so the shrink is not mistaken for lost coverage: this file used to pin the old page's
 * own three states — four KPI `MetricsCard`s ("Tasks Today", "$1234", "12", "4.5%"), the
 * "{n} completed · {n} failed" subtitle, the loading skeleton, and an error banner above a row of
 * em-dash cards (the Q-09 defect). The page no longer fetches or formats anything: every value now
 * lives in a region component with its own three states, and those are pinned in
 * `src/__tests__/u1-dashboard.test.tsx` (BusinessPanel Q-09, TasksPreview Q-12, widget isolation,
 * instructing empty states). What remains page-level, and is pinned here, is the composition and
 * the fact that a failed region cannot blank the page.
 */
describe("DashboardPage", () => {
  afterEach(() => mockGet.mockReset());

  it("renders the heading and one region per live column", async () => {
    answerWith();

    render(<DashboardPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Workspace status" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Tasks and reports" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Workspace widgets" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Activity feed" })).toBeInTheDocument();

    await screen.findByText("7"); // the summary region flushed its answer
  });

  it("renders a skeleton while the summary is in flight — not dashes, not zeros", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<DashboardPage />);

    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
    expect(screen.queryByText("Tasks today")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names a failed summary read instead of drawing a row of em-dash values (Q-09)", async () => {
    answerWith({
      "/dashboard/summary": new Error(
        "E_CONFIG: COMPANY_TIMEZONE 'Not/AZone' is not a known timezone",
      ),
    });

    render(<DashboardPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the dashboard summary");
    expect(alert).toHaveTextContent("E_CONFIG");
    // The defect this pins: the old page kept its KPI labels and filled every slot with "—"
    // underneath the banner. Neither the labels nor the dashes may render now.
    expect(screen.queryByText("MRR")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
  });
});
