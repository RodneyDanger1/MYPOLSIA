"use client";

import { useEffect, useRef, useState } from "react";

import { Card } from "@/components/ui";
import { api } from "@/lib/api";

import { NOT_MEASURED, absoluteStamp } from "./format";
// V9/B5: the cash/tax block prints money, so it uses the ONE house money formatter the finance
// surfaces share ($1,234.56 / em dash for null) rather than a private dialect.
import { usdText } from "@/components/finance/format";

import { DocumentsOverlay } from "./DocumentsOverlay";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/**
 * The wire shape of `GET /dashboard/reports/daily` (`DailyReportResponse` in
 * `backend/app/schemas/report.py`). The endpoint answers **200 with `null`** when no report exists
 * for the day — a documented empty result, not an error — so this component distinguishes three
 * body states: `undefined` = not read yet, `null` = read and none exists, object = the report.
 * Declared locally because the shared api module (another lane's file) does not carry it.
 */
type DailyReport = {
  id: number;
  report_date: string;
  morning_plan: string | null;
  evening_summary: string | null;
  tasks_planned: number;
  tasks_completed: number;
  tasks_failed: number;
  updated_at: string | null;
  /** V9/B2's additive cash/tax block (the daily report builder's new field). Optional AND
   *  nullable: an older report row predates the block and a fresh builder that could not measure
   *  any input serves null — both mean "not measured", and neither may render as $0.00. */
  cash_block?: CashBlock | null;
};

/**
 * R-W10-1 — the report cycle's state, appended by truthseeker to `GET /dashboard/summary`
 * (`DashboardReportCycle`, `backend/app/schemas/report.py:82-145`). Every field is read from
 * committed rows (`activity_log.cycle_claimed`, `daily_reports.evening_summary`, the last named
 * skip) — the shape carries `scheduler_observable: false` plus the reason, because the API cannot
 * see the beat process. Declared locally because the shared api module does not carry it.
 */
type ReportCycle = {
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

type CycleRead =
  | { kind: "loading" }
  /** The summary read failed, or carried no `report_cycle` (an older server): the panel states
   *  which, and does NOT invent a state. */
  | { kind: "unavailable"; reason: string }
  | { kind: "ready"; cycle: ReportCycle };

/** The state vocabulary the backend derives (`DashboardReportCycle` docstring), mapped to one
 *  headline each. An UNKNOWN word is rendered verbatim by `CycleBlock` — never folded into a
 *  happier state. */
const CYCLE_HEADLINES: Record<string, string> = {
  never_triggered: "The report cycle has never started on this deployment",
  not_run_today: "The report cycle has not run today — idle, not broken",
  report_running: "The report cycle is in progress today",
  report_written_today: "The report cycle wrote today's report",
  report_missing: "The report cycle started and never finished — this is the broken signature",
  unknown: "The report cycle's state could not be determined",
};

function asReportCycle(summary: unknown): ReportCycle | null {
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

/** The cash block's wire shape (plan §9 B2): money inputs are nullable-with-a-reason. Money here is
 *  DOLLARS, not cents — the house names every cents field `*_cents` (mrr_cents, amount_cents,
 *  stripe_balance_cents), and this block's fields carry no such suffix. */
type CashBlock = {
  cash_on_hand: number | null;
  in_30d: number | null;
  out_30d: number | null;
  tax_set_aside: number | null;
  next_tax_dates: string[] | null;
  safe_harbor_note: string | null;
  /** B2's tax guardrails, additive: the per-deployment sales-tax reading and a field-level reason. */
  sales_tax_note?: string | null;
  cash_reason?: string | null;
  reason?: string | null;
};

/**
 * Middle column, bottom: the Documents slot of the live dashboard.
 *
 * F-10 (wave 2): the panel's states are named and distinguishable, and the absent state makes no
 * promise. `undefined` = not read yet (aria-busy skeleton), `error` = "Read failed" with the real
 * server refusal and a Retry, `null` = the reports store's own EMPTY ANSWER ("not written yet —
 * this is not a failed read"), object = the report. The old absent copy said "the daily cycle
 * writes one after the evening run — check back once it has run", which promised a write this
 * panel cannot see: the live box had no report since 2026-10-20 and the sentence still rendered.
 * Wave 2 therefore removed the promise and stated the panel's boundary ("it can see reports, not
 * the scheduler").
 *
 * R-W10-1 (wave 3, truthseeker): the boundary is now narrower and the panel can say WHY the day
 * is empty. `GET /dashboard/summary` carries an additive `report_cycle` block derived from
 * committed claim rows (`activity_log.cycle_claimed`, `daily_reports.evening_summary`, the last
 * named skip): `not_run_today` ("idle, not broken"), `report_running`, `report_missing` ("started
 * and never finished — the broken signature"), `never_triggered`, `report_written_today`,
 * `unknown`. It is read ONLY when today's report is absent (the state that needs explaining), so
 * a dashboard with a report issues no extra read. The block still declares
 * `scheduler_observable: false` with its reason — the API cannot see the beat process, so no
 * next-run time is claimed here either. An older server (no `report_cycle`) or a failed summary
 * read renders a NAMED unavailable line, never a guessed state.
 *
 * It is titled **Daily Report** and not "Documents" on purpose — this deployment has no
 * documents store (no documents router exists in `backend/app/api/v1/`), and a "Documents"
 * panel over a report would be mislabelling. What DID ship in V8 (C5) is the reports LIST read —
 * `GET /dashboard/reports[?limit=N]` answering `{items, total}`
 * (`backend/app/api/v1/dashboard.py:145`) — so the panel's VIEW ALL opener below opens
 * `DocumentsOverlay` (ALL / DOCUMENTS / REPORTS over that list). The daily report itself is read
 * here, with an instructing empty state when today's report has not been written yet.
 */
export function DailyReportPanel() {
  const [report, setReport] = useState<DailyReport | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [docsOpen, setDocsOpen] = useState(false);
  const viewAllRef = useRef<HTMLButtonElement>(null);
  // V24/L5: the failure's Retry re-issues this read.
  const [attempt, setAttempt] = useState(0);
  /** R-W10-1: the cycle state, read only once today's report is known to be ABSENT. */
  const [cycle, setCycle] = useState<CycleRead | null>(null);

  useEffect(() => {
    let cancelled = false;
    setReport(undefined);
    setError(null);
    api
      .get<DailyReport | null>("/dashboard/reports/daily")
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  // R-W10-1: the absent state needs the cycle's own evidence to say whether it is idle or broken.
  // A present report needs nothing extra, so this read happens ONLY on the absent path (report
  // === null, after the daily read settled), and it is re-issued by the same Retry control.
  useEffect(() => {
    if (report !== null) {
      setCycle(null);
      return;
    }
    let cancelled = false;
    setCycle({ kind: "loading" });
    api
      .get<unknown>("/dashboard/summary")
      .then((summary) => {
        if (cancelled) return;
        const parsed = asReportCycle(summary);
        setCycle(
          parsed === null
            ? {
                kind: "unavailable",
                reason:
                  "the dashboard summary carried no report-cycle state (this server predates it)",
              }
            : { kind: "ready", cycle: parsed },
        );
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setCycle({ kind: "unavailable", reason: readErrorText(e) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [report, attempt]);

  const loading = report === undefined && !error;

  /** Focus returns to the opener on EVERY close path (Esc / ✕ / scrim-click) — the task sheet's
   *  grammar, owned here because this panel owns the opener button that focus came from. */
  const closeOverlay = () => {
    setDocsOpen(false);
    viewAllRef.current?.focus();
  };

  return (
    <>
      <Card
        title="Daily Report"
        action={
          // The label carries no count on purpose (wave V8b-D3): the live's "VIEW ALL (6)"
          // counter lives INSIDE the overlay as the shown-of-total line, where it comes from the
          // server's real `total` field instead of a number this panel never reads.
          <button
            ref={viewAllRef}
            type="button"
            className="ui-label text-link hover:underline"
            aria-expanded={docsOpen}
            aria-haspopup="dialog"
            onClick={() => setDocsOpen(true)}
          >
            VIEW ALL
          </button>
        }
      >
      {loading ? <PanelSkeleton rows={3} /> : null}
      {!loading && error ? (
        // F-10 (wave 2): the FAILED READ is its own state and its own name ("Read failed") — it was
        // never the same thing as "no report was written", and the two used to be one grey sentence.
        <div data-testid="daily-report-error" data-state="error">
          <PanelError
            label="the daily report"
            message={error}
            onRetry={() => setAttempt((n) => n + 1)}
            retryTestId="daily-report-retry"
          />
        </div>
      ) : null}
      {!loading && !error && report === null ? (
        // F-10 (wave 2): the ABSENT state, named honestly. The endpoint answered 200 with `null`
        // (the documented "no report for the day" — not an error), so this says exactly that.
        // R-W10-1 (wave 3): the cycle block below then says WHICH kind of empty this is — idle,
        // running, never triggered, or started-and-never-finished — from committed claim rows.
        <div data-testid="daily-report-absent" data-state="absent" className="space-y-2">
          <p className="ui-label text-ink">Not written yet — this is not a failed read</p>
          <p className="text-sm text-ink-muted" data-testid="daily-report-absent-copy">
            The reports store answered that no report exists for today. Nothing was written for
            this day; the read itself succeeded.
          </p>
          <CycleBlock read={cycle} />
          <p className="ui-label text-ink-muted">
            VIEW ALL lists every report that HAS been written, with its date.
          </p>
        </div>
      ) : null}
      {!loading && !error && report ? (
        <div data-testid="daily-report-body" data-state="present">
          <ReportBody report={report} />
        </div>
      ) : null}
      </Card>
      <DocumentsOverlay open={docsOpen} onClose={closeOverlay} />
    </>
  );
}

/**
 * R-W10-1 — the report cycle's state, rendered INSIDE the absent state only.
 *
 * Three reads are kept distinct: still reading (`daily-report-cycle-loading`), unavailable
 * (`daily-report-cycle-unavailable` — the summary read failed or this server predates
 * `report_cycle`; the panel says which and does NOT invent a state), and the derived state
 * (`daily-report-cycle`, `data-state` = the server's own word). The headline is a word-for-word
 * mapping of the backend vocabulary (`DashboardReportCycle` docstring); an unknown word is shown
 * verbatim, never folded into a happier state. Every evidence row is a committed fact; absence is
 * stated ("none retained"), never left blank or filled with a guess. The scheduler line always
 * states the boundary: the API cannot observe the beat process, so no next-run time is claimed.
 */
function CycleBlock({ read }: { read: CycleRead | null }) {
  if (read === null || read.kind === "loading") {
    return (
      <p
        className="ui-label text-ink-muted"
        aria-busy="true"
        data-testid="daily-report-cycle-loading"
      >
        Reading the report cycle&rsquo;s state…
      </p>
    );
  }
  if (read.kind === "unavailable") {
    return (
      <div
        data-testid="daily-report-cycle-unavailable"
        data-state="unavailable"
        className="space-y-1"
      >
        <p className="ui-label text-ink">Cycle state unavailable</p>
        <p className="text-sm text-ink-muted">{read.reason}</p>
        <p className="ui-label text-ink-muted">
          Without the cycle state this panel cannot tell an idle cycle from a broken one — it
          reports that boundary rather than guessing a state.
        </p>
      </div>
    );
  }
  const { cycle } = read;
  const headline =
    CYCLE_HEADLINES[cycle.state] ?? `The report cycle reported “${cycle.state}”`;
  const claimLine =
    cycle.last_claim_at !== null
      ? `${cycle.last_claim_at}${cycle.last_claim_cycle ? ` (${cycle.last_claim_cycle})` : ""}${
          cycle.last_claim_day ? ` for ${cycle.last_claim_day}` : ""
        }`
      : "no cycle claim in the retained window";
  return (
    <div data-testid="daily-report-cycle" data-state={cycle.state} className="space-y-1 border border-line bg-surface-sunk p-2">
      <p className="ui-label text-ink" data-testid="daily-report-cycle-headline">
        {headline}
      </p>
      {cycle.state_reason !== null ? (
        <p className="text-sm text-ink-muted" data-testid="daily-report-cycle-reason">
          {cycle.state_reason}
        </p>
      ) : null}
      <dl className="ui-label space-y-0.5 text-ink-muted">
        <div className="flex flex-wrap justify-between gap-2">
          <dt>Last completed report</dt>
          <dd className="text-ink">
            {cycle.last_completed_report_date ?? "never — no evening summary recorded"}
          </dd>
        </div>
        <div className="flex flex-wrap justify-between gap-2">
          <dt>Last cycle claim</dt>
          <dd className="text-ink">{claimLine}</dd>
        </div>
        <div className="flex flex-wrap justify-between gap-2">
          <dt>Last report-cycle (evening) claim</dt>
          <dd className="text-ink">{cycle.last_evening_claim_day ?? "none retained"}</dd>
        </div>
        {cycle.last_skip_reason !== null ? (
          <div className="flex flex-wrap justify-between gap-2">
            <dt>Last named skip</dt>
            <dd className="text-ink">{cycle.last_skip_reason}</dd>
          </div>
        ) : null}
        <div className="flex flex-wrap justify-between gap-2">
          <dt>Evidence retained since</dt>
          <dd className="text-ink">{cycle.claims_retained_since ?? "unknown"}</dd>
        </div>
      </dl>
      <p className="ui-label text-ink-muted" data-testid="daily-report-cycle-scheduler">
        {cycle.scheduler_observable
          ? "The scheduler is observable for this deployment."
          : `No next-run time is claimed: ${
              cycle.scheduler_reason ??
              "the scheduler runs in a separate process this API cannot observe"
            }`}
      </p>
    </div>
  );
}

function ReportBody({ report }: { report: DailyReport }) {
  const rows: Array<{ label: string; value: string | number }> = [
    { label: "Report date", value: report.report_date },
    { label: "Planned", value: report.tasks_planned },
    { label: "Completed", value: report.tasks_completed },
    { label: "Failed", value: report.tasks_failed },
  ];
  const summary = report.evening_summary ?? report.morning_plan;

  return (
    <div>
      <dl className="space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-3">
            <dt className="ui-label text-ink-muted">{row.label}</dt>
            <dd className="ui-label text-ink">{row.value}</dd>
          </div>
        ))}
      </dl>
      {/* W2 D3: the counters are a SNAPSHOT (the daily cycle writes the report before the day's
          work lands). Without its stamp, "Completed 0" said beside a dashboard counting 27 read as
          the day's total. `updated_at` is the server's own write time, already on the wire. */}
      <p className="ui-label mt-1 text-ink-muted" data-testid="daily-report-updated">
        Updated {absoluteStamp(report.updated_at)}
      </p>
      {summary ? (
        <>
          <h3 className="ui-label mt-3 text-ink-muted">
            {report.evening_summary ? "Evening summary" : "Morning plan"}
          </h3>
          <p className="mt-1 line-clamp-4 text-sm leading-relaxed text-ink">{summary}</p>
        </>
      ) : (
        <p className="mt-3 text-sm text-ink-muted">
          No summary text was recorded for this report.
        </p>
      )}
      <CashTaxBlock block={report.cash_block ?? null} />
    </div>
  );
}

/** The per-field reason a null money input carries on the wire (B2's "null + reason" rule). The
 *  block may name one reason for the whole block or one per field; whichever is present is what the
 *  reader gets, and when neither is there the sentence says exactly that. */
function fieldReason(block: CashBlock, field: string): string | null {
  const perField = (block as unknown as Record<string, unknown>)[`${field}_reason`];
  if (typeof perField === "string" && perField !== "") return perField;
  if (typeof block.cash_reason === "string" && block.cash_reason !== "") return block.cash_reason;
  if (typeof block.reason === "string" && block.reason !== "") return block.reason;
  return null;
}

/**
 * The cash/tax block (plan §9 B2): cash on hand, the next 30 days in and out, what is set aside for
 * tax, the four quarterly dates and the safe-harbor note — each read straight off the daily report.
 *
 * Null-honest: an input the builder could not measure renders the em dash plus the reason, NEVER a
 * zero (a `$0.00` here would read as "the business has no cash" — a different and much worse
 * claim). When the whole block is null (no report builder ran, or none of its inputs exist) the
 * section says so instead of disappearing, because a silently missing money panel is the failure
 * mode this project keeps fixing.
 *
 * The tax line is PERSISTENT: it renders with the block in BOTH states — populated and unmeasured —
 * so the one surface that shows money earmarked for tax always carries its disclaimer.
 */
function CashTaxBlock({ block }: { block: CashBlock | null }) {
  const moneyRows: ReadonlyArray<{ label: string; field: keyof CashBlock; testId: string }> = [
    { label: "Cash on hand", field: "cash_on_hand", testId: "cash-on-hand" },
    { label: "In — next 30 days", field: "in_30d", testId: "cash-in-30d" },
    { label: "Out — next 30 days", field: "out_30d", testId: "cash-out-30d" },
    { label: "Set aside for tax", field: "tax_set_aside", testId: "cash-tax-set-aside" },
  ];

  if (block === null) {
    return (
      <section className="mt-3 border-t border-line pt-2" data-testid="cash-tax-block">
        <h3 className="ui-label text-ink-muted">Cash &amp; tax</h3>
        <p className="mt-1 text-sm text-ink-muted" data-testid="cash-block-unmeasured">
          Cash block not measured — no cash inputs have been measured for this report yet. The
          builder serves null rather than 0 for every input it could not read.
        </p>
        <TaxDisclaimer />
      </section>
    );
  }

  const dates = Array.isArray(block.next_tax_dates) ? block.next_tax_dates : [];

  return (
    <section className="mt-3 border-t border-line pt-2" data-testid="cash-tax-block">
      <h3 className="ui-label text-ink-muted">Cash &amp; tax</h3>
      <dl className="mt-1 space-y-1">
        {moneyRows.map(({ label, field, testId }) => {
          const reason = block[field] === null ? fieldReason(block, String(field)) : null;
          return (
            <div key={testId} className="flex items-baseline justify-between gap-3">
              <dt className="ui-label text-ink-muted">{label}</dt>
              <dd className="ui-label text-ink" data-testid={testId}>
                {usdText(block[field] as number | null | undefined)}
                {reason ? <span className="ml-1 text-ink-muted">{reason}</span> : null}
              </dd>
            </div>
          );
        })}
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Next tax dates</dt>
          <dd className="ui-label text-ink" data-testid="cash-next-tax-dates">
            {dates.length > 0 ? dates.join(" · ") : NOT_MEASURED}
          </dd>
        </div>
      </dl>
      <p className="mt-1 text-sm text-ink-muted" data-testid="cash-safe-harbor">
        {block.safe_harbor_note && block.safe_harbor_note !== ""
          ? block.safe_harbor_note
          : "No safe-harbor note was recorded for this report."}
      </p>
      {block.sales_tax_note && block.sales_tax_note !== "" ? (
        <p className="mt-1 text-sm text-ink-muted" data-testid="sales-tax-note">
          {block.sales_tax_note}
        </p>
      ) : null}
      <TaxDisclaimer />
    </section>
  );
}

/** One string, one place: the disclaimer cannot drift. */
function TaxDisclaimer() {
  return (
    <p className="ui-label mt-1 text-ink-muted" data-testid="tax-disclaimer">
      Not tax advice — verify with MN Revenue.
    </p>
  );
}
