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
    expect(absent).toHaveTextContent(/cannot tell whether the daily cycle is still to run, is not scheduled, or stopped/);
    // The old promise must be gone: this panel cannot know when a cycle runs.
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
