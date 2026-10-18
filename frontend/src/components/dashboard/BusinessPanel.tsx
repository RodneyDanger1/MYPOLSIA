"use client";

import { useEffect, useState } from "react";

import { Card } from "@/components/ui";
import { api, type DashboardSummary } from "@/lib/api";

import { NOT_MEASURED, kpiText, moneyText, percentText } from "./format";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/** The planning view's margin template, as `GET /finance/planning` serves it (plan §9 B2). Typed
 *  for the fields this panel reads; every input is `number | null` (unmeasured = null + the reason
 *  the endpoint names, never 0). The cause lives in the payload's `reasons` map keyed 1:1 to the
 *  field (v14 S3-05 — the same contract the Finance page's Planning panel reads). */
type PlanningView = {
  margin_template?: { margin?: number | null } | null;
  reasons?: Record<string, string> | null;
};

/** A margin reading as a percentage. B2's contract does not pin the unit, so the reading is
 *  disambiguated the only honest way available: a value inside [-1, 1] is a fraction (0.38 → 38.0%)
 *  and anything else is already a percentage. Both land on the same rendered truth; a value neither
 *  convention can express is not a reading at all and never prints. */
function marginPercentText(value: number): string {
  if (!Number.isFinite(value)) return NOT_MEASURED;
  // A fraction (|v| ≤ 1) is scaled and shown to one decimal; a value that already IS a percentage
  // is printed exactly as the server sent it — no false precision is added either way.
  if (Math.abs(value) <= 1) return `${(value * 100).toFixed(1)}%`;
  return `${value}%`;
}

/**
 * The left rail's business mini-panel — the live `Business` widget's slot, fed by
 * `GET /dashboard/summary` (today's task counts, the active agents, the company KPIs and the date
 * of the last daily report, all from that one endpoint).
 *
 * Q-09 fixed here. The audit's finding: the old dashboard rendered its error banner AND a full row
 * of em-dash KPI cards beneath it, so a failed read still drew a plausible all-empty dashboard. In
 * this panel the failure REPLACES the value rows — with `error` set, no label/value row is rendered
 * at all. The em dash survives only for a genuinely absent KPI on a SUCCESSFUL read, where "not
 * measured" is the truth (a real 0 still renders as 0).
 */
export function BusinessPanel() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** V9 v1.1: the gross-margin row's own reading. `error` and `reason` are separate on purpose — a
   *  failed read is named as a failure, an answered-but-unmeasured margin is named as unmeasured. */
  const [margin, setMargin] = useState<{ value: number | null; reason: string | null; error: string | null }>(
    { value: null, reason: null, error: null },
  );

  useEffect(() => {
    let cancelled = false;
    api
      .get<DashboardSummary>("/dashboard/summary")
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .get<PlanningView>("/finance/planning")
      .then((view) => {
        if (cancelled) return;
        const raw = view?.margin_template?.margin;
        // The cause is read from the payload's `reasons` map, keyed to the field it explains
        // (v14 S3-05): the margin key is the specific one, the price key the next-most-specific.
        const rawReasons = view?.reasons;
        const reasons: Record<string, string> =
          rawReasons !== null && typeof rawReasons === "object" && !Array.isArray(rawReasons)
            ? rawReasons
            : {};
        const named =
          reasons["margin_template.margin"] ?? reasons["margin_template.price"] ?? null;
        setMargin({
          value: typeof raw === "number" && Number.isFinite(raw) ? raw : null,
          reason: typeof named === "string" && named.trim() !== "" ? named : null,
          error: null,
        });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setMargin({ value: null, reason: null, error: readErrorText(e) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loading = summary === null && !error;

  return (
    <Card title="Business">
      {loading ? <PanelSkeleton rows={4} /> : null}
      {!loading && error ? <PanelError label="the dashboard summary" message={error} /> : null}
      {!loading && !error && summary ? <BusinessRows summary={summary} margin={margin} /> : null}
    </Card>
  );
}

function BusinessRows({
  summary,
  margin,
}: {
  summary: DashboardSummary;
  margin: { value: number | null; reason: string | null; error: string | null };
}) {
  // The response model defaults `kpis` to `{}` server-side; the belt-and-braces `?? {}` only guards
  // against a transport that dropped the key entirely (a missing group must not throw).
  const kpis: Record<string, unknown> = summary.kpis ?? {};
  const rows: Array<{ label: string; value: string | number }> = [
    { label: "Tasks today", value: summary.tasks_today_total },
    { label: "Completed", value: summary.tasks_today_completed },
    { label: "Failed", value: summary.tasks_today_failed },
    { label: "Active agents", value: summary.active_agents.length },
    { label: "MRR", value: moneyText(kpis.mrr_usd) },
    { label: "Active customers", value: kpiText(kpis.active_customers) },
    { label: "Churn rate", value: percentText(kpis.churn_rate) },
    // `last_report_date` is the max report date the summary read found; a null means no report row
    // exists at all, which is a fact to state — not a dash to hide behind.
    { label: "Last report", value: summary.last_report_date ?? "no report yet" },
  ];

  return (
    <>
      <dl className="space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-3">
            <dt className="ui-label text-ink-muted">{row.label}</dt>
            <dd className="ui-label text-ink">{row.value}</dd>
          </div>
        ))}
        {/* V9 v1.1 — the GROSS MARGIN row. Its input is the planning view's margin template
            (`GET /finance/planning`), not the dashboard summary: the margin is a planning-input
            reading (price, AI cost, human hours × rate), so until those inputs exist the row is the
            em dash PLUS the reason, and a read that failed is named here — never a made-up 0%. */}
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Gross margin</dt>
          <dd className="ui-label text-ink" data-testid="gross-margin-value">
            {margin.value === null ? NOT_MEASURED : marginPercentText(margin.value)}
          </dd>
        </div>
      </dl>
      {margin.error !== null ? (
        <p className="mt-2 text-xs text-ink-muted" data-testid="margin-read-error">
          Gross margin could not be read: {margin.error}
        </p>
      ) : null}
      {margin.error === null && margin.value === null ? (
        <p className="mt-2 text-xs text-ink-muted" data-testid="margin-unmeasured">
          {/* The sentence states the truth of the row, then the server's own reason when it named
              one — the sentence is not the reason, it is what the dash means. */}
          Gross margin is not measured yet — {margin.reason ?? "the planning inputs have not been measured yet"}.
        </p>
      ) : null}
    </>
  );
}
