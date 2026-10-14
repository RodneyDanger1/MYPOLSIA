import { NOT_MEASURED } from "@/components/dashboard/format";

import type { FinanceSummary } from "@/lib/api";

import { centsText, countText, usdText } from "./format";

/**
 * The summary's presentation logic, pure: which cards exist, what each one prints, and whether the
 * whole summary is in the idle state. Kept data-in / strings-out so the law — null is "not
 * measured", a measured 0 is `$0.00` — is in one testable place and the grid component stays dumb.
 */

export type SummaryCardSpec = {
  key: string;
  label: string;
  value: string;
  note?: string;
};

/** "AI spend today" (R24's `ai_spend` block). An absent block or an unmeasured day (`spent: null`,
 *  which the backend serves when rows exist but no cost was reported) is the marker; only a
 *  measured zero renders `$0.00`. */
export function aiSpendValue(spend: FinanceSummary["ai_spend"]): string {
  if (!spend) return NOT_MEASURED;
  const spent = usdText(spend.spent);
  if (spent === NOT_MEASURED) return NOT_MEASURED;
  return `${spent} of ${usdText(spend.limit)}`;
}

/** An agent whose day spend was NOT measurable is NAMED, never priced at a fabricated $0.00 per
 *  task — so it is a note on the card, not folded into its number. */
export function aiSpendNote(spend: FinanceSummary["ai_spend"]): string | undefined {
  const unmeasured = spend?.unit_economics?.unmeasured_spend_agents ?? [];
  return unmeasured.length > 0 ? `unmeasured spend: ${unmeasured.join(", ")}` : undefined;
}

/** The seven cards, AI spend leading (spec OPEN DECISION 3: it is the measured, unique number). */
export function summaryCards(summary: FinanceSummary): SummaryCardSpec[] {
  return [
    {
      key: "ai-spend",
      label: "AI spend today",
      value: aiSpendValue(summary.ai_spend),
      note: aiSpendNote(summary.ai_spend),
    },
    { key: "mrr", label: "MRR", value: centsText(summary.mrr_cents) },
    { key: "arr", label: "ARR", value: centsText(summary.arr_cents) },
    { key: "subscribers", label: "Active Subscribers", value: countText(summary.active_subscribers) },
    { key: "stripe-balance", label: "Stripe Balance", value: centsText(summary.stripe_balance_cents) },
    { key: "ad-spend", label: "Total Ad Spend", value: usdText(summary.total_ad_spend_usd) },
    { key: "expenses", label: "Expenses (Month)", value: centsText(summary.total_expenses_month_cents) },
  ];
}

/**
 * The idle state: a successful read on which NOTHING has been measured yet — the live product's
 * own `Not running yet` moment. True only when the snapshot side is absent (no snapshot date and
 * all six money/count fields null) AND the AI-spend ledger shows no agent-run signal at all: an
 * absent day is a measured `0.0` with empty unit economics, which is "no run has happened", and
 * rendering a grid of dashes + `$0.00` for it would read as a broken meter instead of a true
 * "not started". The moment anything is measured — a snapshot, spend above zero, a completion, a
 * per-agent row, or a NAMED unmeasured agent — this is false and the grid renders, dashes and
 * all, because on a successful read the em dash is the truth for an absent individual value.
 */
export function isIdleSummary(summary: FinanceSummary): boolean {
  const snapshotAbsent =
    summary.last_snapshot_date == null &&
    summary.mrr_cents == null &&
    summary.arr_cents == null &&
    summary.active_subscribers == null &&
    summary.total_ad_spend_usd == null &&
    summary.total_expenses_month_cents == null &&
    summary.stripe_balance_cents == null;
  if (!snapshotAbsent) return false;

  const spend = summary.ai_spend;
  if (!spend) return true;
  if (spend.spent != null && spend.spent > 0) return false;
  const economics = spend.unit_economics;
  if (economics) {
    if ((economics.total_completed ?? 0) > 0) return false;
    if (Object.keys(economics.per_agent ?? {}).length > 0) return false;
    if ((economics.unmeasured_spend_agents?.length ?? 0) > 0) return false;
  }
  return true;
}
