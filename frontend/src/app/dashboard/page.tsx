"use client";
import { useEffect, useState } from "react";

import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { AgentStatusGrid } from "@/components/dashboard/AgentStatusGrid";
import { DeepSeekKeyOnboarding } from "@/components/dashboard/DeepSeekKeyOnboarding";
import { MetricsCard } from "@/components/dashboard/MetricsCard";
import { api, type DashboardSummary } from "@/lib/api";

/**
 * KPI display helpers.
 *
 * `DashboardSummary.kpis` is `Record<string, unknown>` on purpose — the API carries whatever the
 * company configured and the type must not pretend otherwise. Every read is therefore `unknown`, and
 * feeding `unknown` straight into a `string | number` prop is a type error: it was exactly this, on
 * the "Active Customers" card, that made `next build` fail, which means the documented frontend image
 * could never have built. These helpers narrow it honestly — a displayable value is shown, a missing
 * one becomes the em-dash fallback, and anything else is stringified rather than rendered as
 * "[object Object]".
 */
function kpiText(value: unknown, fallback = "—"): string | number {
  if (typeof value === "string" || typeof value === "number") return value;
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function kpiNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/**
 * Client component on purpose.
 *
 * This page used to be an async Server Component doing `await api.get(...)` with `catch { return null }`.
 * Next prerenders this route (`○ (Static)` in the build output), so that fetch ran at BUILD time and the
 * API's answer at image-build time was baked into the HTML: the KPI cards could never change again, while
 * the two client children below (AgentStatusGrid / ActivityFeed) kept updating — so a dashboard frozen at
 * the build-time answer reads as "the agents did nothing".
 *
 * The old silent catch made it worse: a failed read became em dashes, which is the one thing this project
 * forbids (the tasks/finance/ads/social/outreach/memory pages carry the same note and the same fix; this
 * was the last page still in the old shape). Three states now, and they are different on purpose:
 *   loading -> a skeleton, because "not read yet" is neither "empty" nor "no data"
 *   error   -> the failure named on the page, never dashes standing in for it
 *   data    -> rendered; only a genuinely absent KPI value keeps its em-dash fallback
 */
export default function DashboardPage() {
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

  const kpis = summary?.kpis ?? {};
  const churn = kpiNumber(kpis.churn_rate);

  const loading = summary === null && !error;

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold text-white">Dashboard</h1>

      {/* Onboarding: visible only while no DeepSeek key is configured (RD9). */}
      <DeepSeekKeyOnboarding />

      {error && (
        <p className="text-red-400" role="alert">
          Could not load the dashboard summary: {error}
        </p>
      )}

      {/* KPI Cards — a skeleton while the read is in flight; the values themselves are only ever
          rendered from a successful response (a stale build-time number is not "data"). */}
      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4" aria-busy="true">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-24 bg-gray-800 rounded-lg animate-pulse" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricsCard
            title="Tasks Today"
            value={summary?.tasks_today_total ?? "—"}
            subtitle={
              summary
                ? `${summary.tasks_today_completed} completed · ${summary.tasks_today_failed} failed`
                : undefined
            }
          />
          <MetricsCard
            title="MRR"
            value={kpis.mrr_usd == null ? "—" : `$${kpiText(kpis.mrr_usd)}`}
            trend="up"
          />
          <MetricsCard
            title="Active Customers"
            value={kpiText(kpis.active_customers)}
          />
          <MetricsCard
            title="Churn Rate"
            value={churn == null ? "—" : `${churn}%`}
            trend={churn != null && churn > 5 ? "down" : "neutral"}
          />
        </div>
      )}

      {/* Agent Grid + Activity Feed */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
            Agent Status
          </h2>
          <AgentStatusGrid />
        </div>
        <div className="h-96">
          <h2 className="text-sm font-semibold text-gray-400 mb-3 uppercase tracking-wider">
            Live Activity
          </h2>
          <ActivityFeed />
        </div>
      </div>
    </div>
  );
}
