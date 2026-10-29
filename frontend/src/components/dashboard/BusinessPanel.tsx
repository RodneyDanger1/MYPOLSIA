"use client";

import { useEffect, useState } from "react";

import { Card } from "@/components/ui";
import { centsText, countText } from "@/components/finance/format";
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
 * TS-4 / F-W3-1 — the MEASURED finance block as this panel reads it (`DashboardMeasuredFinance`).
 *
 * `null` in every field means UNMEASURED (the backend serves `None`, never a fabricated 0), and a
 * measured `0` is kept as 0. `provenance` is the row's coverage word; `null` says the server did
 * not declare one — never invented here.
 */
type MeasuredBlock = {
  mrr_cents: number | null;
  active_subscribers: number | null;
  last_snapshot_date: string | null;
  provenance: string | null;
};

/** The wire block narrowed defensively; `null` when the server did not serve one at all. */
function measuredFinance(summary: DashboardSummary): MeasuredBlock | null {
  const block = summary.measured_finance;
  if (!block || typeof block !== "object" || Array.isArray(block)) return null;
  const record = block as Record<string, unknown>;
  const measuredNumber = (value: unknown): number | null =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  return {
    mrr_cents: measuredNumber(record.mrr_cents),
    active_subscribers: measuredNumber(record.active_subscribers),
    last_snapshot_date:
      typeof record.last_snapshot_date === "string" ? record.last_snapshot_date : null,
    provenance:
      typeof record.provenance === "string" && record.provenance.trim() !== ""
        ? record.provenance
        : null,
  };
}

/** One label/value line; `testId` names the node so a test pins a READING, not a text count. */
function ValueRow({
  label,
  value,
  testId,
}: {
  label: string;
  value: string | number;
  testId?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="ui-label text-ink-muted">{label}</dt>
      <dd className="ui-label text-ink" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
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
 *
 * F-W3-1 / TS-4 fixed here. `kpis` is the OWNER'S CONFIGURED bag (`company_config.kpis`, typed in
 * Settings) and the panel used to print it under the bare labels "MRR / Active customers / Churn
 * rate" — so a configured `mrr_usd: 0` read as a measured `$0.00` on a stack where
 * `GET /finance/summary` correctly answered `mrr_cents: null`. The response now declares
 * `kpis_provenance` and carries `measured_finance`; this panel keeps them strictly apart:
 *
 *   * a MEASURED section prints the ledger's MRR / active subscribers (from the newest revenue
 *     snapshot), `—` when unmeasured, plus the snapshot date and coverage word;
 *   * a CONFIGURED section prints the Settings values under labels that say "configured", headed
 *     "SETTINGS VALUES, NOT MEASURED" — a configured zero can no longer be the only thing a reader
 *     sees, and it never sits under a measured-looking label;
 *   * a server that predates TS-4 (no `measured_finance` / no `kpis_provenance`) gets an explicit
 *     "did not serve a measured finance block" sentence — not a silently-measured-looking bag.
 *
 * There is deliberately NO measured churn rate: the ledger stores `churned_today` (a count), and
 * dividing it would be arithmetic the data does not support (backend `DashboardMeasuredFinance`
 * docstring) — so churn lives only in the configured section and says so.
 */
export function BusinessPanel() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** V9 v1.1: the gross-margin row's own reading. `error` and `reason` are separate on purpose — a
   *  failed read is named as a failure, an answered-but-unmeasured margin is named as unmeasured. */
  const [margin, setMargin] = useState<{ value: number | null; reason: string | null; error: string | null }>(
    { value: null, reason: null, error: null },
  );
  /** V25/L5: the panel's own retry — a failed read stays failed until a full reload otherwise
   *  (this panel does not poll). Both reads that feed it re-issue together. */
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
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
  }, [attempt]);

  useEffect(() => {
    let cancelled = false;
    setMargin({ value: null, reason: null, error: null });
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
  }, [attempt]);

  const loading = summary === null && !error;

  return (
    <Card title="Business">
      {loading ? <PanelSkeleton rows={4} /> : null}
      {!loading && error ? (
        <PanelError
          label="the dashboard summary"
          message={error}
          onRetry={() => setAttempt((n) => n + 1)}
          retryTestId="business-panel-retry"
        />
      ) : null}
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
  const measured = measuredFinance(summary);
  const kpiProvenance =
    typeof summary.kpis_provenance === "string" && summary.kpis_provenance.trim() !== ""
      ? summary.kpis_provenance
      : null;
  const measuredUnmeasured =
    measured !== null &&
    (measured.mrr_cents === null || measured.active_subscribers === null);

  // The COUNT rows are measured from the task ledger — the labels stay bare because the values
  // really are readings (an absent count is the em dash).  The KPI bag moved out of this list.
  const rows: Array<{ label: string; value: string | number }> = [
    { label: "Tasks today", value: summary.tasks_today_total },
    { label: "Completed", value: summary.tasks_today_completed },
    { label: "Failed", value: summary.tasks_today_failed },
    { label: "Active agents", value: summary.active_agents.length },
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

      {/* ── F-W3-1 / TS-4: the MEASURED revenue reading ─────────────────────────────────────── */}
      <p className="ui-label mt-3 border-t border-line pt-2 text-ink-muted">
        Measured — from the finance ledger
      </p>
      <dl className="space-y-1" data-testid="measured-kpis">
        <ValueRow
          label="MRR (measured)"
          value={measured === null ? NOT_MEASURED : centsText(measured.mrr_cents)}
          testId="measured-mrr"
        />
        <ValueRow
          label="Active customers (measured)"
          value={measured === null ? NOT_MEASURED : countText(measured.active_subscribers)}
          testId="measured-active-customers"
        />
      </dl>
      {measured === null ? (
        <p className="mt-1 text-xs text-ink-muted" data-testid="measured-finance-unavailable">
          This server did not serve a measured finance block, so no measured revenue reading is
          shown. The configured values below are the owner&rsquo;s inputs, not a measurement.
        </p>
      ) : (
        <p className="mt-1 text-xs text-ink-muted" data-testid="measured-provenance">
          Coverage: {measured.provenance ?? "not declared by the server"} · snapshot{" "}
          {measured.last_snapshot_date ?? "none recorded"}
        </p>
      )}
      {measuredUnmeasured ? (
        <p className="mt-1 text-xs text-ink-muted" data-testid="measured-unmeasured-note">
          A dash means the ledger holds no measured value for that reading — it is not a zero. The
          configured value below is a Settings input, not a measurement.
        </p>
      ) : null}

      {/* ── F-W3-1 / TS-4: the CONFIGURED bag, labelled as configured ───────────────────────── */}
      <p className="ui-label mt-3 border-t border-line pt-2 text-ink-muted">
        Configured KPIs — Settings values, not measured
      </p>
      <dl className="space-y-1" data-testid="configured-kpis">
        <ValueRow
          label="MRR (configured)"
          value={moneyText(kpis.mrr_usd)}
          testId="configured-mrr"
        />
        <ValueRow
          label="Active customers (configured)"
          value={kpiText(kpis.active_customers)}
          testId="configured-active-customers"
        />
        <ValueRow
          label="Churn rate (configured)"
          value={percentText(kpis.churn_rate)}
          testId="configured-churn"
        />
      </dl>
      <p className="mt-1 text-xs text-ink-muted" data-testid="kpi-provenance-note">
        {kpiProvenance === "configured"
          ? "The server declares these values configured (the company profile / Settings) — no ledger measured them."
          : kpiProvenance !== null
            ? `The server declares the KPI provenance “${kpiProvenance}”; they are not a measured reading.`
            : "This server did not declare the KPI provenance; these are the values stored in the company profile, not a measurement."}
      </p>
    </>
  );
}
