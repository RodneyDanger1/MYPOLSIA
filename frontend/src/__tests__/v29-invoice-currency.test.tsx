/**
 * V29/L4 — invoice currency display + `moneyText` (RED-FIRST leg).
 *
 * The real bug this closes: `invoices/page.tsx` printed `usdText(row.total)` for EVERY row, so a
 * EUR invoice read `$1,000.00` on screen (plan §2.2). The fix has two halves and both are pinned
 * here on BEHAVIOUR:
 *
 *   1. `moneyText(value, currency)` — USD keeps the EXACT `$1,234.50` shape (`usdText`, so every
 *      existing invoice/finance expectation is untouched); a non-USD row reads `EUR 1,234.56`;
 *      null/non-finite stays the house em dash.
 *   2. a non-USD row asks `GET /economics/convert?amount=1&from=<code>&to=USD` ONCE per currency
 *      and renders a DISPLAY-ONLY `≈ $X` line, with the rate's market date in `title`. A refused
 *      conversion renders `≈ —` — never a 1:1 fallback, never an invented number.
 *
 * The invariants the brief calls out: a USD row makes NO conversion call (no new traffic, no
 * behaviour change), the conversion is per-currency (two EUR rows = one read), and NOTHING is
 * written back — a display conversion must never POST/PATCH an invoice total.
 */
import { render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: {
      get: jest.fn(),
      post: jest.fn(),
      put: jest.fn(),
      patch: jest.fn(),
      delete: jest.fn(),
    },
  };
});

import { moneyText } from "@/components/finance/format";
import { api } from "@/lib/api";
import InvoicesPage from "@/app/invoices/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPatch = api.patch as jest.Mock;
const mockDelete = api.delete as jest.Mock;

const LIST_PATH = "/invoices?limit=100";
const EUR_USD_PATH = "/economics/convert?amount=1&from=EUR&to=USD";
const GBP_USD_PATH = "/economics/convert?amount=1&from=GBP&to=USD";

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
});

/** The exact v2 wire shape `GET /economics/convert` serves (`FxConvertResponse`). */
function conversion(over: Record<string, unknown> = {}) {
  return {
    amount: 1,
    from: "EUR",
    to: "USD",
    rate: 1.1236,
    converted: 1.12,
    date: "2026-10-20",
    source: "https://api.frankfurter.dev/v2/rate/EUR/USD",
    fetched_at: "2026-10-20T15:00:00+00:00",
    cached: false,
    ...over,
  };
}

/** Exact-path reads; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(answers: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = { [LIST_PATH]: [], ...answers };
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

function invoice(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    number: "INV-2026-001",
    bill_to: { name: "Acme Co" },
    issue_date: "2026-10-01",
    due_date: "2026-10-31",
    net_days: 30,
    currency: "USD",
    subtotal: 1234.5,
    tax: 0,
    total: 1234.5,
    status: "draft",
    payment_link: null,
    paid_at: null,
    late_fee_note: null,
    line_items: null,
    ...over,
  };
}

describe("moneyText — one money family, currency-aware", () => {
  it("keeps the EXACT USD shape through usdText", () => {
    expect(moneyText(1234.5, "USD")).toBe("$1,234.50");
    expect(moneyText(0, "USD")).toBe("$0.00");
    expect(moneyText(1234.5, "usd")).toBe("$1,234.50"); // normalised, never a second shape
  });

  it("formats a non-USD amount as the code plus the grouped amount", () => {
    expect(moneyText(1234.56, "EUR")).toBe("EUR 1,234.56");
    expect(moneyText(1234.56, "eur")).toBe("EUR 1,234.56");
    expect(moneyText(0, "GBP")).toBe("GBP 0.00"); // a measured 0 IS a reading
  });

  it("keeps null / non-finite as the house em dash and treats a blank code as USD", () => {
    expect(moneyText(null, "EUR")).toBe("—");
    expect(moneyText(undefined, "EUR")).toBe("—");
    expect(moneyText(Number.NaN, "EUR")).toBe("—");
    expect(moneyText(1234.5, null)).toBe("$1,234.50");
    expect(moneyText(1234.5, "")).toBe("$1,234.50");
  });
});

describe("InvoicesPage — currency display (GET /invoices + GET /economics/convert)", () => {
  it("renders a USD row as $1,234.50 and makes NO conversion call", async () => {
    answerWith({ [LIST_PATH]: [invoice()] });
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-1");
    expect(within(row).getByTestId("invoice-total")).toHaveTextContent("$1,234.50");
    expect(within(row).queryByTestId("invoice-converted")).toBeNull();
    // The ONLY read is the list: a USD row must not create new traffic.
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith(LIST_PATH, expect.anything());
  });

  it("renders a EUR row as EUR 1,234.56 plus a display-only ≈ $ line", async () => {
    answerWith({
      [LIST_PATH]: [invoice({ currency: "EUR", total: 1234.56 })],
      [EUR_USD_PATH]: conversion(),
    });
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-1");
    expect(within(row).getByTestId("invoice-total")).toHaveTextContent("EUR 1,234.56");
    expect(within(row).getByTestId("invoice-total")).not.toHaveTextContent("$1,234.56");

    const converted = await within(row).findByTestId("invoice-converted");
    // 1234.56 x 1.1236 = 1387.1516... -> $1,387.15 (the backend rate, applied for display only)
    expect(converted).toHaveTextContent("≈ $1,387.15");
    expect(converted.getAttribute("title")).toContain("2026-10-20");

    expect(mockGet).toHaveBeenCalledWith(EUR_USD_PATH); // one rate read, no options needed
    // DISPLAY-ONLY: no write ever follows a conversion.
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("reads the rate once per currency, not once per row", async () => {
    answerWith({
      [LIST_PATH]: [
        invoice({ id: 1, currency: "EUR", total: 100 }),
        invoice({ id: 2, number: "INV-2026-002", currency: "EUR", total: 200 }),
        invoice({ id: 3, number: "INV-2026-003", currency: "GBP", total: 50 }),
      ],
      [EUR_USD_PATH]: conversion(),
      [GBP_USD_PATH]: conversion({ from: "GBP", rate: 1.25 }),
    });
    render(<InvoicesPage />);

    await screen.findByTestId("invoice-row-1");
    await waitFor(() =>
      expect(within(screen.getByTestId("invoice-row-3")).getByTestId("invoice-converted"))
        .toHaveTextContent("≈ $62.50"),
    );

    const conversionCalls = mockGet.mock.calls
      .map((call) => String(call[0]))
      .filter((path) => path.startsWith("/economics/convert"));
    expect(conversionCalls.sort()).toEqual([EUR_USD_PATH, GBP_USD_PATH]);
  });

  it("renders ≈ — when the conversion is refused and never invents a number", async () => {
    answerWith({
      [LIST_PATH]: [invoice({ currency: "EUR", total: 1234.56 })],
      [EUR_USD_PATH]: new Error("E_VALIDATION: the rate source does not know the currency pair"),
    });
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-1");
    const converted = await within(row).findByTestId("invoice-converted");
    expect(converted).toHaveTextContent("≈ —");
    expect(converted).not.toHaveTextContent("$1,234.56"); // never a 1:1 fallback
    // The row's own currency amount is still exactly what the server stored.
    expect(within(row).getByTestId("invoice-total")).toHaveTextContent("EUR 1,234.56");
  });

  it("draws no conversion line for an unmeasured non-USD total", async () => {
    answerWith({
      [LIST_PATH]: [invoice({ currency: "EUR", subtotal: null, tax: null, total: null })],
    });
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-1");
    expect(within(row).getByTestId("invoice-total")).toHaveTextContent("—");
    expect(within(row).queryByTestId("invoice-converted")).toBeNull();
    expect(mockGet).toHaveBeenCalledTimes(1); // nothing to convert -> no rate read
  });
});
