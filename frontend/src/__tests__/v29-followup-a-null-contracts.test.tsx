/**
 * V29 follow-up A — the null-vs-optional wire contract on the V29 frontend surfaces.
 *
 * Two cases, both the class the V29 e2e run found:
 *
 *  1. A stored record that carries an EXPLICIT `null` is legitimate, and a parser that demands a
 *     boolean/defined value rejects the whole payload and paints a failure. The e2e bug: stored
 *     cleanup rows carry `removed_truncated: null` / `skipped_truncated: null`, the retention
 *     panel's validator required a real boolean, and a good record rendered as "the stored cleanup
 *     result is not the documented shape". The validator now accepts absent-or-null; case 1 below
 *     is the REGRESSION PIN for that fix (the fix shipped without a test — `v29-remainders` pins
 *     `true` and absent, never `null`).
 *
 *  2. A `null` field rendered as a CONCRETE claim is the same lie from the other side.
 *     `ExpenseRecordResponse.currency` / `StripeEventResponse.currency` are `str | None`
 *     (`backend/app/schemas/finance.py`), the columns are nullable (`app/models/finance.py`), and
 *     `finance_service._normalize_currency` stores a blank as NULL — `tests/unit/
 *     test_v12_low_finance_fixes.py` pins `blank_event.currency is None`. So "measured amount, no
 *     recorded currency" is a real row; the shared `moneyCentsText` fell through `currencyCode()`'s
 *     INVOICE default (that column is non-null with a USD default) and printed `$49.99` — a unit
 *     nobody recorded, invented at render time.
 *
 * Everything here is behaviour on the rendered DOM / the exported formatter; no mocks of the
 * component's own logic.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() } };
});

import { render, screen, within } from "@testing-library/react";

import FinancePage from "@/app/finance/page";
import { moneyCentsText } from "@/components/finance/format";
import { OpsRetention } from "@/components/ops/OpsRetention";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
});

// ── 1. the retention record's null truncation flags (the e2e regression pin) ──────────────────

const RESULT_BASE = {
  runs_dir: "C:/data/workspace/runs",
  count: 2,
  kept: 21,
  removed: ["run-old-00", "run-old-01"],
  skipped: [{ name: "not a run", reason: "invalid_name" }],
  policy: { keep_latest: 20, max_age_hours: 72 },
  applied: true,
};

describe("OpsRetention — a stored row with explicit null flags is a valid record", () => {
  it("renders the result instead of 'not the documented shape'", async () => {
    // The exact wire shape the V29 e2e hit: the stored row carries null, not `false` and not an
    // absent key (the GET route cannot exclude None — `{"data": null}` must stay a measured
    // absence, so `RunWorkspaceCleanupResult` serialises its optional flags as JSON null).
    mockGet.mockResolvedValue({
      data: {
        activity_id: 12,
        created_at: "2026-10-19T10:00:00Z",
        trigger: "beat",
        result: {
          ...RESULT_BASE,
          removed_truncated: null,
          skipped_truncated: null,
        },
      },
    });

    render(<OpsRetention />);

    const result = await screen.findByTestId("ops-retention-result");
    expect(within(result).getByTestId("ops-retention-count")).toHaveTextContent("2");
    expect(within(result).getByTestId("ops-retention-kept")).toHaveTextContent("21");
    expect(screen.queryByText(/not the documented shape/i)).toBeNull();
    // null is "the server did not say" — it must not become a truncation CLAIM either.
    expect(screen.queryByTestId("ops-retention-truncated")).toBeNull();
    expect(screen.queryByTestId("ops-retention-skipped-truncated")).toBeNull();
  });
});

// ── 2. a measured finance amount whose currency is null ─────────────────────────────────────

/** The wire shape of `GET /finance/summary` before anything is measured (`FinanceSummary`). */
const EMPTY_SUMMARY = {
  mrr_cents: null,
  arr_cents: null,
  active_subscribers: null,
  total_ad_spend_usd: null,
  total_expenses_month_cents: null,
  stripe_balance_cents: null,
  last_snapshot_date: null,
  ai_spend: null,
};

function wire(expenses: unknown[]): void {
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith("/finance/summary")) return Promise.resolve(EMPTY_SUMMARY);
    if (path.startsWith("/finance/revenue")) return Promise.resolve([]);
    if (path.startsWith("/finance/expenses")) return Promise.resolve(expenses);
    if (path.startsWith("/finance/events")) return Promise.resolve([]);
    return Promise.reject(new Error(`unexpected read: ${path}`));
  });
}

describe("moneyCentsText — a null currency is 'not recorded', never USD", () => {
  it("keeps the measured amount and states the missing unit instead of inventing '$'", () => {
    const nullCurrency = moneyCentsText(4999, null);
    expect(nullCurrency).toContain("49.99");
    expect(nullCurrency).not.toContain("$");
    expect(nullCurrency).toContain("currency not recorded");

    // An absent key is the same absence as an explicit null on this wire.
    expect(moneyCentsText(4999, undefined)).toBe(nullCurrency);
  });

  it("keeps the documented USD/non-USD shapes exactly as they were", () => {
    expect(moneyCentsText(123455, "USD")).toBe("$1,234.55");
    expect(moneyCentsText(123455, "EUR")).toBe("EUR 1,234.55");
    expect(moneyCentsText(null, null)).toBe("—");
  });
});

describe("the expenses table — the user-visible half of the same defect", () => {
  it("does not print a dollar sign on a row whose currency was never recorded", async () => {
    wire([
      {
        id: 1,
        category: "software",
        vendor: "Provider Co",
        amount_cents: 4999,
        currency: null, // `_normalize_currency("") -> None`; a real stored row
        description: null,
        date: "2026-10-12",
        external_ref: null,
        created_at: null,
      },
    ]);

    render(<FinancePage />);

    const panel = await screen.findByTestId("finance-list-expenses");
    const table = await within(panel).findByTestId("expenses-table");
    const amountCell = within(table)
      .getAllByRole("cell")
      .find((cell) => (cell.textContent ?? "").includes("49.99"));

    expect(amountCell).toBeDefined();
    expect(amountCell?.textContent).not.toContain("$");
    expect(amountCell?.textContent).toContain("currency not recorded");
  });
});
