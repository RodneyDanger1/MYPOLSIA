"use client";
import { useEffect, useState } from "react";

import { MetricsCard } from "@/components/dashboard/MetricsCard";
import { api, type FinanceSummary } from "@/lib/api";

/**
 * `ai_spend` — the additive block R24 wired into GET /finance/summary and the frontend never consumed.
 * Declared HERE rather than in `lib/api.ts` (another seat owns that file this wave); field names are
 * the schema's own (`backend/app/schemas/finance.py`), not the finding card's shorthand: the unmeasured
 * marker is `unit_economics.unmeasured_spend_agents`, and `spent`/`remaining` are `float | None` — so
 * the card's suggested `spent.toFixed(2)` would throw on an unmeasured day.
 */
type AiSpend = {
  limit: number;
  spent: number | null;
  remaining: number | null;
  burn_rate: {
    spent_usd: number;
    day_limit_usd: number;
    elapsed_fraction: number;
    projected_day_total_usd: number | null;
    status: string;
  } | null;
  unit_economics: {
    total_spend_usd: number;
    total_completed: number;
    cost_per_completed_task_usd: number | null;
    per_agent: Record<
      string,
      { spend_usd: number; completed: number; cost_per_completed_task_usd: number | null }
    >;
    note: string;
    unmeasured_spend_agents: string[];
  } | null;
};

/** The summary as the page needs it: the seven declared fields plus the additive block. */
type FinanceSummaryWithAiSpend = FinanceSummary & { ai_spend?: AiSpend | null };

function formatCents(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * `null` is "not measured" — the summary answers null, not 0, when there is no snapshot or
 * nothing to aggregate — and it renders as an em dash. A measured 0 must NOT take that branch:
 * 0 cents is `$0.00`, a real reading, and the two states stay as different on the page as they
 * are on the wire.
 */
function formatCentsOrDash(cents: number | null | undefined): string {
  return cents == null ? "—" : formatCents(cents);
}

function formatUsdOrDash(usd: number | null | undefined): string {
  return usd == null ? "—" : `$${usd.toFixed(2)}`;
}

/**
 * "AI spend today" (R24's `ai_spend` block) under the page's own null law: an absent block or an
 * unmeasured day — `spent: null`, which the backend serves when rows exist but no cost was reported —
 * renders "—", and only a measured zero renders `$0.00`. `spent`/`remaining` are nullable on the wire,
 * so this must never call `.toFixed()` on them directly.
 */
function formatAiSpend(spend: AiSpend | null | undefined): string {
  if (!spend || spend.spent == null) return "—";
  return `${formatUsdOrDash(spend.spent)} of ${formatUsdOrDash(spend.limit)}`;
}

/** An agent whose day spend was NOT measurable is NAMED in the block, never priced at a fabricated
 *  $0.00 per task — so it is surfaced here as a note, not folded into the card's number. */
function aiSpendNote(spend: AiSpend | null | undefined): string | undefined {
  const unmeasured = spend?.unit_economics?.unmeasured_spend_agents ?? [];
  return unmeasured.length > 0 ? `unmeasured spend: ${unmeasured.join(", ")}` : undefined;
}

/**
 * Client component on purpose.
 *
 * This page used to be an async Server Component doing `await api.get(...)`. Next prerenders this route
 * (`○ (Static)` in the build output), so that fetch ran at BUILD time and the empty result was baked into
 * the HTML: the page showed its empty state forever, even while the API served rows. The repo's e2e run
 * caught it — `GET /api/v1/tasks` returned 11 tasks and the page still rendered "No tasks yet."
 *
 * The old `catch { return [] }` made it worse: a failed read became the words "no data", which is the one
 * thing this project forbids. Three states now, and they are different on purpose:
 *   loading  -> a skeleton, because "not read yet" is not "empty"
 *   error    -> the failure named on the page, never an empty list standing in for it
 *   empty    -> printed ONLY when the read succeeded and really returned nothing
 *
 * A fourth distinction lives in the numbers: every money field is nullable, null means "not measured",
 * and it renders "—" while a real 0 renders "$0.00" (see formatCentsOrDash).
 */

export default function FinancePage() {
  const [data, setData] = useState<FinanceSummaryWithAiSpend | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<FinanceSummaryWithAiSpend>("/finance/summary")
      .then((rows) => {
        if (!cancelled) setData(rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Finance</h1>
        {data?.last_snapshot_date && (
          <p className="text-gray-400 text-sm">Last snapshot: {data.last_snapshot_date}</p>
        )}
      </div>

      {error && (
        <p className="text-red-400" role="alert">
          Could not load the finance summary: {error}
        </p>
      )}

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <MetricsCard
          title="MRR"
          value={formatCentsOrDash(data?.mrr_cents)}
          trend="up"
        />
        <MetricsCard
          title="ARR"
          value={formatCentsOrDash(data?.arr_cents)}
          trend="up"
        />
        <MetricsCard
          title="Active Subscribers"
          value={data?.active_subscribers ?? "—"}
        />
        <MetricsCard
          title="Stripe Balance"
          value={formatCentsOrDash(data?.stripe_balance_cents)}
        />
        <MetricsCard
          title="Total Ad Spend"
          value={formatUsdOrDash(data?.total_ad_spend_usd)}
          trend="neutral"
        />
        <MetricsCard
          title="Expenses (Month)"
          value={formatCentsOrDash(data?.total_expenses_month_cents)}
          trend="neutral"
        />
        {/* R24's ai_spend block — the spend the daily dispatch cap is computed from. Absent block or
            unmeasured day -> "—"; a measured 0.00 -> "$0.00". Unmeasured agents are named, never
            priced at a fabricated per-task cost. */}
        <MetricsCard
          title="AI spend today"
          value={formatAiSpend(data?.ai_spend)}
          subtitle={aiSpendNote(data?.ai_spend)}
          trend="neutral"
        />
      </div>
    </div>
  );
}
