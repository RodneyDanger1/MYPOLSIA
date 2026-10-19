import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { api } from "@/lib/api";
import InvoicesPage from "@/app/invoices/page";

/**
 * V22/L3 — W1 GAP-1: invoice `line_items` had no UI.
 *
 * The wire carries `line_items` on every `InvoiceResponse` (`backend/app/schemas/invoice.py`), the
 * create route accepts them, and the row rendered nothing — an invoice with an itemised breakdown
 * looked identical to one without. The fix is a read-only expander per row (no fetch, no write):
 * the list row already holds the data.
 *
 * Pinned here:
 *  - an invoice with items renders an "Items (N)" expander and the real description/qty/amount;
 *  - null and [] render NO expander (nothing was recorded — no empty shell);
 *  - hostile item shapes never crash or print NaN/undefined.
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

function invoice(id: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    number: `INV-2026-00${id}`,
    bill_to: { name: "Acme" },
    issue_date: "2026-10-01",
    due_date: "2026-10-31",
    net_days: 30,
    currency: "USD",
    subtotal: 1520,
    tax: 0,
    total: 1520,
    status: "draft",
    payment_link: null,
    paid_at: null,
    late_fee_note: null,
    line_items: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("Invoices — line items are rendered from the wire (W1 GAP-1)", () => {
  it("renders an Items (N) expander and the real description, quantity and amount", async () => {
    mockApiGet.mockResolvedValue([
      invoice(1, {
        line_items: [
          { description: "Logo design", quantity: 2, unit_price: 750 },
          { description: "Hosting", amount: 20 },
        ],
      }),
    ]);

    render(<InvoicesPage />);

    const details = await screen.findByTestId("invoice-items-1");
    expect(details).not.toHaveAttribute("open");
    expect(details.querySelector("summary")).toHaveTextContent("Items (2)");

    await userEvent.setup().click(details.querySelector("summary") as HTMLElement);

    expect(details).toHaveTextContent("Logo design");
    expect(details).toHaveTextContent("$750.00");
    expect(details).toHaveTextContent("Hosting");
    expect(details).toHaveTextContent("$20.00");
    // Expansion is local — the list read is the only request.
    expect(mockApiGet).toHaveBeenCalledTimes(1);
  });

  it("null line_items render no expander (not recorded is not an empty list)", async () => {
    mockApiGet.mockResolvedValue([invoice(2, { line_items: null })]);

    render(<InvoicesPage />);

    await screen.findByTestId("invoice-row-2");
    expect(screen.queryByTestId("invoice-items-2")).toBeNull();
  });

  it("an empty array renders no expander either", async () => {
    mockApiGet.mockResolvedValue([invoice(3, { line_items: [] })]);

    render(<InvoicesPage />);

    await screen.findByTestId("invoice-row-3");
    expect(screen.queryByTestId("invoice-items-3")).toBeNull();
  });

  it("hostile item shapes render an honest line, never NaN/undefined", async () => {
    mockApiGet.mockResolvedValue([
      invoice(4, {
        line_items: [{ description: 7, quantity: "two", amount: "lots" }, "not-an-object", null],
      }),
    ]);

    render(<InvoicesPage />);

    const details = await screen.findByTestId("invoice-items-4");
    expect(details.querySelector("summary")).toHaveTextContent("Items (3)");
    await userEvent.setup().click(details.querySelector("summary") as HTMLElement);

    const text = details.textContent ?? "";
    expect(text).not.toContain("NaN");
    expect(text).not.toContain("undefined");
    expect(text).toContain("no description, quantity or amount recorded");
  });
});
