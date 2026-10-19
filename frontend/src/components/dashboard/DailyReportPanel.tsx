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

  useEffect(() => {
    let cancelled = false;
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
  }, []);

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
      {!loading && error ? <PanelError label="the daily report" message={error} /> : null}
      {!loading && !error && report === null ? (
        <p className="text-sm text-ink-muted">
          No report for today yet. The daily cycle writes one after the evening run — check back
          once it has run.
        </p>
      ) : null}
      {!loading && !error && report ? <ReportBody report={report} /> : null}
      </Card>
      <DocumentsOverlay open={docsOpen} onClose={closeOverlay} />
    </>
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
