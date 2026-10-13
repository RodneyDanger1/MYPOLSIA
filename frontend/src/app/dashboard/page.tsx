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

async function getSummary(): Promise<DashboardSummary | null> {
  try {
    return await api.get<DashboardSummary>("/dashboard/summary");
  } catch {
    return null;
  }
}

export default async function DashboardPage() {
  const summary = await getSummary();

  const kpis = summary?.kpis ?? {};
  const churn = kpiNumber(kpis.churn_rate);

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold text-white">Dashboard</h1>

      {/* Onboarding: visible only while no DeepSeek key is configured (RD9). */}
      <DeepSeekKeyOnboarding />

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricsCard
          title="Tasks Today"
          value={summary?.tasks_today_total ?? "—"}
          subtitle={`${summary?.tasks_today_completed ?? 0} completed · ${summary?.tasks_today_failed ?? 0} failed`}
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
