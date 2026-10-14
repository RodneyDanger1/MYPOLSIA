import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api, type FinanceSummary } from "@/lib/api";
import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import type { ExpenseRow, RevenueSnapshotRow, StripeEventRow } from "@/components/finance/types";
import FinancePage from "@/app/finance/page";

/**
 * Wave U3 — coder L1's own suite for the rebuilt Finance page.
 *
 * Scope discipline: run with `node node_modules/jest/bin/jest.js u3-finance`.
 *
 * What is pinned here:
 *  - Q-06 (the headline): a never-resolving read is a 7-tile `aria-busy` skeleton — the string "—"
 *    appears nowhere while the read is in flight;
 *  - Q-09 class: a failed read REPLACES the value rows (named alert, zero metric cards, no dash grid);
 *  - the null-vs-zero law inside the shared formatters ($0.00 for a measured zero, — for absent);
 *  - the idle surface (live's `Not running yet` pattern) for a genuinely nothing-measured summary,
 *    including the real fresh-stack wire shape (`spent: 0.0`, empty unit economics, no snapshot);
 *  - the three real list endpoints (revenue / expenses / events) with their own three states and the
 *    Q-12 count + truncation-marker idiom;
 *  - hostile bodies: null body, non-list body, absent fields — each a named state, never a crash and
 *    never a dash standing in for a failure.
 */

/** A test-local mock handle. The real `api.get` is generic; this suite wires one response body per
 *  path, so the handle is used untyped here — every assertion below is on the rendered DOM. */
const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

const EMPTY: FinanceSummary = {
  mrr_cents: null,
  arr_cents: null,
  active_subscribers: null,
  total_ad_spend_usd: null,
  total_expenses_month_cents: null,
  stripe_balance_cents: null,
  last_snapshot_date: null,
  ai_spend: null,
};

const MEASURED: FinanceSummary = {
  mrr_cents: 500000, // $5,000.00
  arr_cents: 6000000, // $60,000.00
  active_subscribers: 42,
  total_ad_spend_usd: 1234.5, // $1,234.50
  total_expenses_month_cents: 98765, // $987.65
  stripe_balance_cents: 100000000, // $1,000,000.00
  last_snapshot_date: "2026-10-13",
  ai_spend: {
    limit: 10.0,
    spent: 2.5,
    remaining: 7.5,
    burn_rate: null,
    unit_economics: {
      total_spend_usd: 2.5,
      total_completed: 1,
      cost_per_completed_task_usd: 2.5,
      per_agent: {},
      note: "note",
      unmeasured_spend_agents: [],
    },
  },
};

type Reads = {
  /** `"summary" in reads` decides presence, so an explicit `null` body stays null. */
  summary?: unknown;
  revenue?: unknown;
  expenses?: unknown;
  events?: unknown;
};

/** One response body per path; lists default to an honest empty page. */
function wire(reads: Reads): void {
  const bodies = {
    summary: "summary" in reads ? reads.summary : EMPTY,
    revenue: "revenue" in reads ? reads.revenue : [],
    expenses: "expenses" in reads ? reads.expenses : [],
    events: "events" in reads ? reads.events : [],
  };
  mockApiGet.mockImplementation((path: string) => {
    if (path.startsWith("/finance/summary")) return Promise.resolve(bodies.summary);
    if (path.startsWith("/finance/revenue")) return Promise.resolve(bodies.revenue);
    if (path.startsWith("/finance/expenses")) return Promise.resolve(bodies.expenses);
    if (path.startsWith("/finance/events")) return Promise.resolve(bodies.events);
    return Promise.reject(new Error(`unexpected path in test: ${path}`));
  });
}

function card(key: string): HTMLElement {
  const el = document.querySelector(`[data-kpi-key="${key}"]`);
  if (!(el instanceof HTMLElement)) throw new Error(`the ${key} card must be rendered`);
  return el;
}

function bodyText(): string {
  return document.body.textContent ?? "";
}

function revenueRow(id: number, overrides: Partial<RevenueSnapshotRow> = {}): RevenueSnapshotRow {
  return {
    id,
    snapshot_date: `2026-10-${String((id % 28) + 1).padStart(2, "0")}`,
    mrr_cents: 100000,
    arr_cents: 1200000,
    active_subscribers: 3,
    churned_today: null,
    new_today: null,
    total_revenue_month_cents: null,
    stripe_balance_cents: null,
    created_at: null,
    ...overrides,
  };
}

const expenseRow: ExpenseRow = {
  id: 1,
  category: "software",
  vendor: "Provider Co",
  amount_cents: 4999,
  currency: "usd",
  description: null,
  date: "2026-10-12",
  external_ref: null,
  created_at: null,
};

const eventRow: StripeEventRow = {
  id: 1,
  stripe_event_id: "evt_1",
  event_type: "charge.succeeded",
  customer_id: "cus_1",
  amount_cents: 500,
  currency: "usd",
  status: null,
  processed_at: "2026-10-13T18:21:39Z",
};

beforeEach(() => {
  mockApiGet.mockReset();
});

/* ---------------------------------------------------------------------------
 * Three states (Q-06 headline; finance.md acceptance 1)
 * ------------------------------------------------------------------------- */

describe("Finance — the loading state is a skeleton, never an em-dash grid (Q-06, acceptance 1)", () => {
  it("a never-resolving read renders 7 aria-busy skeleton tiles and NO em dash anywhere", () => {
    mockApiGet.mockImplementation(() => new Promise<never>(() => undefined));

    render(<FinancePage />);

    expect(screen.getByTestId("summary-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.getAllByTestId("kpi-skeleton")).toHaveLength(7);
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
    // The audit's exact complaint: not one "not measured" marker may stand in for "not read yet".
    expect(bodyText()).not.toContain(NOT_MEASURED);
    expect(screen.getAllByText("Loading…").length).toBeGreaterThan(0);
  });

  it("a resolved payload replaces every skeleton with a real reading", async () => {
    wire({ summary: MEASURED });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(screen.queryAllByTestId("kpi-skeleton")).toHaveLength(0);
    expect(screen.getAllByTestId("kpi-card")).toHaveLength(7);
    expect(card("mrr")).toHaveTextContent("$5,000.00");
    expect(screen.getByTestId("finance-freshness")).toHaveTextContent("Last snapshot 2026-10-13");
  });
});

/* ---------------------------------------------------------------------------
 * Failure (Q-09 class; acceptance 2)
 * ------------------------------------------------------------------------- */

describe("Finance — a failure replaces the value rows (Q-09 class, acceptance 2)", () => {
  it("a rejected read names the failure (ApiError.display) and renders ZERO metric cards", async () => {
    mockApiGet.mockRejectedValue(new ApiError(503, "backing store refused", "E_REDIS", "check the redis service"));

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the finance summary/);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_REDIS: backing store refused — check the redis service");
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
    expect(screen.queryAllByTestId("kpi-skeleton")).toHaveLength(0);
    // No dash grid under the banner — no ELEMENT renders the "not measured" marker as its value
    // (the em dashes inside the refusal sentences themselves are prose, not a reading).
    expect(screen.queryAllByText(NOT_MEASURED)).toHaveLength(0);
  });

  it("a non-ApiError failure still shows its own words, never an apology", async () => {
    mockApiGet.mockImplementation((path: string) =>
      path.startsWith("/finance/summary") ? Promise.reject(new Error("socket closed")) : Promise.resolve([]),
    );

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the finance summary/);
    expect(alert).toHaveTextContent("socket closed");
  });

  it("a 200 with an EMPTY body is a named failure, never an endless skeleton (hostile body)", async () => {
    wire({ summary: null });

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the finance summary/);
    expect(alert).toHaveTextContent("the server returned an empty body");
    expect(screen.queryAllByTestId("kpi-skeleton")).toHaveLength(0);
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
  });

  it("a successful response that is not a summary object is named, not rendered (hostile body)", async () => {
    wire({ summary: ["not", "a", "summary"] });

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the finance summary/);
    expect(alert).toHaveTextContent("the server returned no summary object");
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
  });
});

/* ---------------------------------------------------------------------------
 * Null vs zero (acceptance 3)
 * ------------------------------------------------------------------------- */

describe("Finance — null vs zero (acceptance 3)", () => {
  it("null is an em dash; a measured 0 is $0.00 — the two never collapse", async () => {
    wire({ summary: { ...EMPTY, last_snapshot_date: "2026-10-13", stripe_balance_cents: 0 } });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(card("mrr")).toHaveTextContent(NOT_MEASURED);
    expect(card("stripe-balance")).toHaveTextContent("$0.00");
  });

  it("money renders with thousands separators and two decimals (the Q-13 collapse)", async () => {
    wire({ summary: { ...EMPTY, last_snapshot_date: "2026-10-13", mrr_cents: 0, total_ad_spend_usd: 1234.5 } });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(card("mrr")).toHaveTextContent("$0.00");
    expect(card("ad-spend")).toHaveTextContent("$1,234.50");
  });

  it("an unmeasured spend day renders — with the affected agents NAMED, never priced at $0.00", async () => {
    wire({
      summary: {
        ...EMPTY,
        last_snapshot_date: "2026-10-13",
        ai_spend: {
          limit: 10.0,
          spent: null,
          remaining: null,
          burn_rate: null,
          unit_economics: {
            total_spend_usd: 0,
            total_completed: 0,
            cost_per_completed_task_usd: null,
            per_agent: {},
            note: "note",
            unmeasured_spend_agents: ["finance", "social_media"],
          },
        },
      },
    });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(card("ai-spend")).toHaveTextContent(NOT_MEASURED);
    expect(card("ai-spend")).toHaveTextContent("unmeasured spend: finance, social_media");
    expect(card("ai-spend")).not.toHaveTextContent("$0.00");
  });

  it("an absent ai_spend block renders the em dash — never a fabricated $0.00", async () => {
    wire({ summary: { ...EMPTY, last_snapshot_date: "2026-10-13" } });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(card("ai-spend")).toHaveTextContent(NOT_MEASURED);
    expect(card("ai-spend")).not.toHaveTextContent("$0.00");
  });

  it("a MEASURED zero spend (the ledger answered 0.0) renders $0.00 of $10.00", async () => {
    wire({
      summary: {
        ...EMPTY,
        last_snapshot_date: "2026-10-13",
        ai_spend: { limit: 10.0, spent: 0, remaining: 10.0, burn_rate: null, unit_economics: null },
      },
    });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(card("ai-spend")).toHaveTextContent("$0.00 of $10.00");
  });

  it("a non-finite or non-numeric money payload is 'not measured', never $NaN", async () => {
    wire({
      summary: {
        ...EMPTY,
        last_snapshot_date: "2026-10-13",
        mrr_cents: Number.NaN,
        total_ad_spend_usd: "1234.5" as unknown as number,
      },
    });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(card("mrr")).toHaveTextContent(NOT_MEASURED);
    expect(card("ad-spend")).toHaveTextContent(NOT_MEASURED);
    expect(bodyText()).not.toContain("NaN");
  });
});

/* ---------------------------------------------------------------------------
 * Idle (acceptance 5)
 * ------------------------------------------------------------------------- */

describe("Finance — the idle surface (acceptance 5)", () => {
  it("nothing measured at all renders live's idle copy, not a grid of dashes and not $0.00s", async () => {
    wire({ summary: EMPTY });

    render(<FinancePage />);

    await screen.findByTestId("finance-idle");
    expect(screen.getByTestId("finance-idle")).toHaveTextContent("Not running yet");
    expect(screen.getByTestId("finance-idle")).toHaveTextContent(/Agents page/);
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
    expect(screen.queryAllByTestId("kpi-skeleton")).toHaveLength(0);
    expect(bodyText()).not.toContain("$0.00");
    // No element's whole text is the not-measured marker (the prose dashes in the copy are prose).
    expect(screen.queryAllByText(NOT_MEASURED)).toHaveLength(0);
  });

  it("the real fresh-stack wire shape (spent 0.0, empty unit economics, no snapshot) is idle too", async () => {
    wire({
      summary: {
        ...EMPTY,
        ai_spend: {
          limit: 10.0,
          spent: 0,
          remaining: 10.0,
          burn_rate: null,
          unit_economics: {
            total_spend_usd: 0,
            total_completed: 0,
            cost_per_completed_task_usd: null,
            per_agent: {},
            note: "note",
            unmeasured_spend_agents: [],
          },
        },
      },
    });

    render(<FinancePage />);

    await screen.findByTestId("finance-idle");
    expect(screen.queryAllByTestId("kpi-card")).toHaveLength(0);
  });
});

/* ---------------------------------------------------------------------------
 * Refused surfaces (acceptance 6)
 * ------------------------------------------------------------------------- */

describe("Finance — no phantom money surfaces (acceptance 6)", () => {
  it("never renders SETUP PAYMENTS / CREDIT / TOP UP / UPGRADE — none has an endpoint here", async () => {
    wire({ summary: MEASURED });

    render(<FinancePage />);

    await screen.findByTestId("finance-freshness");
    expect(bodyText()).not.toMatch(/SETUP PAYMENTS/i);
    expect(bodyText()).not.toMatch(/\bCREDITS?\b/i);
    expect(bodyText()).not.toMatch(/TOP ?UP/i);
    expect(bodyText()).not.toMatch(/UPGRADE/i);
  });
});

/* ---------------------------------------------------------------------------
 * The real list endpoints (spec R4; Q-12 count honesty)
 * ------------------------------------------------------------------------- */

describe("Finance — the list surfaces serve real fields (spec R4)", () => {
  it("revenue rows render their real fields, with the measured count — never a claimed total", async () => {
    wire({ summary: MEASURED, revenue: [revenueRow(1, { mrr_cents: 250000 }), revenueRow(2, { mrr_cents: null })] });

    render(<FinancePage />);

    const table = await screen.findByTestId("revenue-table");
    expect(within(table).getAllByRole("row")).toHaveLength(3); // header + 2 rows
    expect(within(table).getByText("$2,500.00")).toBeInTheDocument();
    expect(within(table).getByText(NOT_MEASURED)).toBeInTheDocument();
    expect(screen.getByTestId("revenue-count")).toHaveTextContent("2 snapshots");
  });

  it("a full page says the read is capped (Q-12); a short page says nothing extra", async () => {
    wire({ summary: MEASURED, revenue: Array.from({ length: 30 }, (_, i) => revenueRow(i + 1)) });

    render(<FinancePage />);

    expect(await screen.findByTestId("revenue-truncation")).toHaveTextContent("Showing the first 30");
  });

  it("a short list renders NO truncation marker (nothing was cut off)", async () => {
    wire({ summary: MEASURED, revenue: [revenueRow(1)] });

    render(<FinancePage />);

    await screen.findByTestId("revenue-count");
    expect(screen.queryByTestId("revenue-truncation")).toBeNull();
  });

  it("an empty list is an honest empty sentence per surface, never a fake row", async () => {
    wire({ summary: MEASURED, expenses: [expenseRow], events: [eventRow] });

    render(<FinancePage />);

    await screen.findByTestId("revenue-count");
    expect(screen.getByTestId("revenue-count")).toHaveTextContent("0 snapshots");
    expect(screen.getByText(/No revenue snapshots yet/)).toBeInTheDocument();
    expect(screen.queryByTestId("revenue-table")).toBeNull();
    // …while the surfaces that DID return rows render them.
    expect(screen.getByTestId("expenses-table")).toBeInTheDocument();
    expect(screen.getByTestId("events-table")).toBeInTheDocument();
  });

  it("expense rows and event timestamps render real fields; a null timestamp is 'date unknown'", async () => {
    wire({
      summary: MEASURED,
      expenses: [expenseRow],
      events: [
        eventRow,
        {
          id: 2,
          stripe_event_id: "evt_2",
          event_type: "customer.created",
          customer_id: null,
          amount_cents: null,
          currency: null,
          status: null,
          processed_at: null,
        },
      ],
    });

    render(<FinancePage />);

    const expenses = await screen.findByTestId("expenses-table");
    expect(within(expenses).getByText("Provider Co")).toBeInTheDocument();
    expect(within(expenses).getByText("$49.99")).toBeInTheDocument();

    const events = screen.getByTestId("events-table");
    expect(within(events).getByText(absoluteStamp(eventRow.processed_at))).toBeInTheDocument();
    expect(within(events).getByText("date unknown")).toBeInTheDocument();
    expect(events.textContent ?? "").not.toContain("Invalid Date");
    expect(within(events).getAllByText(NOT_MEASURED).length).toBeGreaterThan(0); // null amount + null customer
  });

  it("a failing list names itself while the summary region stays intact (failure isolation)", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith("/finance/summary")) return Promise.resolve(MEASURED);
      if (path.startsWith("/finance/revenue")) return Promise.reject(new ApiError(500, "boom", "E_HTTP_500"));
      return Promise.resolve([]);
    });

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the revenue list/);
    expect(alert).toHaveTextContent("E_HTTP_500: boom");
    expect(screen.getAllByTestId("kpi-card")).toHaveLength(7);
    expect(screen.queryByText(/Could not load the finance summary/)).toBeNull();
  });

  it("a list body that is not a list is named, not rendered (hostile body)", async () => {
    wire({ summary: MEASURED, revenue: { rows: [] } });

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the revenue list/);
    expect(alert).toHaveTextContent("the server returned no list of rows");
  });
});

/* ---------------------------------------------------------------------------
 * Refresh (spec OPEN DECISION 5; C7 — no polling)
 * ------------------------------------------------------------------------- */

describe("Finance — explicit Refresh", () => {
  it("Refresh re-issues the reads and lands back on settled values", async () => {
    wire({ summary: MEASURED });

    render(<FinancePage />);
    await screen.findByTestId("finance-freshness");

    await userEvent.setup().click(screen.getByTestId("finance-refresh"));

    await waitFor(() => {
      const summaryCalls = mockApiGet.mock.calls.filter((call) => String(call[0]).startsWith("/finance/summary"));
      expect(summaryCalls).toHaveLength(2);
    });
    await screen.findByTestId("finance-freshness");
    expect(screen.getAllByTestId("kpi-card")).toHaveLength(7);
  });
});
