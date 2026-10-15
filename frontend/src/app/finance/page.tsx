"use client";

import { useCallback, useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { FinanceListPanel, type FinanceColumn } from "@/components/finance/FinanceListPanel";
import { FinanceSummaryGrid } from "@/components/finance/FinanceSummaryGrid";
import { centsText, countText, textOrDash, usdText } from "@/components/finance/format";
import { IdleNotice, ListSkeleton, ReadError, SummarySkeleton } from "@/components/finance/states";
import { isIdleSummary } from "@/components/finance/summary";
import type { ExpenseRow, RevenueSnapshotRow, StripeEventRow } from "@/components/finance/types";
import { useFinanceRead } from "@/components/finance/useFinanceRead";
import { Card, Chip } from "@/components/ui";
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

/* -------------------------------------------------------------------------------------------
 * V9/B5 — the Planning panel (plan §9 B2's `GET /api/v1/finance/planning`).
 *
 * The endpoint answers the burn line items, the capacity reading from the owner's hours, and the
 * margin template — with anything it could not measure served as `null` PLUS the reason. This panel
 * is that contract rendered, and it never improves on it. THE CONTRACT (aligned wave v14, S3-05 —
 * the same words live in `backend/app/api/v1/finance.py::build_planning_view`):
 *   - the three segments are ALWAYS objects on the wire; a field inside one is `null` when that
 *     input was not measured, and its cause is recorded in the payload's `reasons` map, keyed 1:1
 *     to the field: `capacity.<field>`, `margin_template.<field>`, and `burn_line_items` for the
 *     whole month (`burn_line_items.<category>` per category);
 *   - a null burn row carries the same string inlined as its own `reason`;
 *   - this panel renders that cause NEXT TO the reading it explains: a null input is the em dash
 *     (the project's one "not measured" marker) followed by the reason text, and a whole segment
 *     that came back null says "not measured" with the payload's reason — never a hardcoded cause;
 *   - `pricing_guardrails` (R3's anti-patterns) render as flag chips WHEN PRESENT — an empty list is
 *     a real answer and simply has no warnings to show;
 *   - a body carrying none of the planning fields at all (a wrong or truncated read) is named as
 *     such rather than rendered as three empty segments.
 * ----------------------------------------------------------------------------------------- */

/** See `BusinessPanel.marginPercentText` for why the unit is disambiguated rather than assumed:
 *  B2's contract pins the field name, not whether a fraction or a percent is on the wire. */
function planningPercentText(value: number): string {
  if (!Number.isFinite(value)) return NOT_MEASURED;
  // Fraction vs already-a-percent: see `BusinessPanel.marginPercentText`. One decimal for the
  // fraction branch, the server's own digits for the percent branch.
  if (Math.abs(value) <= 1) return `${(value * 100).toFixed(1)}%`;
  return `${value}%`;
}

type BurnLineItem = {
  label: string;
  monthly_usd: number | null;
  source: string | null;
  reason?: string | null;
};

type PlanningCapacity = {
  hours_per_client_lo: number | null;
  hours_per_client_hi: number | null;
  max_clients: number | null;
  mrr_at_capacity: number | null;
  break_even_clients: number | null;
};

type MarginTemplate = {
  price: number | null;
  ai_cost: number | null;
  human_hours: number | null;
  human_rate: number | null;
  margin: number | null;
};

type PlanningView = {
  burn_line_items?: BurnLineItem[] | null;
  capacity?: PlanningCapacity | null;
  margin_template?: MarginTemplate | null;
  /** The field-level reason contract (v14 S3-05): every input the endpoint could not measure
   *  carries its cause here, keyed 1:1 to the field — `capacity.<field>`, `margin_template.<field>`,
   *  `burn_line_items` (whole month) / `burn_line_items.<category>` (per category). The panel renders
   *  these next to the readings they explain; a key exists only for an unmeasured field. */
  reasons?: Record<string, string> | null;
  pricing_guardrails?: string[] | null;
};

/** The cause the payload records for one field, or null when it records none (a measured field has
 *  no reason — the map only explains nulls). Blank/unreadable entries are treated as absent. */
function reasonFor(reasons: Record<string, string>, key: string): string | null {
  const text = reasons[key];
  return typeof text === "string" && text.trim() !== "" ? text : null;
}

/* -------------------------------------------------------------------------------------------
 * Market context (parent scope amendment, owner-driven): ONE compact mono line fed by
 * `GET /api/v1/economics/context`, whose frozen shape is
 *   {inflation: {value, period, source, fetched_at} | null (+ reason), fx: <same> | null (+ reason),
 *    generated_at}
 * The line shows the facts that EXIST with their source label and fetched-at date, and states the
 * reason for any section the endpoint could not measure. A failed read is named as a failure. No
 * value is ever inferred, rounded, unit-guessed or hidden.
 * ----------------------------------------------------------------------------------------- */

type MarketFact = {
  value?: string | number | null;
  period?: string | null;
  /** fx-shape (V9 settle): the frankfurter section carries a DATE + a rates map, no `value`. */
  date?: string | null;
  rates?: Record<string, number> | null;
  source?: string | null;
  fetched_at?: string | null;
  /** Present inside the section when the value is answered-but-null — the dash's explanation. */
  reason?: string | null;
};

type MarketContext = {
  inflation?: MarketFact | null;
  inflation_reason?: string | null;
  fx?: MarketFact | null;
  fx_reason?: string | null;
  generated_at?: string | null;
  reason?: string | null;
};

/** One fact, in the order the reader needs it: value, period, source label, fetch stamp. */
function marketFactText(label: string, fact: MarketFact | null | undefined, reason: string | null): string {
  if (!fact) {
    return `${label} ${NOT_MEASURED} not measured: ${reason ?? "the read gave no reason"}`;
  }
  // The fx section measures RATES, not a single value; render the map rather than dropping it.
  const ratesText =
    fact.rates && typeof fact.rates === "object"
      ? Object.entries(fact.rates)
          .map(([code, rate]) => `${code} ${rate}`)
          .join(" · ")
      : null;
  const absent = fact.value === null || fact.value === undefined;
  const parts: string[] = [absent ? (ratesText ?? NOT_MEASURED) : String(fact.value)];
  if (fact.period) parts.push(fact.period);
  if (fact.date) parts.push(fact.date);
  if (fact.source) parts.push(`source ${fact.source}`);
  if (fact.fetched_at) parts.push(`fetched ${absoluteStamp(fact.fetched_at)}`);
  // A null value answered INSIDE the section carries its own reason — the dash must be explained.
  if (absent && !ratesText && fact.reason) parts.push(fact.reason);
  return `${label} ${parts.join(" · ")}`;
}

function MarketContextStrip({ reloadKey }: { reloadKey: number }) {
  const { data, error, loading } = useFinanceRead<MarketContext>("/economics/context", reloadKey);

  if (loading) {
    return (
      <p className="ui-label text-ink-muted" aria-busy="true" data-testid="market-context-loading">
        {/* Q-06's law applies to every region on this page: a loading state says "Loading…", it
            never prints the not-measured marker (which means "read, and nothing was there"). */}
        Market context: Loading…
      </p>
    );
  }
  if (error !== null) {
    // Named, in the alert voice colour, but NOT an ARIA alert: this page's one live alert region is
    // the summary's own failure, and an auxiliary read that fails must inform without hijacking it.
    return (
      <p className="text-sm text-alert" data-testid="market-context-error">
        Market context could not be read: {error}
      </p>
    );
  }

  const context = data ?? {};
  return (
    <p className="ui-label text-ink-muted" data-testid="market-context">
      <span data-testid="market-context-inflation">
        {marketFactText("Inflation", context.inflation, context.inflation_reason ?? context.reason ?? null)}
      </span>
      {" · "}
      <span data-testid="market-context-fx">
        {marketFactText("FX", context.fx, context.fx_reason ?? context.reason ?? null)}
      </span>
      {context.generated_at ? <>{" · "}as of {absoluteStamp(context.generated_at)}</> : null}
    </p>
  );
}

/** A bare reading, in its own element so the marker is exactly the marker (nothing appended to it
 *  that would make "—" unreadable as one value). */
function Reading({ text }: { text: string }) {
  return <span className="ui-label text-ink">{text}</span>;
}

function moneyOrDash(value: number | null | undefined): string {
  return usdText(value);
}

function NumberRow({
  label,
  value,
  reason,
}: {
  label: string;
  value: string;
  /** The payload's recorded cause for this reading, when it could not be measured. */
  reason?: string | null;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="ui-label text-ink-muted">{label}</dt>
      <dd className="text-right">
        <Reading text={value} />
        {reason ? (
          <span className="ml-2 text-xs text-ink-muted" data-testid="planning-reason">
            {reason}
          </span>
        ) : null}
      </dd>
    </div>
  );
}

function FinancePlanningPanel({ reloadKey }: { reloadKey: number }) {
  const { data, error, loading } = useFinanceRead<PlanningView>("/finance/planning", reloadKey);
  const view = data !== null && typeof data === "object" && !Array.isArray(data) ? data : null;

  const recognised =
    view !== null &&
    (view.burn_line_items !== undefined ||
      view.capacity !== undefined ||
      view.margin_template !== undefined ||
      view.pricing_guardrails !== undefined);

  /** The payload's reason map (v14 S3-05): the ONE place each unmeasured input's cause is read
   *  from. Defensive normalization — a non-object answer is treated as "no reasons recorded". */
  const reasons: Record<string, string> =
    view !== null && view.reasons !== null && typeof view.reasons === "object" && !Array.isArray(view.reasons)
      ? view.reasons
      : {};

  return (
    <Card title="Planning" data-testid="finance-planning">
      {loading ? <ListSkeleton testId="finance-planning-loading" /> : null}
      {!loading && error !== null ? (
        // The same sentence `ReadError` prints, without its `role="alert"`: this page owns exactly
        // one live alert region (the summary's), and u3-finance pins that an auxiliary read failing
        // must not raise a second one. The failure is still NAMED here, in the alert voice.
        <p className="text-sm text-alert" data-testid="planning-read-error">
          Could not load the finance planning view: {error}
        </p>
      ) : null}

      {!loading && error === null && view !== null && !recognised ? (
        <p className="text-sm text-ink-muted" data-testid="planning-unrecognised">
          The planning read answered without any planning fields — no burn items, capacity or margin
          inputs came back, so there is nothing on this read to show.
        </p>
      ) : null}

      {!loading && error === null && view !== null && recognised ? (
        <div className="space-y-3">
          {/* Burn line items */}
          {Array.isArray(view.burn_line_items) && view.burn_line_items.length > 0 ? (
            <ul className="space-y-1" data-testid="planning-burn">
              {view.burn_line_items.map((item, index) => (
                <li key={`${item.label}-${index}`} className="flex items-baseline justify-between gap-3">
                  <span className="ui-label text-ink-muted">{item.label}</span>
                  <span className="text-right">
                    <Reading text={moneyOrDash(item.monthly_usd)} />
                    <span className="ml-2 text-xs text-ink-muted">
                      {item.monthly_usd === null
                        ? (item.reason ?? "no reason was recorded for this input")
                        : item.source
                          ? item.source
                          : "source not recorded"}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-muted" data-testid="planning-burn-empty">
              No burn line items were measured
              {reasonFor(reasons, "burn_line_items")
                ? ` — ${reasonFor(reasons, "burn_line_items")}`
                : ""}.
            </p>
          )}

          {/* Capacity from the owner's available hours. Each reading that could not be measured
              prints the cause the payload recorded for THAT field (never a hardcoded one). */}
          {view.capacity ? (
            <dl className="space-y-1" data-testid="planning-capacity">
              <NumberRow
                label="Hours per client"
                value={`${countText(view.capacity.hours_per_client_lo)}–${countText(
                  view.capacity.hours_per_client_hi,
                )}`}
                reason={
                  reasonFor(reasons, "capacity.hours_per_client_lo") ??
                  reasonFor(reasons, "capacity.hours_per_client_hi")
                }
              />
              <NumberRow
                label="Max clients"
                value={countText(view.capacity.max_clients)}
                reason={reasonFor(reasons, "capacity.max_clients")}
              />
              <NumberRow
                label="MRR at capacity"
                value={usdText(view.capacity.mrr_at_capacity)}
                reason={reasonFor(reasons, "capacity.mrr_at_capacity")}
              />
              <NumberRow
                label="Break-even clients"
                value={countText(view.capacity.break_even_clients)}
                reason={reasonFor(reasons, "capacity.break_even_clients")}
              />
            </dl>
          ) : (
            <p className="text-sm text-ink-muted" data-testid="planning-capacity-unmeasured">
              Capacity is not measured —{" "}
              {reasonFor(reasons, "capacity.max_clients") ??
                reasonFor(reasons, "capacity.hours_per_client_lo") ??
                "the payload named no reason for this read"}.
            </p>
          )}

          {/* The margin template */}
          {view.margin_template ? (
            <dl className="space-y-1" data-testid="planning-margin">
              <NumberRow
                label="Price"
                value={usdText(view.margin_template.price)}
                reason={reasonFor(reasons, "margin_template.price")}
              />
              <NumberRow
                label="AI cost"
                value={usdText(view.margin_template.ai_cost)}
                reason={reasonFor(reasons, "margin_template.ai_cost")}
              />
              <NumberRow
                label="Human hours"
                value={countText(view.margin_template.human_hours)}
                reason={reasonFor(reasons, "margin_template.human_hours")}
              />
              <NumberRow
                label="Human rate"
                value={usdText(view.margin_template.human_rate)}
                reason={reasonFor(reasons, "margin_template.human_rate")}
              />
              <NumberRow
                label="Margin"
                value={
                  view.margin_template.margin === null
                    ? NOT_MEASURED
                    : planningPercentText(view.margin_template.margin)
                }
                reason={reasonFor(reasons, "margin_template.margin")}
              />
            </dl>
          ) : (
            <p className="text-sm text-ink-muted" data-testid="planning-margin-unmeasured">
              The margin template is not measured —{" "}
              {reasonFor(reasons, "margin_template.margin") ??
                reasonFor(reasons, "margin_template.price") ??
                "the payload named no reason for this read"}.
            </p>
          )}

          {/* R3's pricing-model guardrails: rendered when present, silent when empty */}
          {Array.isArray(view.pricing_guardrails) && view.pricing_guardrails.length > 0 ? (
            <ul className="flex flex-wrap gap-2" data-testid="planning-guardrails">
              {view.pricing_guardrails.map((warning, index) => (
                <li key={`${warning}-${index}`}>
                  <Chip variant="soft" className="text-amber-800">
                    {warning}
                  </Chip>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {/* The market strip is its own read with its own three states, so it renders in EVERY
          planning state above — a broken planning read must not take the market line with it. */}
      <MarketContextStrip reloadKey={reloadKey} />
    </Card>
  );
}

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

      {/* V9/B5: the planning panel reads its own endpoint and keeps its own three states, so a
          failed planning read cannot blank the finance page (and vice versa). */}
      <FinancePlanningPanel reloadKey={reloadKey} />

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
