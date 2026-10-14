"use client";

import { useEffect, useState } from "react";

import { Card } from "@/components/ui";
import { api, type DashboardSummary } from "@/lib/api";

import { kpiText, moneyText, percentText } from "./format";
import { PanelError, PanelSkeleton } from "./states";

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

  useEffect(() => {
    let cancelled = false;
    api
      .get<DashboardSummary>("/dashboard/summary")
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
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
      {!loading && !error && summary ? <BusinessRows summary={summary} /> : null}
    </Card>
  );
}

function BusinessRows({ summary }: { summary: DashboardSummary }) {
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
    <dl className="space-y-1">
      {rows.map((row) => (
        <div key={row.label} className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">{row.label}</dt>
          <dd className="ui-label text-ink">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}
