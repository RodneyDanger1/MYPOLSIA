import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockRouter = { push: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import { absoluteStamp } from "@/components/dashboard/format";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { DocumentsOverlay } from "@/components/dashboard/DocumentsOverlay";
import { EmailsSection, NOTIFY_ROWS, type NotifyKey } from "@/components/settings/EmailsSection";
import { CommandPalette } from "@/components/shell/CommandPalette";
import { NotificationsBell } from "@/components/layout/NotificationsPanel";

/**
 * V22/L3 — W2 D3–D6 (working-tree snapshot, renumbered after D1/D2 went backend-side).
 *
 * D3 — the Daily Report panel never rendered `updated_at`, so a 06:00 snapshot's "0 completed"
 *      read as the day's total beside a dashboard saying 27. The stamp now names the snapshot.
 * D4 — Settings → Emails described the requeued toggle as "sends nothing yet"; the notification
 *      service routes `requeued` to that toggle (notification_service._OUTCOME_ROUTES).
 * D5 — the palette searched only the newest 50/50/20 rows while claiming "No matches." over the
 *      whole ledger. A persistent scope line states the window.
 * D6 — the bell's aria-label printed the route's 20-row cap as "20 in the last 48 hours"; at the
 *      cap it now says "up to 20".
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

type DailyReportWire = {
  id: number;
  report_date: string;
  morning_plan: string | null;
  evening_summary: string | null;
  tasks_planned: number;
  tasks_completed: number;
  tasks_failed: number;
  updated_at: string | null;
  cash_block: null;
};

function dailyReport(overrides: Partial<DailyReportWire> = {}): DailyReportWire {
  return {
    id: 1,
    report_date: "2026-10-19",
    morning_plan: "plan",
    evening_summary: null,
    tasks_planned: 8,
    tasks_completed: 0,
    tasks_failed: 0,
    updated_at: "2026-10-19T06:00:11Z",
    cash_block: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockRouter.push.mockReset();
});

describe("D3 — the daily report names when its snapshot was taken", () => {
  it("renders 'Updated <stamp>' beside the counters", async () => {
    mockApiGet.mockResolvedValue(dailyReport());

    render(<DailyReportPanel />);

    const stamp = await screen.findByTestId("daily-report-updated");
    expect(stamp).toHaveTextContent(`Updated ${absoluteStamp("2026-10-19T06:00:11Z")}`);
  });

  it("renders the stamp on each history row in the overlay", async () => {
    mockApiGet.mockResolvedValue({ items: [dailyReport()], total: 1 });

    render(<DocumentsOverlay open onClose={jest.fn()} />);

    const stamp = await screen.findByTestId("report-updated-1");
    expect(stamp).toHaveTextContent(`Updated ${absoluteStamp("2026-10-19T06:00:11Z")}`);
  });
});

describe("D4 — the requeued toggle's helper states the shipped behaviour", () => {
  it("says one email is sent when a failed attempt is requeued, not 'sends nothing yet'", () => {
    const values = Object.fromEntries(NOTIFY_ROWS.map((row) => [row.key, false])) as Record<
      NotifyKey,
      boolean
    >;

    render(<EmailsSection values={values} onChange={jest.fn()} />);

    expect(
      screen.getByText(/One email when a failed attempt is put back in the queue/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/sends nothing yet/i)).toBeNull();
  });
});

describe("D5 — the palette states the read window it actually searches", () => {
  it("shows a persistent scope line naming the newest 50 tasks, 50 runs and 20 reports", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path === "/tasks?limit=50") return Promise.resolve([]);
      if (path === "/runs?limit=50") return Promise.resolve({ items: [], total: 0 });
      if (path === "/dashboard/reports?limit=20") return Promise.resolve({ items: [], total: 0 });
      return Promise.reject(new Error(`unexpected path: ${path}`));
    });

    render(<CommandPalette />);
    await userEvent.setup().keyboard("{Control>}k{/Control}");

    expect(await screen.findByTestId("palette-read-scope")).toHaveTextContent(
      "Searches the newest 50 tasks, 50 runs and 20 reports.",
    );
  });
});

describe("D6 — the bell does not print the 20-row cap as a window count", () => {
  it("says 'up to 20' when the served count is exactly the cap", async () => {
    const items = Array.from({ length: 20 }, (_, index) => ({
      id: `task:${index}`,
      kind: "task_completed",
      title: `task ${index}`,
      task_id: index,
      created_at: "2026-10-19T06:00:00Z",
    }));
    mockApiGet.mockResolvedValue({ items, count: 20 });

    render(<NotificationsBell />);

    const bell = await screen.findByTestId("topbar-notifications");
    expect(bell).toHaveAccessibleName(/Notifications, up to 20 in the last 48 hours/);
  });
});
