"use client";

import { useEffect, useRef, useState } from "react";

import { Card } from "@/components/ui";
import { api } from "@/lib/api";

import { DocumentsOverlay } from "./DocumentsOverlay";
import { PanelError, PanelSkeleton } from "./states";

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
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
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
    </div>
  );
}
