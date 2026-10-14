"use client";

import { useCallback, useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { FinanceListPanel, type FinanceColumn } from "@/components/finance/FinanceListPanel";
import { FinanceSummaryGrid } from "@/components/finance/FinanceSummaryGrid";
import { centsText, countText, textOrDash } from "@/components/finance/format";
import { IdleNotice, ReadError, SummarySkeleton } from "@/components/finance/states";
import { isIdleSummary } from "@/components/finance/summary";
import type { ExpenseRow, RevenueSnapshotRow, StripeEventRow } from "@/components/finance/types";
import { useFinanceRead } from "@/components/finance/useFinanceRead";
import type { FinanceSummary } from "@/lib/api";

/**
 * Finance — the money surface, rebuilt to the live design language (wave U3).
 *
 * Reads four REAL endpoints and nothing else: `GET /finance/summary` (the seven KPI cards) and the
 * three list endpoints the spec verified as served-but-unused — `GET /finance/revenue`,
 * `GET /finance/expenses`, `GET /finance/events` — each tabular, each row rendering its real
 * fields. No chart is drawn (no series endpoint exists; an invented series is forbidden), and no
 * polling runs (C7): one fetch per region on mount plus the explicit Refresh, which re-issues
 * every read.
 *
 * Q-06 (the headline fix). The old page had NO loading state: while the summary read was in
 * flight `data` was null, so all seven cards rendered their "not measured" em dash and a cold load
 * read as "nothing was ever measured". Now the three states are disjoint and each is stated in its
 * own words:
 *   loading -> a seven-tile skeleton with aria-busy (the audit's own prescribed shape)
 *   error   -> the failure NAMED (ApiError.display; a non-ApiError shows its own message), and the
 *              value rows are REPLACED, not drawn under the banner (Q-09's class: no dash grid
 *              beneath a failed read)
 *   success -> values; the em dash survives ONLY for a genuinely absent value on a successful
 *              read, where "not measured" is the truth (a real 0 still renders $0.00)
 *
 * On a successful read where NOTHING has been measured at all — no snapshot date, no money field,
 * no agent-run signal — the page renders the live product's idle pattern (`Not running yet` + a
 * next-step line) INSTEAD of a grid of dashes: that state is "not started", not "measured empty".
 *
 * Refused BY NAME, not drawn: the live `SETUP PAYMENTS` button and every credit/top-up/upgrade
 * surface have no clone endpoint (no Stripe onboarding/Connect route and no credits route exist in
 * this backend — matrix §G/§K), so nothing here shows them. The live `trend` arrows are gone too:
 * they were inert props (Q-23), and a decoration that cannot be true is noise.
 */

/** List read sizes — the endpoints' own server defaults, passed explicitly. */
const REVENUE_LIMIT = 30;
const EXPENSES_LIMIT = 100;
const EVENTS_LIMIT = 100;

const REVENUE_COLUMNS: FinanceColumn<RevenueSnapshotRow>[] = [
  { key: "date", label: "Date", value: (row) => row.snapshot_date },
  { key: "mrr", label: "MRR", value: (row) => centsText(row.mrr_cents) },
  { key: "arr", label: "ARR", value: (row) => centsText(row.arr_cents) },
  { key: "subscribers", label: "Subscribers", value: (row) => countText(row.active_subscribers) },
  { key: "recorded", label: "Recorded", value: (row) => absoluteStamp(row.created_at) },
];

const EXPENSE_COLUMNS: FinanceColumn<ExpenseRow>[] = [
  { key: "date", label: "Date", value: (row) => row.date },
  { key: "category", label: "Category", value: (row) => row.category },
  { key: "vendor", label: "Vendor", value: (row) => row.vendor },
  { key: "amount", label: "Amount", value: (row) => centsText(row.amount_cents) },
];

const EVENT_COLUMNS: FinanceColumn<StripeEventRow>[] = [
  { key: "received", label: "Received", value: (row) => absoluteStamp(row.processed_at) },
  { key: "type", label: "Type", value: (row) => row.event_type },
  { key: "amount", label: "Amount", value: (row) => centsText(row.amount_cents) },
  { key: "customer", label: "Customer", value: (row) => textOrDash(row.customer_id) },
];

export default function FinancePage() {
  const [reloadKey, setReloadKey] = useState(0);
  const { data, error, loading } = useFinanceRead<FinanceSummary>("/finance/summary", reloadKey);
  const refresh = useCallback(() => setReloadKey((n) => n + 1), []);

  // A successful status whose body is not an object at all is a broken read, named as one.
  const summary = data !== null && typeof data === "object" && !Array.isArray(data) ? data : null;
  const brokenBody = data !== null && summary === null;
  const idle = summary !== null && isIdleSummary(summary);

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Finance</h1>
          {/* R1: the freshness line is data, so it exists only on a settled, non-idle read — a
              mono-caps LAST SNAPSHOT with the date AS-IS (`last_snapshot_date` is a date, not a
              timestamp: a relative clock derived from it would be fabricated precision). */}
          {summary !== null && !idle ? (
            <p className="ui-label text-ink-muted" data-testid="finance-freshness">
              Last snapshot {summary.last_snapshot_date ?? NOT_MEASURED}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          className="link ui-label"
          data-testid="finance-refresh"
          onClick={refresh}
        >
          Refresh
        </button>
      </div>

      {loading ? <SummarySkeleton /> : null}

      {!loading && error !== null ? (
        <ReadError label="the finance summary" message={error} />
      ) : null}

      {!loading && error === null && brokenBody ? (
        <ReadError label="the finance summary" message="the server returned no summary object" />
      ) : null}

      {!loading && error === null && idle ? <IdleNotice /> : null}

      {!loading && error === null && summary !== null && !idle ? (
        <FinanceSummaryGrid summary={summary} />
      ) : null}

      <FinanceListPanel
        title="Revenue"
        testId="revenue"
        path={`/finance/revenue?limit=${REVENUE_LIMIT}`}
        limit={REVENUE_LIMIT}
        noun="snapshot"
        errorLabel="the revenue list"
        emptyText="No revenue snapshots yet — the finance agent records one for each day it runs."
        rowKey={(row) => row.id}
        columns={REVENUE_COLUMNS}
        reloadKey={reloadKey}
      />

      <FinanceListPanel
        title="Expenses"
        testId="expenses"
        path={`/finance/expenses?limit=${EXPENSES_LIMIT}`}
        limit={EXPENSES_LIMIT}
        noun="expense"
        errorLabel="the expenses list"
        emptyText="No expenses recorded yet — the finance agent writes them from its run output."
        rowKey={(row) => row.id}
        columns={EXPENSE_COLUMNS}
        reloadKey={reloadKey}
      />

      <FinanceListPanel
        title="Stripe events"
        testId="events"
        path={`/finance/events?limit=${EVENTS_LIMIT}`}
        limit={EVENTS_LIMIT}
        noun="event"
        errorLabel="the Stripe events list"
        emptyText="No Stripe events received yet — the webhook records each delivery as Stripe sends it."
        rowKey={(row) => row.id}
        columns={EVENT_COLUMNS}
        reloadKey={reloadKey}
      />
    </div>
  );
}
