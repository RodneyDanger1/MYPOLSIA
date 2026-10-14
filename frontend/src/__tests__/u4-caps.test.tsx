/**
 * Wave U4 / B9 — the caps panel (`CapsPanel`) and its mount in the dashboard widgets column.
 *
 * Every fixture mirrors the VERIFIED live payload of `GET /channels/caps` (U3: eight channels
 * sorted by name). Every assertion pins one arm of the null discipline: an unknown counter
 * stays unknown (never a fabricated 0), an absent cap is named ("no cap configured"), a
 * negative remaining is honestly over-cap, a failure is named (never an empty list), and the
 * empty list is only ever rendered after a real empty answer.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";

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
// The feed rail has its own suite and a WebSocket; this file is about the caps panel.
jest.mock("@/components/dashboard/ActivityFeed", () => ({ ActivityFeed: () => null }));

import { api } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";
import { CapsPanel } from "@/components/dashboard/CapsPanel";

const mockGet = api.get as jest.Mock;

const ISO = "2026-10-14T15:30:00Z";
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

/** The verified live payload: email 0/50, twitter 0/12, linkedin 0/4 counted; github,
 *  google_ads, meta_ads, render, stripe have NO local send table (sent_today null). */
const LIVE_CAPS = {
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
  generated_at: ISO,
};

describe("CapsPanel", () => {
  afterEach(() => mockGet.mockReset());

  it("renders sent/cap per channel from the real payload — one read on mount, no polling", async () => {
    mockGet.mockResolvedValue(LIVE_CAPS);

    render(<CapsPanel />);

    expect(screen.getByText("Send caps today")).toBeInTheDocument();
    expect(await screen.findByText("0 / 50")).toBeInTheDocument();
    expect(screen.getByText("· 50 left")).toBeInTheDocument();
    expect(screen.getByText("0 / 4")).toBeInTheDocument();
    expect(screen.getByText("· 4 left")).toBeInTheDocument();
    expect(screen.getByText("0 / 12")).toBeInTheDocument();
    expect(screen.getByText("· 12 left")).toBeInTheDocument();
    // Every channel the policy names has its row.
    for (const entry of LIVE_CAPS.channels) {
      expect(screen.getByText(entry.channel)).toBeInTheDocument();
    }

    // ONE read, on the real path, and none after it — there is no polling.
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/channels/caps");

    // The as-of line is real (`generated_at`, pinned format) and the scope wording is OURS:
    // the company-local day. The live product's "resets 00:00 UTC" is false for this repo
    // (channel_service.day_window — the company timezone, not UTC) and must not appear.
    const asOf = screen.getByText(/^As of /);
    expect(asOf).toHaveTextContent(/\d{1,2}:\d{2} (AM|PM) · [A-Z]{3} \d{1,2}, \d{4}/);
    expect(asOf).toHaveTextContent(/company-local day/);
    expect(asOf).not.toHaveTextContent("UTC");
  });

  it("keeps an unknown counter unknown — never a fabricated zero, never a bare dash", async () => {
    mockGet.mockResolvedValue(LIVE_CAPS);

    render(<CapsPanel />);

    const markers = await screen.findAllByText("— not counted (no local send table)");
    expect(markers).toHaveLength(5); // github, google_ads, meta_ads, render, stripe
    expect(screen.getByText("· cap 6")).toBeInTheDocument();
    // The unknown channels must never read as measured zeros against their real caps.
    expect(screen.queryByText("0 / 6")).toBeNull();
    expect(screen.queryByText("0 / 10")).toBeNull();
    expect(screen.queryByText("0 / 3")).toBeNull();
    expect(screen.queryByText("0 / 5")).toBeNull();
    // The not-measured marker never stands alone (it always carries its reason).
    expect(screen.queryByText("—")).toBeNull();
  });

  it("names a missing day cap instead of inventing one", async () => {
    mockGet.mockResolvedValue({
      channels: [
        { channel: "render", sent_today: 3, cap: null, remaining: null },
        { channel: "webhooks", sent_today: null, cap: null, remaining: null },
      ],
      generated_at: ISO,
    });

    render(<CapsPanel />);

    expect(await screen.findByText("3 sent")).toBeInTheDocument();
    expect(screen.getByText("· no cap configured")).toBeInTheDocument();
    // Unknown counter AND no cap: both facts are stated; neither is a number.
    expect(
      screen.getByText("— not counted (no local send table) · no cap configured"),
    ).toBeInTheDocument();
  });

  it("renders a signed negative remaining as honestly over cap", async () => {
    mockGet.mockResolvedValue({
      channels: [{ channel: "twitter", sent_today: 14, cap: 12, remaining: -2 }],
      generated_at: ISO,
    });

    render(<CapsPanel />);

    expect(await screen.findByText("14 / 12")).toBeInTheDocument();
    const over = screen.getByText("· over cap by 2");
    expect(over).toHaveClass("text-alert");
    expect(screen.queryByText(/left/)).toBeNull();
  });

  it("names a failed read — never an empty list — and retries on demand", async () => {
    mockGet
      .mockImplementationOnce(() =>
        Promise.reject(new Error("E_DB: count for email sends today failed")),
      )
      .mockResolvedValue(LIVE_CAPS);

    render(<CapsPanel />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load channel send caps: E_DB: count for email sends today failed",
    );
    // A failure must not masquerade as the honest-empty answer, and no number may stand in.
    expect(screen.queryByText(/names no channels/)).toBeNull();
    expect(screen.queryByText("0 / 50")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    // The retry re-reads and renders the real answer; the failure state is gone.
    expect(await screen.findByText("0 / 50")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it("an empty channel list is an honest empty answer, not an error", async () => {
    mockGet.mockResolvedValue({ channels: [], generated_at: ISO });

    render(<CapsPanel />);

    expect(await screen.findByText(/names no channels/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(/^As of /)).toBeInTheDocument();
  });

  it("a malformed answer is a named failure, not an empty list", async () => {
    mockGet.mockResolvedValue({ generated_at: ISO });

    render(<CapsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load channel send caps: the endpoint answered without a channel list",
    );
    expect(screen.queryByText(/names no channels/)).toBeNull();
  });

  it("drops the leftover clause instead of inventing one when remaining is null", async () => {
    // Outside the frozen U3 contract (the route computes remaining when both sides are
    // known), but the wire type permits it — so the row must not fabricate a leftover.
    mockGet.mockResolvedValue({
      channels: [{ channel: "email", sent_today: 5, cap: 50, remaining: null }],
      generated_at: ISO,
    });

    render(<CapsPanel />);

    expect(await screen.findByText("5 / 50")).toBeInTheDocument();
    expect(screen.queryByText(/left/)).toBeNull();
    expect(screen.queryByText(/over cap/)).toBeNull();
  });

  it("keeps an unparseable generated_at as 'date unknown', never a fabricated instant", async () => {
    mockGet.mockResolvedValue({ channels: [], generated_at: null });

    render(<CapsPanel />);

    expect(await screen.findByText(/As of date unknown/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/* ------------------------------------------------------------------ page mount */

const DAY_MS = 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;

const AGENT = {
  agent_type: "finance",
  last_run_status: "completed",
  last_run_at: ago(HOUR_MS),
  tasks_today: 1,
  tasks_total: 4,
};

const SUMMARY = {
  tasks_today_total: 7,
  tasks_today_completed: 3,
  tasks_today_pending: 3,
  tasks_today_failed: 1,
  active_agents: ["finance"],
  kpis: { mrr_usd: 1234, active_customers: 12, churn_rate: 4.5 },
  last_report_date: null,
};

const TASK = {
  id: 1,
  title: "Draft the owner launch checklist",
  agent_type: "business_planning",
  status: "pending",
  priority: 3,
  created_at: ago(2 * DAY_MS),
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
  published_at: ago(2 * DAY_MS),
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

/** Every read the dashboard makes, answered per path; an unlisted path fails LOUDLY. */
function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/agents/status": [AGENT],
    "/dashboard/summary": SUMMARY,
    "/tasks": [TASK],
    "/dashboard/reports/daily": REPORT,
    "/social/posts": [POST],
    "/outreach/prospects": [PROSPECT],
    "/ads/campaigns": [CAMPAIGN],
    "/config/deepseek-key": { configured: true, source: "stored", masked: "sk-…1234" },
    "/channels/caps": LIVE_CAPS,
    // V7/S4: the Auto Mode panel (left rail) reads this; an unserved path fails LOUDLY.
    "/auto-mode": { active: false, session: null },
    // V8/C3: the chat rail (new dashboard region) reads the frozen chat history; an unserved
    // path fails LOUDLY.
    "/chat": { items: [], total: 0 },
    ...overrides,
  };
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(paths).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    const value = paths[key];
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

describe("caps panel on the dashboard (B9)", () => {
  afterEach(() => mockGet.mockReset());

  it("mounts inside the Workspace widgets column and shows real rows", async () => {
    answerWith();

    render(<DashboardPage />);

    const widgets = await screen.findByRole("region", { name: "Workspace widgets" });
    expect(within(widgets).getByText("Send caps today")).toBeInTheDocument();
    expect(await within(widgets).findByText("0 / 50")).toBeInTheDocument();
    expect(within(widgets).getByText("· 50 left")).toBeInTheDocument();
    // The neighbours in the same column still carry their own data.
    expect(within(widgets).getByText(CAMPAIGN.name)).toBeInTheDocument();
    expect(within(widgets).getByText(PROSPECT.email)).toBeInTheDocument();
  });

  it("a failing caps read cannot blank the dashboard's other regions", async () => {
    answerWith({ "/channels/caps": new Error("E_DB: count for email sends today failed") });

    render(<DashboardPage />);

    // The failure is named, with its real error code.
    expect(
      await screen.findByText(/Could not load channel send caps: E_DB/),
    ).toBeInTheDocument();
    // And every other region kept rendering.
    expect(screen.getByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByText(TASK.title)).toBeInTheDocument();
    expect(screen.getByText(POST.content)).toBeInTheDocument();
    expect(screen.getByText(PROSPECT.email)).toBeInTheDocument();
    expect(screen.getByText(CAMPAIGN.name)).toBeInTheDocument();
  });
});
