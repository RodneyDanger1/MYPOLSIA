/**
 * WAVE 2 / F-10 — the Daily Report panel must distinguish "not written yet" from "broken".
 *
 * The measured defect (Visuals F-10): `/dashboard` showed the sentence "No report for today yet.
 * The daily cycle writes one after the evening run — check back once it has run." while no report
 * had been written since 2026-10-20. The sentence promised a write the panel cannot see, and a
 * failed READ (the other way a report can be missing) was the only state that looked different.
 *
 * The read side is honest and unchanged (`GET /dashboard/reports/daily` answers 200 with `null`
 * for "none today" — a documented empty result, not an error). These tests pin the UI states:
 *   : not read yet  -> aria-busy skeleton, no empty claim, no alert
 *   : error         -> "Read failed" + the named refusal + Retry (data-state="error")
 *   : null          -> named "not written yet — this is not a failed read" (data-state="absent")
 *                      and NO promise that a cycle will write one
 *   : report        -> the report body (data-state="present")
 */
import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";

const mockGet = api.get as jest.Mock;

const REPORT = {
  id: 5,
  report_date: "2026-10-12",
  morning_plan: "Morning plan text",
  evening_summary: "Evening summary text",
  tasks_planned: 4,
  tasks_completed: 3,
  tasks_failed: 1,
  updated_at: "2026-10-12T22:00:00Z",
  cash_block: null,
};

beforeEach(() => mockGet.mockReset());

describe("F-10 — four readable states, one question each", () => {
  it("the empty answer is NAMED and makes no promise about the scheduler", async () => {
    mockGet.mockResolvedValue(null);
    render(<DailyReportPanel />);

    const absent = await screen.findByTestId("daily-report-absent");
    expect(absent.getAttribute("data-state")).toBe("absent");
    expect(absent).toHaveTextContent("Not written yet — this is not a failed read");
    expect(absent).toHaveTextContent(
      "The reports store answered that no report exists for today. Nothing was written for this day; the read itself succeeded.",
    );
    // WAVE 3 / R-W10-1 REPAIR (old -> new): this pinned the wave-2 sentence "This panel can see
    // reports, not the scheduler — it cannot tell whether the daily cycle is still to run, is not
    // scheduled, or stopped." The backend now serves `report_cycle` (committed claim rows), so the
    // panel no longer has to say it cannot tell the states apart. What remains true — and is
    // pinned below — is the NARROWER boundary: without a cycle state (here the summary mock
    // answers null) it must say so and never guess; with one it names the state. The old
    // promise-about-a-write copy is still banned.
    expect(await screen.findByTestId("daily-report-cycle-unavailable")).toHaveTextContent(
      /cannot tell an idle cycle from a broken one/,
    );    // The old promise must be gone: this panel cannot know when a cycle runs.
    expect(screen.queryByText(/daily cycle writes one after the evening run/)).toBeNull();
    expect(screen.queryByText(/check back once it has run/)).toBeNull();
    // An empty answer is not an error state.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByTestId("daily-report-error")).toBeNull();
  });

  it("a failed read is its OWN state — named, with the server refusal and a Retry", async () => {
    mockGet.mockRejectedValue(
      new ApiError(503, "the database is not reachable", "E_DB", "check DATABASE_URL"),
    );
    render(<DailyReportPanel />);

    const failed = await screen.findByTestId("daily-report-error");
    expect(failed.getAttribute("data-state")).toBe("error");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("E_DB: the database is not reachable — check DATABASE_URL");
    expect(screen.getByTestId("daily-report-retry")).toBeInTheDocument();
    // A failed read must never be shown as "nothing was written".
    expect(screen.queryByTestId("daily-report-absent")).toBeNull();
  });

  it("an existing report renders the report body (data-state=present)", async () => {
    mockGet.mockResolvedValue(REPORT);
    render(<DailyReportPanel />);

    const body = await screen.findByTestId("daily-report-body");
    expect(body.getAttribute("data-state")).toBe("present");
    expect(screen.getByText("Evening summary text")).toBeInTheDocument();
    expect(screen.getByTestId("daily-report-updated")).toHaveTextContent("Updated");
    expect(screen.queryByTestId("daily-report-absent")).toBeNull();
    expect(screen.queryByTestId("daily-report-error")).toBeNull();
  });

  it("before the read settles, no state is claimed (skeleton only)", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container } = render(<DailyReportPanel />);

    expect(container.querySelector(".animate-pulse")).not.toBeNull();
    expect(screen.queryByTestId("daily-report-absent")).toBeNull();
    expect(screen.queryByTestId("daily-report-error")).toBeNull();
    expect(screen.queryByTestId("daily-report-body")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

// ── R-W10-1: the state that says WHICH kind of empty this is ────────────────────────────────
const cycle = (overrides: Record<string, unknown> = {}) => ({
  state: "not_run_today",
  state_reason:
    "the newest report-cycle claim is 2026-10-19 and its report was written; no cycle has started since",
  last_claim_at: "2026-10-20T06:00:00Z",
  last_claim_cycle: "morning",
  last_claim_day: "2026-10-20",
  last_evening_claim_day: "2026-10-19",
  last_completed_report_date: "2026-10-19",
  last_skip_reason: null,
  claims_retained_since: "2026-10-16T06:00:00Z",
  scheduler_observable: false,
  scheduler_reason:
    "the report cycle is a Celery beat entry in a separate process; this API cannot observe it",
  ...overrides,
});

function absentWith(cycleBody: unknown) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/dashboard/reports/daily") return Promise.resolve(null);
    if (path === "/dashboard/summary") return Promise.resolve({ report_cycle: cycleBody });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

describe("R-W10-1 — the absent state names the cycle's own state", () => {
  it("idle is not broken: a not_run_today cycle shows why and what was last written", async () => {
    absentWith(cycle());
    render(<DailyReportPanel />);

    const block = await screen.findByTestId("daily-report-cycle");
    expect(block.getAttribute("data-state")).toBe("not_run_today");
    expect(screen.getByTestId("daily-report-cycle-headline")).toHaveTextContent(
      /idle, not broken/,
    );
    expect(screen.getByTestId("daily-report-cycle-reason")).toHaveTextContent(
      /no cycle has started since/,
    );
    expect(block).toHaveTextContent("2026-10-19");
    expect(screen.getByTestId("daily-report-cycle-scheduler")).toHaveTextContent(
      /No next-run time is claimed/,
    );
    // The absent state is still not a failed read, and no old promise came back.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/daily cycle writes one after the evening run/)).toBeNull();
  });

  it("a cycle that claimed a day and never finished is the BROKEN signature, not 'idle'", async () => {
    absentWith(
      cycle({
        state: "report_missing",
        state_reason:
          "the report cycle claimed 2026-10-20 and no evening summary exists for that day",
        last_completed_report_date: "2026-10-19",
      }),
    );
    render(<DailyReportPanel />);

    const block = await screen.findByTestId("daily-report-cycle");
    expect(block.getAttribute("data-state")).toBe("report_missing");
    expect(screen.getByTestId("daily-report-cycle-headline")).toHaveTextContent(
      /started and never finished — this is the broken signature/,
    );
    // A missing report is NOT stated as an error read: the read succeeded, the cycle did not.
    expect(screen.queryByTestId("daily-report-error")).toBeNull();
  });

  it("an unobservable scheduler is STATED, never imitated by a made-up next run", async () => {
    absentWith(cycle());
    render(<DailyReportPanel />);

    const scheduler = await screen.findByTestId("daily-report-cycle-scheduler");
    expect(scheduler).toHaveTextContent(/separate process/);
    expect(scheduler).not.toHaveTextContent(/next run at|scheduled for \d/);
    expect(screen.queryByText(/^\d{4}-\d{2}-\d{2}T.*next/)).toBeNull();
  });

  it("a server without report_cycle is NAMED as outdated — no state is invented", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/dashboard/reports/daily") return Promise.resolve(null);
      if (path === "/dashboard/summary") return Promise.resolve({ last_report_date: "2026-10-20" });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    render(<DailyReportPanel />);

    const unavailable = await screen.findByTestId("daily-report-cycle-unavailable");
    expect(unavailable).toHaveTextContent(/predates it/);
    expect(screen.queryByTestId("daily-report-cycle")).toBeNull();
  });

  it("a failed cycle read is a named unavailable state, never an absent-state claim", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/dashboard/reports/daily") return Promise.resolve(null);
      if (path === "/dashboard/summary") {
        return Promise.reject(new ApiError(503, "the database is not reachable", "E_DB"));
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    render(<DailyReportPanel />);

    const unavailable = await screen.findByTestId("daily-report-cycle-unavailable");
    expect(unavailable).toHaveTextContent("E_DB: the database is not reachable");
    expect(screen.queryByTestId("daily-report-cycle")).toBeNull();
  });

  it("an unknown state word is shown verbatim with its reason", async () => {
    absentWith(
      cycle({
        state: "clock_fault",
        state_reason: "the claimed day is in the future relative to today",
      }),
    );
    render(<DailyReportPanel />);

    expect(await screen.findByTestId("daily-report-cycle-headline")).toHaveTextContent(
      "The report cycle reported “clock_fault”",
    );
    expect(screen.getByTestId("daily-report-cycle-reason")).toHaveTextContent(
      /claimed day is in the future/,
    );
  });

  it("a present report issues NO extra summary read — the cycle state is absent-only", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/dashboard/reports/daily") return Promise.resolve(REPORT);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    render(<DailyReportPanel />);

    await screen.findByTestId("daily-report-body");
    const summaryReads = mockGet.mock.calls.filter(([path]) => path === "/dashboard/summary");
    expect(summaryReads).toHaveLength(0);
  });

  it("never_triggered states absence instead of inventing a report or a schedule", async () => {
    absentWith(
      cycle({
        state: "never_triggered",
        state_reason: "no cycle claim and no report exist in the retained window",
        last_claim_at: null,
        last_claim_cycle: null,
        last_claim_day: null,
        last_evening_claim_day: null,
        last_completed_report_date: null,
        claims_retained_since: null,
      }),
    );
    render(<DailyReportPanel />);

    const block = await screen.findByTestId("daily-report-cycle");
    expect(block).toHaveTextContent(/never — no evening summary recorded/);
    expect(block).toHaveTextContent(/no cycle claim in the retained window/);
    expect(block).toHaveTextContent(/none retained/);
    expect(block).toHaveTextContent(/Evidence retained since/);
    expect(block).toHaveTextContent("unknown");
  });
});
