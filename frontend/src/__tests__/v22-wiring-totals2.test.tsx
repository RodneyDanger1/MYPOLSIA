import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import InvoicesPage from "@/app/invoices/page";
import OutreachPage from "@/app/outreach/page";
import SupportPage from "@/app/support/page";

/**
 * V22/L3 — W1 GAP-1 (remaining `X-Polsia-Total` reads): invoices, outreach, support.
 *
 * Each route declares the FULL filtered count in `X-Polsia-Total`; each page read the body only.
 * `/invoices` therefore truncated at 100 silently, and `/outreach` + `/support` disclosed the cap
 * without ever naming the true count. All three now capture the header from the SAME response and
 * print "{shown} of {total}"; a response without a readable header keeps the old page-length copy.
 */

type ReadInit = { onHeaders?: (headers: Headers) => void };

const mockApiGet = api.get as jest.MockedFunction<
  (path: string, init?: ReadInit) => Promise<unknown>
>;

function headers(total: string | null): Headers {
  const h = new Headers();
  if (total !== null) h.set("X-Polsia-Total", total);
  return h;
}

function invoice(id: number): Record<string, unknown> {
  return {
    id,
    number: `INV-2026-${id}`,
    bill_to: { name: "Acme" },
    issue_date: "2026-10-01",
    due_date: "2026-10-31",
    net_days: 30,
    currency: "USD",
    subtotal: 10,
    tax: 0,
    total: 10,
    status: "sent",
    payment_link: null,
    paid_at: null,
    late_fee_note: null,
    line_items: null,
  };
}

function prospect(id: number): Record<string, unknown> {
  return {
    id,
    email: `p${id}@example.com`,
    first_name: "Pat",
    last_name: null,
    company: "Acme",
    title: null,
    source: "agent",
    status: "new",
    created_at: "2026-10-01T00:00:00Z",
  };
}

function inboxItem(id: number): Record<string, unknown> {
  return {
    id,
    provider_message_id: `pm-${id}`,
    from_address: "customer@example.com",
    subject: "Help",
    body: "Question",
    status: "pending",
    received_at: "2026-10-01T00:00:00Z",
    task_id: null,
    created_at: "2026-10-01T00:00:00Z",
  };
}

beforeEach(() => {
  mockApiGet.mockReset();
});

describe("Invoices — the 100-row cap is disclosed against the server's total (GAP-1 1a)", () => {
  it("prints 'Showing the first 100 of 137 invoices'", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (path.startsWith("/invoices")) {
        init?.onHeaders?.(headers("137"));
        return Promise.resolve(Array.from({ length: 100 }, (_, index) => invoice(100 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<InvoicesPage />);

    expect(await screen.findByTestId("invoices-truncation")).toHaveTextContent(
      "Showing the first 100 of 137 invoices",
    );
  });

  it("a complete read renders no truncation marker", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (path.startsWith("/invoices")) {
        init?.onHeaders?.(headers("2"));
        return Promise.resolve([invoice(2), invoice(1)]);
      }
      return Promise.reject(new Error(path));
    });

    render(<InvoicesPage />);

    await screen.findByTestId("invoice-row-2");
    expect(screen.queryByTestId("invoices-truncation")).toBeNull();
  });
});

describe("Outreach — the count names the declared total (GAP-1 1c)", () => {
  it("prints '100 of 137 prospects'", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (path.startsWith("/outreach/prospects")) {
        init?.onHeaders?.(headers("137"));
        return Promise.resolve(Array.from({ length: 100 }, (_, index) => prospect(100 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<OutreachPage />);

    expect(await screen.findByTestId("outreach-count")).toHaveTextContent("100 of 137 prospects");
  });

  it("a missing header keeps the page-length count and the cap disclosure", async () => {
    mockApiGet.mockImplementation((path) => {
      if (path.startsWith("/outreach/prospects")) {
        return Promise.resolve(Array.from({ length: 100 }, (_, index) => prospect(100 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<OutreachPage />);

    expect(await screen.findByTestId("outreach-count")).toHaveTextContent(
      "100 prospects · showing the first 100",
    );
  });
});

describe("Support — the count names the declared total (GAP-1 1c)", () => {
  it("prints '50 of 137 messages'", async () => {
    mockApiGet.mockImplementation((path, init) => {
      if (path.startsWith("/support/inbox")) {
        init?.onHeaders?.(headers("137"));
        return Promise.resolve(Array.from({ length: 50 }, (_, index) => inboxItem(50 - index)));
      }
      return Promise.reject(new Error(path));
    });

    render(<SupportPage />);

    expect(await screen.findByTestId("support-count")).toHaveTextContent("50 of 137 messages");
  });
});
