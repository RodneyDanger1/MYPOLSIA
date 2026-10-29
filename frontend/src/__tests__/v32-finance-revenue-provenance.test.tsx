/**
 * WAVE 4 / F-33 — the Revenue table renders the PROVENANCE the wire already carries (TS-1).
 *
 * THE DEFECT: `GET /finance/revenue` has served `measured` / `unmeasured` / `provenance` per snapshot
 * row since wave 2, and the finance page rendered none of it. A row whose only measured columns are
 * the month revenue and the Stripe balance printed MRR/ARR/subscribers as em dashes with no word
 * saying whether that day was measured at all — a partial snapshot and a day nobody measured read
 * identically.
 *
 * THE FIX: a `Provenance` column, fed by the row's own list (never a guessed count): `complete` with
 * the measured count, `partial` with the unmeasured count, `unmeasured` saying no column measured,
 * an unknown word VERBATIM, and `not declared` for a pre-TS-1 server. Unmeasured cells keep their
 * em dash (the null-vs-zero law).
 */
import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import FinancePage from "@/app/finance/page";
import { revenueProvenanceText } from "@/components/finance/summary";
import type { RevenueSnapshotRow } from "@/components/finance/types";

const mockGet = api.get as jest.Mock;

const SEVEN = [
  "mrr_cents",
  "arr_cents",
  "active_subscribers",
  "churned_today",
  "new_today",
  "total_revenue_month_cents",
  "stripe_balance_cents",
];

function row(id: number, overrides: Partial<RevenueSnapshotRow> = {}): RevenueSnapshotRow {
  return {
    id,
    snapshot_date: `2026-10-${String(id).padStart(2, "0")}`,
    mrr_cents: null,
    arr_cents: null,
    active_subscribers: null,
    churned_today: null,
    new_today: null,
    total_revenue_month_cents: null,
    stripe_balance_cents: null,
    created_at: null,
    ...overrides,
  };
}

const ROWS: RevenueSnapshotRow[] = [
  row(1, {
    mrr_cents: 250000,
    arr_cents: 3000000,
    active_subscribers: 3,
    churned_today: 0,
    new_today: 1,
    total_revenue_month_cents: 0,
    stripe_balance_cents: 0,
    measured: SEVEN,
    unmeasured: [],
    provenance: "complete",
  }),
  row(2, {
    total_revenue_month_cents: 0,
    stripe_balance_cents: 0,
    measured: ["total_revenue_month_cents", "stripe_balance_cents"],
    unmeasured: ["mrr_cents", "arr_cents", "active_subscribers", "churned_today", "new_today"],
    provenance: "partial",
  }),
  row(3, { measured: [], unmeasured: SEVEN, provenance: "unmeasured" }),
  row(4, { measured: [], unmeasured: [], provenance: "reconciled" }),
  row(5, {}),
];

function wire(): void {
  mockGet.mockImplementation((path: string) => {
    if (path === "/finance/summary")
      return Promise.resolve({
        mrr_cents: null,
        arr_cents: null,
        active_subscribers: null,
        total_ad_spend_usd: null,
        total_expenses_month_cents: null,
        stripe_balance_cents: null,
        last_snapshot_date: null,
        ai_spend: null,
      });
    if (path === "/finance/planning") return Promise.resolve({});
    if (path === "/economics/context") return Promise.resolve({ generated_at: null });
    if (path.startsWith("/finance/revenue?")) return Promise.resolve(ROWS);
    if (path.startsWith("/finance/expenses?")) return Promise.resolve([]);
    if (path.startsWith("/finance/events?")) return Promise.resolve([]);
    if (path === "/config") return Promise.resolve({ human_hourly_rate: null });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

describe("F-33 — revenueProvenanceText (pure)", () => {
  it("renders each coverage word from the row's own lists", () => {
    expect(revenueProvenanceText(ROWS[0])).toBe("complete · 7/7 measured");
    expect(revenueProvenanceText(ROWS[1])).toBe("partial · 5 unmeasured");
    expect(revenueProvenanceText(ROWS[2])).toBe("unmeasured — no column measured");
    // An unknown word is shown verbatim, named as unknown — never mapped onto a known state.
    expect(revenueProvenanceText(ROWS[3])).toBe(
      "reconciled — not a coverage word this build knows",
    );
    // No marker on the wire is "not declared", not "unmeasured".
    expect(revenueProvenanceText(ROWS[4])).toBe("not declared");
  });

  it("does not invent counts when the marker carries the word but not the lists", () => {
    expect(revenueProvenanceText(row(9, { provenance: "complete" }))).toBe("complete");
    expect(revenueProvenanceText(row(9, { provenance: "partial" }))).toBe("partial");
  });
});

describe("F-33 — the Revenue table renders the column", () => {
  it("shows the Provenance column with the marker's own words and keeps unmeasured cells as dashes", async () => {
    wire();

    render(<FinancePage />);

    expect(
      await screen.findByRole("columnheader", { name: "Provenance" }),
    ).toBeInTheDocument();

    expect(screen.getByText("complete · 7/7 measured")).toBeInTheDocument();
    expect(screen.getByText("partial · 5 unmeasured")).toBeInTheDocument();
    expect(screen.getByText("unmeasured — no column measured")).toBeInTheDocument();
    expect(
      screen.getByText("reconciled — not a coverage word this build knows"),
    ).toBeInTheDocument();
    expect(screen.getByText("not declared")).toBeInTheDocument();

    // The null-vs-zero law survives: a partial row's MRR cell is the em dash (measured zero would
    // print $0.00), and the panel never claims a snapshot's numbers are complete.
    const revenue = screen.getByRole("table", { name: "Revenue" });
    expect(revenue).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.queryByText(/measured \$0\.00/i)).toBeNull();
  });
});
