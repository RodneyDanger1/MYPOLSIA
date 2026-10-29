/**
 * The wire shapes of the three finance list endpoints, adopted field-for-field from the backend's
 * response schemas (`backend/app/schemas/finance.py`: `RevenueSnapshotResponse`,
 * `ExpenseRecordResponse`, `StripeEventResponse`). Every nullable field is nullable HERE for the
 * same reason it is nullable there — a raw-SQL/ops writer can leave it NULL — so a renderer must
 * send it through the formatters, which render "not measured" instead of guessing.
 */

export type RevenueSnapshotRow = {
  id: number;
  snapshot_date: string;
  mrr_cents: number | null;
  arr_cents: number | null;
  active_subscribers: number | null;
  churned_today: number | null;
  new_today: number | null;
  total_revenue_month_cents: number | null;
  stripe_balance_cents: number | null;
  created_at: string | null;
  /**
   * TS-1 provenance (F-33 renders it): the row's own measured columns, their exact complement, and
   * the coverage word `complete` / `partial` / `unmeasured` — computed server-side by
   * `finance_service.snapshot_provenance`, never stored. Optional here because a pre-TS-1 server
   * omits them; the table then says "not declared" instead of inventing coverage.
   */
  measured?: string[];
  unmeasured?: string[];
  provenance?: string;
};

export type ExpenseRow = {
  id: number;
  category: string;
  vendor: string;
  amount_cents: number;
  currency: string | null;
  description: string | null;
  date: string;
  external_ref: string | null;
  created_at: string | null;
};

export type StripeEventRow = {
  id: number;
  stripe_event_id: string;
  event_type: string;
  customer_id: string | null;
  amount_cents: number | null;
  currency: string | null;
  status: string | null;
  processed_at: string | null;
};
