/**
 * WAVE 5 / F-37 — the Business panel must not present a morning-plan-only row as a report.
 *
 * THE DEFECT (measured live on the wave-4 build): the dashboard showed
 * `LAST REPORT 2026-10-20` in the Business panel (the max `daily_reports.report_date`) while the
 * Daily Report panel beside it said `LAST COMPLETED REPORT 2026-10-19`. The 2026-10-20 row holds a
 * morning plan and **no evening summary** (live: `evening_summary: null`, `tasks_completed: 0`), so
 * the Business panel's bare "last report" label reads as a report that was written. Two panels, two
 * different facts, one misleading word.
 *
 * WHAT THIS PINS (the "state both honestly" resolution):
 *   * the row fed by `last_report_date` is labelled as a REPORT ROW (the wire's real meaning);
 *   * a separate "last completed report" row reads `report_cycle.last_completed_report_date` —
 *     the same figure the Daily Report panel shows;
 *   * when the two dates disagree, a sentence names BOTH and says a morning plan alone is a row,
 *     not a written report;
 *   * when they agree, the sentence says so instead of manufacturing a discrepancy;
 *   * a server without `report_cycle` says it did not serve the block — no date is invented;
 *   * a `report_cycle` with no completed report says "never", not a date.
 */
import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";

const mockGet = api.get as jest.Mock;

const BASE = {
  tasks_today_total: 4,
  tasks_today_completed: 1,
  tasks_today_pending: 3,
  tasks_today_failed: 0,
  active_agents: [],
  kpis: { mrr_usd: 0, active_customers: 0 },
  kpis_provenance: "configured",
};

/** `GET /dashboard/summary`'s additive report-cycle block (the fields this panel reads). */
function cycle(overrides: Record<string, unknown> = {}) {
  return {
    state: "not_run_today",
    state_reason: "the evening cycle did not claim today",
    last_claim_at: "2026-10-20T06:00:00Z",
    last_claim_cycle: "morning",
    last_claim_day: "2026-10-20",
    last_evening_claim_day: null,
    last_completed_report_date: "2026-10-19",
    last_skip_reason: null,
    claims_retained_since: "2026-10-16T06:00:00Z",
    scheduler_observable: false,
    scheduler_reason: "the API cannot observe the beat process",
    ...overrides,
  };
}

function answerWith(summary: unknown): void {
  mockGet.mockImplementation((path: string) => {
    if (path === "/dashboard/summary") return Promise.resolve(summary);
    if (path === "/finance/planning") return Promise.resolve({ margin_template: null, reasons: {} });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

it("a morning-plan-only report row is named as a row — beside the real last completed report", async () => {
  answerWith({
    ...BASE,
    // The live shape: the newest report row (2026-10-20) has a morning plan and no evening summary;
    // the last COMPLETED report is 2026-10-19.
    last_report_date: "2026-10-20",
    report_cycle: cycle({ last_completed_report_date: "2026-10-19" }),
  });

  render(<BusinessPanel />);

  await screen.findByTestId("last-report-row");
  // Both facts are on screen, each under its own name.
  expect(screen.getByTestId("last-report-row")).toHaveTextContent("2026-10-20");
  expect(screen.getByTestId("last-completed-report")).toHaveTextContent("2026-10-19");
  // The word "completed" is required for the row the Daily Report panel also shows.
  expect(screen.getByTestId("last-completed-report")).not.toHaveTextContent("2026-10-20");

  const note = screen.getByTestId("last-report-note");
  expect(note).toHaveTextContent("2026-10-20");
  expect(note).toHaveTextContent("2026-10-19");
  expect(note).toHaveTextContent(/not a completed report/i);
  expect(note).toHaveTextContent(/morning plan/i);
  expect(note).toHaveTextContent(/not a written report/i);
});

it("when the newest row IS the last completed report, the note says they agree", async () => {
  answerWith({
    ...BASE,
    last_report_date: "2026-10-19",
    report_cycle: cycle({ last_completed_report_date: "2026-10-19" }),
  });

  render(<BusinessPanel />);

  await screen.findByTestId("last-report-row");
  expect(screen.getByTestId("last-report-row")).toHaveTextContent("2026-10-19");
  expect(screen.getByTestId("last-completed-report")).toHaveTextContent("2026-10-19");
  expect(screen.getByTestId("last-report-note")).toHaveTextContent(
    /newest report row is also the last completed report/i,
  );
  expect(screen.getByTestId("last-report-note")).not.toHaveTextContent(/not a completed report/i);
});

it("a pre-R-W10-1 server says it did not serve the cycle block — no date is invented", async () => {
  answerWith({ ...BASE, last_report_date: "2026-10-20" });

  render(<BusinessPanel />);

  await screen.findByTestId("last-report-row");
  expect(screen.getByTestId("last-report-row")).toHaveTextContent("2026-10-20");
  expect(screen.getByTestId("last-completed-report")).toHaveTextContent("not declared");
  expect(screen.getByTestId("last-completed-report")).not.toHaveTextContent("2026-10-19");
  expect(screen.getByTestId("last-report-note")).toHaveTextContent(
    /did not serve a report-cycle block/i,
  );
});

it("a cycle with no completed report says never, not a fabricated date", async () => {
  answerWith({
    ...BASE,
    last_report_date: "2026-10-20",
    report_cycle: cycle({ last_completed_report_date: null }),
  });

  render(<BusinessPanel />);

  await screen.findByTestId("last-report-row");
  expect(screen.getByTestId("last-completed-report")).toHaveTextContent(/never/i);
  expect(screen.getByTestId("last-report-note")).toHaveTextContent(
    /no completed report .* recorded/i,
  );
});

it("a malformed cycle block is treated as not served, never as a date", async () => {
  answerWith({ ...BASE, last_report_date: "2026-10-20", report_cycle: { last_completed_report_date: "2026-10-19" } });

  render(<BusinessPanel />);

  await screen.findByTestId("last-report-row");
  // No `state` in the block → this build cannot read a cycle at all; it must say so.
  expect(screen.getByTestId("last-completed-report")).toHaveTextContent("not declared");
});
