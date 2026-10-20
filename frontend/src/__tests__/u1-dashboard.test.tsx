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
// The feed rail is the realtime lane's component with its own suite (and a WebSocket); this file
// is about the dashboard's regions, so the rail is stubbed at the boundary.
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));

import { api } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";
import { AdsWidget } from "@/components/dashboard/AdsWidget";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { OutreachWidget } from "@/components/dashboard/OutreachWidget";
import { PolsiaStatusCard } from "@/components/dashboard/PolsiaStatusCard";
import { SocialWidget } from "@/components/dashboard/SocialWidget";
import { TASKS_PREVIEW_LIMIT, TasksPreview } from "@/components/dashboard/TasksPreview";

const mockGet = api.get as jest.Mock;

const DAY_MS = 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

const AGENTS = [
  {
    agent_type: "social_media",
    last_run_status: "running",
    last_run_at: ago(30 * 60 * 1000 + 5_000),
    tasks_today: 2,
    tasks_total: 9,
  },
  {
    agent_type: "finance",
    last_run_status: "completed",
    last_run_at: ago(2 * DAY_MS + HOUR_MS),
    tasks_today: 0,
    tasks_total: 4,
  },
];

const SUMMARY = {
  tasks_today_total: 7,
  tasks_today_completed: 3,
  tasks_today_pending: 3,
  tasks_today_failed: 1,
  active_agents: ["finance", "social_media"],
  kpis: { mrr_usd: 1234, active_customers: 12, churn_rate: 4.5 },
  last_report_date: "2026-10-12",
};

const TASK_ROW = {
  id: 1,
  title: "Draft the owner launch checklist",
  description: "One focused first-run checklist",
  agent_type: "business_planning",
  status: "pending",
  priority: 3,
  created_at: ago(2 * DAY_MS + HOUR_MS),
};

const REPORT = {
  id: 5,
  report_date: "2026-10-12",
  morning_plan: "Morning plan text",
  evening_summary: "Evening summary text",
  tasks_planned: 4,
  tasks_completed: 3,
  tasks_failed: 1,
  updated_at: ago(HOUR_MS),
};

const POST = {
  id: 11,
  content: "Independent contractors lose jobs to missed calls and forgotten follow-ups.",
  status: "published",
  scheduled_for: null,
  published_at: ago(2 * DAY_MS + HOUR_MS),
  engagement: null,
};

const PROSPECT = {
  id: 21,
  email: "lead@example.test",
  first_name: "Ada",
  company: "Northline",
  status: "contacted",
  created_at: ago(HOUR_MS),
};

const CAMPAIGN = {
  id: 31,
  platform: "meta",
  name: "Missed-lead test campaign",
  status: "active",
  daily_budget_usd: 5,
  total_spent_usd: 1.5,
};

/** U4/B9: the caps panel mounts in the widgets column, so the page now performs the
 *  `/channels/caps` read — answered here in the live shape (nulls included), recorded during
 *  wave U4; the panel's own contract lives in `__tests__/u4-caps.test.tsx`. */
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

/** Every read the dashboard makes, answered per path; an unlisted path fails LOUDLY so a widget
 *  that starts reading something new cannot pass this suite silently. */
function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/agents/status": AGENTS,
    "/dashboard/summary": SUMMARY,
    "/tasks": [TASK_ROW],
    "/dashboard/reports/daily": REPORT,
    // V9/B5 fixture addition (recorded in the lane receipt): BusinessPanel's GROSS MARGIN row reads
    // the planning view's margin_template. The row renders a MEASURED margin here, so this suite's
    // em-dash count (absent MRR + absent churn) is unchanged — no assertion was touched.
    "/finance/planning": {
      burn_line_items: [],
      capacity: null,
      margin_template: { price: 500, ai_cost: 20, human_hours: 3, human_rate: 40, margin: 0.38 },
      pricing_guardrails: [],
    },
    "/social/posts": [POST],
    "/outreach/prospects": [PROSPECT],
    "/ads/campaigns": [CAMPAIGN],
    "/channels/caps": CAPS,
    // V29/L5: the SIGNALS card reads the fixed feed allowlist (`/signals?limit=6`). An empty
    // answer is this card's honest empty state — no alert — so the at-rest dashboard stays
    // alert-free like every other panel here. Listed so a NEW unserved read still fails LOUDLY.
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

describe("dashboard composition (wave U1)", () => {
  afterEach(() => mockGet.mockReset());

  it("composes the live four regions inside the shell", async () => {
    answerWith();

    render(<DashboardPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Workspace status" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Tasks and reports" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Workspace widgets" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Activity feed" })).toBeInTheDocument();

    // Every region really read its own endpoint.
    expect(await screen.findByText(TASK_ROW.title)).toBeInTheDocument();
    expect(screen.getByText(PROSPECT.email)).toBeInTheDocument();
    expect(screen.getByText(CAMPAIGN.name)).toBeInTheDocument();
  });

  it("renders skeletons while reads are in flight — never the empty states, never an error", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<DashboardPage />);

    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
    expect(screen.queryByText(/No tasks yet/)).toBeNull();
    expect(screen.queryByText(/No posts yet/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("isolates one failing widget — the other regions keep their data", async () => {
    answerWith({ "/social/posts": new Error("E_HTTP_503: social unavailable") });

    render(<DashboardPage />);

    expect(await screen.findByText(/Could not load social posts: E_HTTP_503/)).toBeInTheDocument();
    expect(screen.getByText(PROSPECT.email)).toBeInTheDocument();
    expect(screen.getByText(CAMPAIGN.name)).toBeInTheDocument();
    expect(screen.getByText(TASK_ROW.title)).toBeInTheDocument();
  });

  it("prints an instructing empty state per widget only after a successful empty read (Q-26)", async () => {
    answerWith({
      "/tasks": [],
      "/dashboard/reports/daily": null,
      "/social/posts": [],
      "/outreach/prospects": [],
      "/ads/campaigns": [],
    });

    render(<DashboardPage />);

    expect(
      await screen.findByText(/No tasks yet\. Agents add tasks during their cycles/),
    ).toBeInTheDocument();
    expect(screen.getByText(/No report for today yet\. The daily cycle writes one/)).toBeInTheDocument();
    expect(
      screen.getByText(/No posts yet\. The social media agent drafts posts during its cycles/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/No prospects yet\. The outreach agent finds prospects when it runs/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/No campaigns yet\. The ads agent creates campaigns during its cycles/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("BusinessPanel (Q-09)", () => {
  afterEach(() => mockGet.mockReset());

  it("replaces the value rows with the named failure — no em-dash row beneath the alert", async () => {
    answerWith({ "/dashboard/summary": new Error("E_NETWORK: request timed out") });

    render(<BusinessPanel />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load the dashboard summary: E_NETWORK: request timed out",
    );
    // The Q-09 defect: a full row of "not measured" labels under the banner. Neither the labels
    // nor a single em dash may render here.
    expect(screen.queryByText("MRR")).toBeNull();
    expect(screen.queryByText("Tasks today")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("renders the values the API answered with, including a stated 'no report yet'", async () => {
    answerWith({ "/dashboard/summary": { ...SUMMARY, last_report_date: null } });

    render(<BusinessPanel />);

    expect(await screen.findByText("7")).toBeInTheDocument();
    // V17/L4 re-pin: the dashboard money shape is the finance family's (grouping + two decimals);
    // the raw-float "$1234" this used to pin was the Q-13 split the shell had left behind.
    expect(screen.getByText("$1,234.00")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("4.5%")).toBeInTheDocument();
    expect(screen.getByText("no report yet")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the em dash for a genuinely absent KPI — and never for a real 0", async () => {
    answerWith({ "/dashboard/summary": { ...SUMMARY, kpis: { active_customers: 0 } } });

    render(<BusinessPanel />);

    expect(await screen.findByText("0")).toBeInTheDocument(); // measured zero, not a dash
    expect(screen.getAllByText("—")).toHaveLength(2); // absent MRR + absent churn rate
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("TasksPreview (Q-12)", () => {
  afterEach(() => mockGet.mockReset());

  it("labels a truncated preview instead of inventing a total", async () => {
    answerWith({ "/tasks": [1, 2, 3, 4].map((n) => ({ ...TASK_ROW, id: n, title: `Task ${n}` })) });

    render(<TasksPreview />);

    expect(await screen.findByText("Task 1")).toBeInTheDocument();
    expect(
      screen.getByText(`Showing the ${TASKS_PREVIEW_LIMIT} newest tasks — more may exist`),
    ).toBeInTheDocument();
  });

  it("says the list is complete when fewer rows came back than were asked for", async () => {
    answerWith({ "/tasks": [{ ...TASK_ROW, id: 1 }, { ...TASK_ROW, id: 2, title: "Second" }] });

    render(<TasksPreview />);

    expect(await screen.findByText("Showing all 2 tasks")).toBeInTheDocument();
  });

  it("names a failed read and does not print the empty state", async () => {
    answerWith({ "/tasks": new Error("E_HTTP_500: boom") });

    render(<TasksPreview />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load tasks: E_HTTP_500: boom",
    );
    expect(screen.queryByText(/No tasks yet/)).toBeNull();
  });

  it("renders the wire's own chips without inventing a credit cost", async () => {
    answerWith();

    render(<TasksPreview />);

    expect(await screen.findByText("Strategy")).toBeInTheDocument(); // agent label
    expect(screen.getByText("pending")).toBeInTheDocument(); // status chip
    expect(screen.getByText("Priority 3")).toBeInTheDocument(); // the real field in the cost slot
    // No credit/cost exists on our Task wire type — and none may be printed.
    expect(screen.queryByText(/CREDIT/i)).toBeNull();
  });
});

describe("PolsiaStatusCard", () => {
  afterEach(() => mockGet.mockReset());

  it("expresses the real run state from /agents/status", async () => {
    answerWith();

    render(<PolsiaStatusCard />);

    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 agents running now")).toBeInTheDocument();
    expect(screen.getByText("30m ago")).toBeInTheDocument(); // newest last_run_at
    expect(screen.getByText("1 of 2")).toBeInTheDocument(); // agents with tasks today
  });

  it("reads Idle only from the data — no run in flight and none recorded", async () => {
    answerWith({
      "/agents/status": [
        {
          agent_type: "finance",
          last_run_status: null,
          last_run_at: null,
          tasks_today: 0,
          tasks_total: 0,
        },
      ],
    });

    render(<PolsiaStatusCard />);

    expect(await screen.findByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("No agent run in flight")).toBeInTheDocument();
    expect(screen.getByText("none recorded")).toBeInTheDocument();
  });

  it("names a failed agent-status poll instead of showing a plausible idle card", async () => {
    answerWith({ "/agents/status": new Error("E_NETWORK: request timed out") });

    render(<PolsiaStatusCard />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load agent status: E_NETWORK: request timed out",
    );
    expect(screen.queryByText("Idle")).toBeNull();
    expect(screen.queryByText("Working")).toBeNull();
  });
});

describe("right-column widgets", () => {
  afterEach(() => mockGet.mockReset());

  it("SocialWidget keeps a null published_at as a stated placeholder (Q-25)", async () => {
    answerWith({ "/social/posts": [{ ...POST, published_at: null, status: null }] });

    render(<SocialWidget />);

    expect(await screen.findByText("date unknown")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("OutreachWidget states the real loaded count instead of a mail total it cannot know", async () => {
    answerWith();

    render(<OutreachWidget />);

    expect(await screen.findByText(PROSPECT.email)).toBeInTheDocument();
    expect(screen.getByText("contacted")).toBeInTheDocument();
    expect(screen.getByText("1 most recent loaded")).toBeInTheDocument();
    expect(screen.queryByText(/SENT/i)).toBeNull();
  });

  it("AdsWidget renders a not-recorded spend as the em dash, never $0.00", async () => {
    answerWith({ "/ads/campaigns": [{ ...CAMPAIGN, total_spent_usd: null }] });

    render(<AdsWidget />);

    expect(await screen.findByText(CAMPAIGN.name)).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).toBeNull();
    // U4 parent money-format alignment (old -> new: "$5" -> "$5.00"): the widget renders the
    // house shape (separators + two decimals) like every other money surface.
    expect(screen.getByText("$5.00")).toBeInTheDocument(); // the budget that IS recorded
  });

  it("DailyReportPanel renders the report it read", async () => {
    answerWith();

    render(<DailyReportPanel />);

    expect(await screen.findByText("2026-10-12")).toBeInTheDocument();
    expect(screen.getByText("Evening summary text")).toBeInTheDocument();
    expect(screen.getByText("Evening summary")).toBeInTheDocument();
  });

  it("DailyReportPanel's 'no report today' is an empty state, not an error", async () => {
    answerWith({ "/dashboard/reports/daily": null });

    render(<DailyReportPanel />);

    expect(
      await screen.findByText(/No report for today yet\. The daily cycle writes one/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
