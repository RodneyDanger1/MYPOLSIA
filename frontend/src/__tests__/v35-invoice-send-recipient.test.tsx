/**
 * WAVE 7 — the email SEND surfaces on screen: the recipient fact and the named refusal.
 *
 * The real outbound-email send in this product is `POST /invoices/{id}/send`
 * (`invoice_service.send_invoice` → `deliver_invoice_email`: compliance footer → recipient →
 * policy firewall → provider). Two honesty gaps this suite closes/pins:
 *
 *  1. **The recipient fact was invisible.** `invoice_service._recipient` refuses a row whose
 *     `bill_to` carries no usable email (`E_VALIDATION: no bill_to.email`), and the live draft
 *     INV-2026-007 has `bill_to: null` — yet the page said nothing before the click and the
 *     refusal the owner saw first was about the SENDER identity (the footer gate runs first). The
 *     row now states the fact it already carries, and the Send control's title names the address a
 *     send would use.
 *  2. **The boundary is deliberate.** This page mirrors only the STATUS matrix and leaves the
 *     backend the arbiter of every other refusal (pinned by `v19-fe-contract.test.tsx` §5). So a
 *     no-recipient row's Send stays ENABLED and the server's own refusal is what renders — this
 *     suite pins BOTH halves so neither side can drift silently.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import InvoicesPage from "@/app/invoices/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

/** One invoice in the frozen `InvoiceResponse` shape (`GET /invoices`). */
function invoice(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    number: "INV-2026-001",
    bill_to: { name: "Acme Co", email: "billing@acme.test" },
    issue_date: "2026-10-01",
    due_date: "2026-10-31",
    net_days: 30,
    currency: "USD",
    subtotal: 250,
    tax: 0,
    total: 250,
    status: "draft",
    payment_link: null,
    paid_at: null,
    late_fee_note: null,
    line_items: null,
    ...over,
  };
}

function serve(rows: unknown[]) {
  mockGet.mockImplementation((path: string) => {
    if (path.startsWith("/invoices?limit=")) return Promise.resolve(rows);
    return Promise.reject(new Error(`unexpected read: ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("W7 — the invoice send states who it emails", () => {
  it("a draft WITH an address: no no-recipient note, and the control names the address", async () => {
    serve([invoice({ id: 1, status: "draft" })]);
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-1");
    expect(within(row).queryByTestId("invoice-no-recipient-1")).toBeNull();
    const send = within(row).getByRole("button", { name: /^Send$/ });
    expect(send).toBeEnabled();
    expect(send).toHaveAttribute(
      "title",
      "Emails invoice INV-2026-001 to billing@acme.test (POST /invoices/1/send)",
    );
  });

  it("a draft WITHOUT an address (the live INV-2026-007 shape): the fact is stated before the click", async () => {
    serve([invoice({ id: 7, number: "INV-2026-007", status: "draft", bill_to: null })]);
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-7");
    const note = within(row).getByTestId("invoice-no-recipient-7");
    // W7/F-44: the backend refuses the sender identity FIRST, then the recipient; the copy states both
    // gates in that order (OLD text named only E_VALIDATION).
    expect(note.textContent).toMatch(/Sending this invoice is refused at the sender-identity gate first/);
    expect(note.textContent).toMatch(/E_VALIDATION: no bill_to\.email/);
    // The control stays live ON PURPOSE (the backend is the arbiter, v19-fe-contract §5) and its
    // title carries the same fact.
    const send = within(row).getByRole("button", { name: /^Send$/ });
    expect(send).toBeEnabled();
    expect(send.getAttribute("title")).toMatch(/carries no bill_to\.email/);
  });

  it("the server's own refusal still renders by name when the click is made anyway", async () => {
    serve([invoice({ id: 7, number: "INV-2026-007", status: "draft", bill_to: null })]);
    mockPost.mockRejectedValue(
      new ApiError(
        422,
        "invoice INV-2026-007 has no bill_to.email; the notice cannot be addressed",
        "E_VALIDATION",
        "set bill_to.email on the invoice (POST /invoices accepts it at create time) before sending",
      ),
    );
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-7");
    fireEvent.click(within(row).getByRole("button", { name: /^Send$/ }));

    const error = await screen.findByTestId("invoice-action-error-7");
    expect(error.textContent).toContain("E_VALIDATION");
    expect(error.textContent).toContain("no bill_to.email");
    expect(error.textContent).toContain("before sending");
    // The row keeps the SERVER's state: still a draft, never an optimistic flip.
    expect(within(row).getByTestId("invoice-status")).toHaveTextContent("draft");
    expect(mockPost).toHaveBeenCalledWith("/invoices/7/send", {});
  });

  it("a terminal row with no address shows the terminal reason, not a second, contradictory one", async () => {
    serve([invoice({ id: 5, status: "void", bill_to: null })]);
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-5");
    expect(within(row).getByTestId("invoice-terminal-reason-5")).toHaveTextContent(/terminal/);
    // The recipient note is about a send that could happen; a void row has no send at all.
    expect(within(row).queryByTestId("invoice-no-recipient-5")).toBeNull();
  });

  it("a sent row with an address keeps the draft-only reason (the status matrix is unchanged)", async () => {
    serve([invoice({ id: 2, status: "sent" })]);
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-2");
    expect(within(row).getByRole("button", { name: /^Send$/ })).toBeDisabled();
    expect(within(row).getByTestId("invoice-send-reason-2")).toHaveTextContent(
      /only a draft invoice can be sent/i,
    );
    expect(within(row).queryByTestId("invoice-no-recipient-2")).toBeNull();
    await waitFor(() => expect(mockPost).not.toHaveBeenCalled());
  });
});
