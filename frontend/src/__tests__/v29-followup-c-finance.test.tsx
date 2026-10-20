/**
 * V29 follow-up C — the finance page's two hand-rolled reads, and the expense/event money columns.
 *
 * Proven defects (both measured on the real page with a 200 whose body is a JSON list — the shape a
 * proxy, a wrong route or a truncated rewrite produces):
 *
 *  1. THE PLANNING PANEL RENDERS NOTHING AT ALL. `view` is narrowed to "a plain object" and every
 *     branch is gated on `view !== null`, so a list body leaves the card holding its heading and the
 *     market strip and NO sentence — not a failure, not "unrecognised", nothing. The panel's own
 *     contract says a body carrying no planning fields "is named as such"; a body that is not even
 *     an object is the same broken read and must be named too.
 *
 *  2. THE MARKET STRIP CALLS A BROKEN READ "NOT MEASURED". `const context = data ?? {}` turns a list
 *     body into an object with no sections, so the strip prints "Inflation — not measured: the read
 *     gave no reason · FX — not measured: …". That is a MEASUREMENT claim about a body that carried
 *     no measurement: the honest answer is that the read did not produce a market-context object.
 *
 * Plus a wiring pin for the V29 currency work: the expense and Stripe-event tables must render each
 * row in the row's OWN code (`EUR 12.34`) and keep the USD shape (`$12.34`) — `moneyCentsText` is
 * unit-tested in v29-currency-followups, but until now nothing pinned that these two columns pass
 * `row.currency` to it at all. The wording for a row whose currency is NULL belongs to
 * `components/finance/format.ts` (a sibling lane's file, in flight while this lane ran) and is
 * deliberately NOT pinned here; this column's claim is only that it forwards the row's code.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { render, screen } from "@testing-library/react";

import FinancePage from "@/app/finance/page";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

const SUMMARY = {
  last_snapshot_date: "2026-10-19",
  mrr_cents: 100_000,
  arr_cents: 1_200_000,
  active_subscribers: 1,
  total_ad_spend_usd: 0,
  total_expenses_month_cents: 10,
  stripe_balance_cents: 5,
  ai_spend: {
    spent: 0,
    limit: 10,
    unit_economics: { total_completed: 0, per_agent: {}, unmeasured_spend_agents: [] },
  },
};

const PLANNING = {
  burn_line_items: [],
  capacity: null,
  margin_template: { price: 1, ai_cost: 1, human_hours: 1, human_rate: 1, margin: 0.5 },
  pricing_guardrails: [],
};

const CONTEXT = {
  inflation: {
    value: 2.9,
    period: "Sep 2026",
    source: "frankfurter",
    fetched_at: "2026-10-20T00:00:00Z",
  },
  fx: {
    rates: { EUR: 1.1 },
    date: "2026-10-20",
    source: "frankfurter",
    fetched_at: "2026-10-20T00:00:00Z",
  },
  generated_at: "2026-10-20T00:00:00Z",
};

function financeReads(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/finance/summary": SUMMARY,
    "/finance/revenue": [],
    "/finance/expenses": [],
    "/finance/events": [],
    "/finance/planning": PLANNING,
    "/economics/context": CONTEXT,
    "/config": { human_hourly_rate_usd: null },
    ...overrides,
  };
  mockGet.mockImplementation((path: string) => {
    const key = Object.keys(paths).find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return Promise.resolve(paths[key]);
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

// ── 1. a planning body that is not an object is named ────────────────────────────────────────
describe("the planning panel", () => {
  it("names a list body instead of rendering an empty card", async () => {
    financeReads({ "/finance/planning": [] });

    render(<FinancePage />);

    const named = await screen.findByTestId("planning-unreadable");
    expect(named).toHaveTextContent(/not a planning object/i);
    // The defect: no planning sentence at all — neither a refusal nor the unrecognised note.
    expect(screen.queryByTestId("planning-unrecognised")).toBeNull();
    // And it never pretends the three segments were measured-and-empty.
    expect(screen.queryByTestId("planning-burn")).toBeNull();
    expect(screen.queryByTestId("planning-capacity")).toBeNull();
  });

  it("keeps the existing unrecognised-object sentence for a plain object with no planning fields", async () => {
    financeReads({ "/finance/planning": { unexpected: true } });

    render(<FinancePage />);

    expect(await screen.findByTestId("planning-unrecognised")).toBeInTheDocument();
    expect(screen.queryByTestId("planning-unreadable")).toBeNull();
  });
});

// ── 2. the market strip never calls a broken body "not measured" ─────────────────────────────
describe("the market-context strip", () => {
  it("names a list body as a broken read instead of claiming nothing was measured", async () => {
    financeReads({ "/economics/context": [] });

    render(<FinancePage />);

    const named = await screen.findByTestId("market-context-error");
    expect(named).toHaveTextContent(/no market-context object/i);
    // The defect: both sections printed "— not measured: the read gave no reason".
    expect(screen.queryByTestId("market-context")).toBeNull();
    expect(screen.queryByText(/Inflation — not measured/)).toBeNull();
  });

  it("still renders the measured facts from a real context body", async () => {
    financeReads();

    render(<FinancePage />);

    const strip = await screen.findByTestId("market-context");
    expect(strip).toHaveTextContent("Inflation 2.9");
    expect(strip).toHaveTextContent("FX EUR 1.1");
    expect(screen.queryByTestId("market-context-error")).toBeNull();
  });
});

// ── 3. the expense / Stripe-event money columns read the row's own currency ──────────────────
describe("the expense and Stripe-event money columns", () => {
  it("prints each row in its own code and keeps the USD shape", async () => {
    financeReads({
      "/finance/expenses": [
        {
          id: 1,
          category: "hosting",
          vendor: "Hetzner",
          amount_cents: 1234,
          currency: "EUR",
          description: null,
          date: "2026-10-19",
          external_ref: null,
          created_at: "2026-10-19T10:00:00Z",
        },
        {
          id: 2,
          category: "domains",
          vendor: "Namecheap",
          amount_cents: 1234,
          currency: "USD",
          description: null,
          date: "2026-10-19",
          external_ref: null,
          created_at: "2026-10-19T10:00:00Z",
        },
      ],
      "/finance/events": [
        {
          id: 9,
          stripe_event_id: "evt_1",
          event_type: "invoice.paid",
          customer_id: "cus_1",
          amount_cents: 4999,
          currency: "GBP",
          status: "processed",
          processed_at: "2026-10-19T10:00:00Z",
        },
      ],
    });

    render(<FinancePage />);

    const expenses = await screen.findByTestId("expenses-table");
    const rows = Array.from(expenses.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("EUR 12.34");
    // A USD row keeps the house `$` shape (never a bare number).
    expect(rows[1]).toHaveTextContent("$12.34");
    expect(rows[0].textContent ?? "").not.toMatch(/\$12\.34/);

    const events = await screen.findByTestId("events-table");
    expect(events).toHaveTextContent("GBP 49.99");
    expect(events.textContent ?? "").not.toMatch(/\$49\.99/);
  });
});
