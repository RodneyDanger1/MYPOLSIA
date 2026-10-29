/**
 * The report cycle's wire block, read in ONE place.
 *
 * R-W10-1 (wave 3, truthseeker) appended `report_cycle` to `GET /dashboard/summary`
 * (`DashboardReportCycle`, `backend/app/schemas/report.py`).  Every field is derived from committed
 * rows (`activity_log.cycle_claimed`, `daily_reports.evening_summary`, the last named skip) — the
 * route never authors a state, and the block declares `scheduler_observable: false` plus the reason
 * because the API cannot see the beat process.
 *
 * The block had one consumer (the Daily Report panel, which declares it locally).  F-37 (wave 5)
 * gave it a second: the Business panel's "newest report row" label.  Two parsers would be two
 * dialects of the same wire fact, so the type and the defensive parse live here and both panels
 * import them.  Nothing here invents a state: a missing block, a non-object, or an object without a
 * non-empty `state` reads `null` and the caller says the server did not declare it.
 */

export type ReportCycle = {
  state: string;
  state_reason: string | null;
  last_claim_at: string | null;
  last_claim_cycle: string | null;
  last_claim_day: string | null;
  last_evening_claim_day: string | null;
  last_completed_report_date: string | null;
  last_skip_reason: string | null;
  claims_retained_since: string | null;
  scheduler_observable: boolean;
  scheduler_reason: string | null;
};

/** The summary payload as a report-cycle block, or `null` when the server did not serve one. */
export function asReportCycle(summary: unknown): ReportCycle | null {
  if (summary === null || typeof summary !== "object" || Array.isArray(summary)) return null;
  const raw = (summary as { report_cycle?: unknown }).report_cycle;
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const cycle = raw as Record<string, unknown>;
  if (typeof cycle.state !== "string" || cycle.state.trim() === "") return null;
  const text = (value: unknown): string | null =>
    typeof value === "string" && value !== "" ? value : null;
  return {
    state: cycle.state,
    state_reason: text(cycle.state_reason),
    last_claim_at: text(cycle.last_claim_at),
    last_claim_cycle: text(cycle.last_claim_cycle),
    last_claim_day: text(cycle.last_claim_day),
    last_evening_claim_day: text(cycle.last_evening_claim_day),
    last_completed_report_date: text(cycle.last_completed_report_date),
    last_skip_reason: text(cycle.last_skip_reason),
    claims_retained_since: text(cycle.claims_retained_since),
    scheduler_observable: cycle.scheduler_observable === true,
    scheduler_reason: text(cycle.scheduler_reason),
  };
}
