/**
 * W3/unit-2 — the /invoices page (wiring audit F2: invoices shipped backend-only in V9).
 *
 * The API module is mocked the way the sibling suites do it (u1/u2/u3/u7/u8b/u9): a factory over
 * `jest.requireActual` so `ApiError` stays REAL, and every surface is pinned on its BEHAVIOUR —
 * what it reads, what it sends, and what it refuses to invent.
 *
 * Wire truth written from the REAL backend files (read 2026-10-15):
 *   GET  /api/v1/invoices?limit=&status=   -> 200 [InvoiceResponse]        (invoices.py:85)
 *   POST /api/v1/invoices                  -> 201 InvoiceResponse          (invoices.py:64)
 *   POST /api/v1/invoices/{id}/send        -> 200 InvoiceResponse          (invoices.py:103)
 *   POST /api/v1/invoices/{id}/void        -> 200 InvoiceResponse          (invoices.py:122)
 * Request bodies: InvoiceCreateRequest / InvoiceSendRequest / InvoiceVoidRequest declared IN
 * inboxes.py (invoices.py:33-61). Response: `app/schemas/invoice.py` InvoiceResponse — money is
 * `float | None` (null is "not measured", NEVER 0) and `paid_at` is `datetime | None`.
 * Send refusals are the service's named PolsiaErrors (E_CONFIG / E_SANDBOX_BLOCKED /
 * E_RATE_LIMITED / E_INTEGRATION; illegal transition = E_VALIDATION) — the page must render
 * `ApiError.display` (code + how_to_fix), never a friendlier lie.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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

import { absoluteStamp } from "@/components/dashboard/format";
import { api, ApiError } from "@/lib/api";
import InvoicesPage from "@/app/invoices/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPatch = api.patch as jest.Mock;
const mockDelete = api.delete as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
});

/** Exact-path reads; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(answers: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/invoices?limit=100": [],
    ...answers,
  };
  mockGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/** One invoice in the frozen InvoiceResponse shape. */
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

describe("InvoicesPage — the list (GET /invoices)", () => {
  it("reads /invoices?limit=100 and states the honest empty list", async () => {
    answerWith();
    render(<InvoicesPage />);

    const empty = await screen.findByTestId("invoices-empty");
    expect(empty).toHaveTextContent(/No invoices yet/);
    expect(mockGet).toHaveBeenCalledWith("/invoices?limit=100");
    expect(screen.queryByTestId("invoices-read-error")).toBeNull();
  });

  it("renders every field the server sent: number, bill-to, dates, total, status, paid stamp", async () => {
    answerWith({
      "/invoices?limit=100": [
        invoice(),
        invoice({
          id: 2,
          number: "INV-2026-002",
          status: "paid",
          total: 0,
          bill_to: null,
          paid_at: "2026-10-05T10:00:00Z",
        }),
      ],
    });
    render(<InvoicesPage />);

    const first = await screen.findByTestId("invoice-row-1");
    expect(within(first).getByTestId("invoice-number")).toHaveTextContent("INV-2026-001");
    expect(within(first).getByTestId("invoice-bill-to")).toHaveTextContent("Acme Co");
    expect(within(first).getByTestId("invoice-issued")).toHaveTextContent("2026-10-01");
    expect(within(first).getByTestId("invoice-due")).toHaveTextContent("2026-10-31");
    expect(within(first).getByTestId("invoice-total")).toHaveTextContent("$250.00");
    expect(within(first).getByTestId("invoice-status")).toHaveTextContent("draft");

    const second = screen.getByTestId("invoice-row-2");
    // A measured 0 IS a reading: $0.00 renders exactly (null != 0 — the other half of the law).
    expect(within(second).getByTestId("invoice-total")).toHaveTextContent("$0.00");
    // A null bill_to is the em dash, not an invented name.
    expect(within(second).getByTestId("invoice-bill-to")).toHaveTextContent("—");
    // paid_at is rendered through the house stamp, from the server's instant.
    expect(within(second).getByTestId("invoice-paid")).toHaveTextContent(
      absoluteStamp("2026-10-05T10:00:00Z"),
    );
  });

  it("renders a null money field as the em dash — never as $0.00", async () => {
    answerWith({
      "/invoices?limit=100": [
        invoice({ id: 3, number: "INV-2026-003", subtotal: null, tax: null, total: null, bill_to: null }),
      ],
    });
    render(<InvoicesPage />);

    const row = await screen.findByTestId("invoice-row-3");
    expect(within(row).getByTestId("invoice-total")).toHaveTextContent("—");
    expect(within(row).getByTestId("invoice-total")).not.toHaveTextContent("$0.00");
    expect(within(row).getByTestId("invoice-paid")).toHaveTextContent("—");
    expect(row).not.toHaveTextContent("$0.00");
  });

  it("shows a loading state before the read settles (no dash grid, no empty claim)", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    render(<InvoicesPage />);

    expect(screen.getByTestId("invoices-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText(/No invoices yet/)).toBeNull();
    expect(screen.queryByTestId("invoices-read-error")).toBeNull();
  });

  it("names a failed read with ApiError.display and draws no rows and no empty claim", async () => {
    answerWith({
      "/invoices?limit=100": new ApiError(500, "boom", "E_HTTP_500", "check the backend"),
    });
    render(<InvoicesPage />);

    const alert = await screen.findByTestId("invoices-read-error");
    expect(alert).toHaveTextContent("Could not load the invoice list: E_HTTP_500: boom — check the backend");
    expect(screen.queryByTestId("invoice-row-1")).toBeNull();
    expect(screen.queryByText(/No invoices yet/)).toBeNull();
  });

  it("names a broken (non-array) body instead of laundering it into an empty list", async () => {
    answerWith({ "/invoices?limit=100": { detail: "not a list" } });
    render(<InvoicesPage />);

    const alert = await screen.findByTestId("invoices-read-error");
    expect(alert).toHaveTextContent("the server returned no invoice list");
    expect(screen.queryByText(/No invoices yet/)).toBeNull();
  });

  it("re-reads with the real status query param when the filter changes", async () => {
    answerWith({
      "/invoices?limit=100": [invoice()],
      "/invoices?limit=100&status=draft": [invoice()],
    });
    render(<InvoicesPage />);
    await screen.findByTestId("invoice-row-1");

    fireEvent.change(screen.getByLabelText("Status filter"), { target: { value: "draft" } });

    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith("/invoices?limit=100&status=draft"),
    );
  });
});

describe("InvoicesPage — create (POST /invoices)", () => {
  it("POSTs the exact body and shows the server's row (created note + prepended row, no invented re-read)", async () => {
    answerWith({ "/invoices?limit=100": [invoice()] });
    mockPost.mockResolvedValue(invoice({ id: 9, number: "INV-2026-009", total: 125, subtotal: 125 }));
    render(<InvoicesPage />);
    await screen.findByTestId("invoice-row-1");

    fireEvent.change(screen.getByLabelText("Bill to name"), { target: { value: "Acme Co" } });
    fireEvent.change(screen.getByLabelText("Bill to email"), { target: { value: "billing@acme.test" } });
    fireEvent.change(screen.getByLabelText("Total"), { target: { value: "125" } });
    fireEvent.click(screen.getByRole("button", { name: "Create invoice" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/invoices", {
        bill_to: { name: "Acme Co", email: "billing@acme.test" },
        net_days: 30,
        total: 125,
      }),
    );

    expect(await screen.findByTestId("invoice-created-note")).toHaveTextContent("INV-2026-009");
    expect(await screen.findByTestId("invoice-row-9")).toBeInTheDocument();
    // The answer of the POST is the row — no follow-up read is invented.
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("renders a create refusal as ApiError.display — no invented invoice", async () => {
    answerWith({ "/invoices?limit=100": [] });
    mockPost.mockRejectedValue(
      new ApiError(400, "net_days must be >= 0, got -5", "E_VALIDATION", "fix the field and retry"),
    );
    render(<InvoicesPage />);
    await screen.findByTestId("invoices-empty");

    fireEvent.change(screen.getByLabelText("Bill to name"), { target: { value: "Acme Co" } });
    fireEvent.click(screen.getByRole("button", { name: "Create invoice" }));

    const error = await screen.findByTestId("invoice-create-error");
    expect(error).toHaveTextContent("E_VALIDATION: net_days must be >= 0, got -5 — fix the field and retry");
    expect(screen.queryByTestId("invoice-created-note")).toBeNull();
  });

  it("an out-of-range net_days rides the body — the server refuses it BY NAME, it is never silently dropped (S2-F3)", async () => {
    answerWith({ "/invoices?limit=100": [] });
    mockPost.mockRejectedValue(
      new ApiError(422, "net_days must be <= 365", "E_VALIDATION", "fix the field and retry"),
    );
    render(<InvoicesPage />);
    await screen.findByTestId("invoices-empty");

    fireEvent.change(screen.getByLabelText("Net days"), { target: { value: "400" } });
    fireEvent.click(screen.getByRole("button", { name: "Create invoice" }));

    // The operator's stated terms are NOT dropped from the body (the old shape omitted the key and
    // the service applied its own default under a success notice): 400 goes to the endpoint whose
    // constraint refuses it by name, and THAT refusal is what renders.
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/invoices", { net_days: 400 }));
    const error = await screen.findByTestId("invoice-create-error");
    expect(error).toHaveTextContent("E_VALIDATION: net_days must be <= 365 — fix the field and retry");
    expect(screen.queryByTestId("invoice-created-note")).toBeNull();
  });
});

describe("InvoicesPage — send / void (POST /invoices/{id}/send|void)", () => {
  it("sends with the real path and replaces the row from the server's answer", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 7, number: "INV-2026-007" })] });
    mockPost.mockResolvedValue(invoice({ id: 7, number: "INV-2026-007", status: "sent" }));
    render(<InvoicesPage />);
    const row = await screen.findByTestId("invoice-row-7");

    fireEvent.click(within(row).getByRole("button", { name: /^Send$/ }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/invoices/7/send", {}));
    await waitFor(() =>
      expect(within(screen.getByTestId("invoice-row-7")).getByTestId("invoice-status")).toHaveTextContent(
        "sent",
      ),
    );
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("renders the send refusal by name — the row stays a draft", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 8, number: "INV-2026-008" })] });
    mockPost.mockRejectedValue(
      new ApiError(
        400,
        "invoice email refused: the compliance footer is empty — sender identity is not configured",
        "E_CONFIG",
        "set sender_legal_name and sender_postal_address in the company settings, then retry",
      ),
    );
    render(<InvoicesPage />);
    const row = await screen.findByTestId("invoice-row-8");

    fireEvent.click(within(row).getByRole("button", { name: /^Send$/ }));

    const error = await screen.findByTestId("invoice-action-error-8");
    expect(error).toHaveTextContent(
      "E_CONFIG: invoice email refused: the compliance footer is empty — sender identity is not configured — set sender_legal_name and sender_postal_address in the company settings, then retry",
    );
    expect(within(screen.getByTestId("invoice-row-8")).getByTestId("invoice-status")).toHaveTextContent(
      "draft",
    );
  });

  it("voids through a confirm step and updates the row from the server's answer", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 9, number: "INV-2026-009" })] });
    mockPost.mockResolvedValue(invoice({ id: 9, number: "INV-2026-009", status: "void" }));
    render(<InvoicesPage />);
    const row = await screen.findByTestId("invoice-row-9");

    fireEvent.click(within(row).getByRole("button", { name: /^Void$/ }));
    // The first click must not write anything — void is terminal.
    expect(mockPost).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("invoice-void-confirm-9"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/invoices/9/void", {}));
    await waitFor(() =>
      expect(within(screen.getByTestId("invoice-row-9")).getByTestId("invoice-status")).toHaveTextContent(
        "void",
      ),
    );
  });

  it("renders a void refusal by name — the row keeps its status", async () => {
    answerWith({ "/invoices?limit=100": [invoice({ id: 10, number: "INV-2026-010", status: "paid" })] });
    mockPost.mockRejectedValue(
      new ApiError(400, "illegal invoice transition paid -> void for invoice INV-2026-010", "E_VALIDATION", "fix the field"),
    );
    render(<InvoicesPage />);
    const row = await screen.findByTestId("invoice-row-10");

    fireEvent.click(within(row).getByRole("button", { name: /^Void$/ }));
    fireEvent.click(screen.getByTestId("invoice-void-confirm-10"));

    const error = await screen.findByTestId("invoice-action-error-10");
    expect(error).toHaveTextContent(
      "E_VALIDATION: illegal invoice transition paid -> void for invoice INV-2026-010 — fix the field",
    );
    expect(within(screen.getByTestId("invoice-row-10")).getByTestId("invoice-status")).toHaveTextContent(
      "paid",
    );
  });
});
