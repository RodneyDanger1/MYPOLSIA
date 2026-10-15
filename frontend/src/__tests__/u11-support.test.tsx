import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V11 / unit 2 — the support inbox mirror (wiring audit F4). Suite `u11-support`.
 *
 * House mocking pattern (as in `u3-task-truth`): the factory replaces `@/lib/api`'s `api` object
 * with jest.fn()s while keeping the ACTUAL module's classes (`ApiError` must stay the real class
 * so `errorText`'s `instanceof` branch is exercised — refusing the ApiError path would let a
 * broken refusal render pass). The REAL endpoint paths and payloads are asserted per action:
 * `GET /support/inbox` (list, filter, limit) and `POST /support/inbox` (the exact create body).
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import SupportPage from "@/app/support/page";
import type { SupportInboxItem } from "@/components/support/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

/** A resolve/reject handle — the only way to observe a TRUE loading state deterministically. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const item = (overrides: Partial<SupportInboxItem> = {}): SupportInboxItem => ({
  id: 1,
  provider_message_id: "pmid-1",
  from_address: "buyer@example.com",
  subject: "Where is my order?",
  body: "Checking in on order 123.",
  status: "pending",
  received_at: null,
  task_id: null,
  created_at: null,
  ...overrides,
});

async function fillRequiredFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/Provider message id/), "pmid-9");
  await user.type(screen.getByLabelText(/From address/), "buyer@example.com");
  await user.type(screen.getByLabelText(/^Subject/), "Cannot log in");
  await user.type(screen.getByLabelText(/^Body/), "Password reset does nothing.");
}

// ---------------------------------------------------------------------------------------------
// The read — GET /support/inbox
// ---------------------------------------------------------------------------------------------

describe("support inbox read", () => {
  it("shows a real loading state, reads GET /support/inbox?limit=50, then renders the rows", async () => {
    const d = deferred<SupportInboxItem[]>();
    mockGet.mockReturnValueOnce(d.promise);

    render(<SupportPage />);

    expect(screen.getByTestId("support-loading")).toBeInTheDocument();
    expect(screen.getByTestId("support-loading")).toHaveAttribute("aria-busy", "true");
    expect(mockGet).toHaveBeenCalledWith("/support/inbox?limit=50");

    d.resolve([item({ id: 7, from_address: "ana@shop.example", subject: "Refund question" })]);

    expect(await screen.findByText("Refund question")).toBeInTheDocument();
    expect(screen.getByTestId("support-count")).toHaveTextContent("1 message");
    expect(screen.queryByTestId("support-loading")).toBeNull();
  });

  it("an empty inbox after a successful read prints the empty copy only — never an error", async () => {
    mockGet.mockResolvedValue([]);

    render(<SupportPage />);

    expect(await screen.findByText(/No messages in the inbox/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByTestId("support-count")).toHaveTextContent("0 messages");
  });

  it("a failed read renders the named refusal (ApiError.display) and NOT empty copy", async () => {
    mockGet.mockRejectedValueOnce(
      new ApiError(503, "the database refused the read", "E_DB", "check that PostgreSQL is running"),
    );

    render(<SupportPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_DB");
    expect(alert).toHaveTextContent("the database refused the read");
    expect(alert).toHaveTextContent("check that PostgreSQL is running");
    // The one forbidden stand-in: a failed read rendered as "no data".
    expect(screen.queryByText(/No messages in the inbox/)).toBeNull();
  });

  it("the status filter sends the REAL values the backend accepts; unknown values never ride the wire", async () => {
    mockGet.mockResolvedValue([]);
    const user = userEvent.setup();

    render(<SupportPage />);
    await screen.findByText(/No messages in the inbox/);

    const filter = screen.getByLabelText("Status");
    expect(within(filter).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual([
      "",
      "pending",
      "dispatched",
    ]);

    await user.selectOptions(filter, "dispatched");
    await waitFor(() =>
      expect(mockGet).toHaveBeenLastCalledWith("/support/inbox?status=dispatched&limit=50"),
    );

    await user.selectOptions(filter, "");
    await waitFor(() => expect(mockGet).toHaveBeenLastCalledWith("/support/inbox?limit=50"));
  });

  it("renders the schema's nullables honestly (null received_at / created_at / task_id) and links a dispatched item's task", async () => {
    mockGet.mockResolvedValue([
      item({ id: 3, subject: "No timestamps", received_at: null, created_at: null, task_id: null }),
      item({ id: 5, subject: "Attached one", status: "dispatched", task_id: 42, received_at: "2026-10-14T10:00:00Z" }),
    ]);

    render(<SupportPage />);

    const bare = await screen.findByTestId("support-item-3");
    expect(bare).toHaveTextContent("date unknown");
    expect(bare).toHaveTextContent("arrival not stated");
    expect(bare).toHaveTextContent("not attached to a task yet");
    expect(within(bare).queryByRole("link")).toBeNull();
    expect(bare.textContent ?? "").not.toMatch(/Invalid Date/);

    const attached = screen.getByTestId("support-item-5");
    const link = within(attached).getByRole("link", { name: "Open task #42" });
    expect(link).toHaveAttribute("href", "/tasks/42");
    // The support vocabulary keeps its own chip (not the tasks map's fallback).
    expect(within(attached).getByText("dispatched")).toHaveClass("text-blue-800");
  });
});

// ---------------------------------------------------------------------------------------------
// The write — POST /support/inbox
// ---------------------------------------------------------------------------------------------

describe("support inbox composer", () => {
  it("POSTs the exact create body (received_at omitted when blank), reports the stored row and re-reads the list", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValueOnce(item({ id: 12, status: "pending" }));
    const user = userEvent.setup();

    render(<SupportPage />);
    await screen.findByText(/No messages in the inbox/);

    await fillRequiredFields(user);
    await user.click(screen.getByRole("button", { name: /^file message$/i }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0]).toEqual([
      "/support/inbox",
      {
        provider_message_id: "pmid-9",
        from_address: "buyer@example.com",
        subject: "Cannot log in",
        body: "Password reset does nothing.",
      },
    ]);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Stored as inbox item #12");
    // The at-least-once truth: the copy never claims a fresh creation.
    expect(notice).toHaveTextContent(/returns this stored row unchanged/);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it("a stated arrival is converted to its instant with an offset before the wire (the service refuses tz-less by name)", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValueOnce(item({ id: 13 }));
    const user = userEvent.setup();

    render(<SupportPage />);
    await screen.findByText(/No messages in the inbox/);
    await fillRequiredFields(user);
    fireEvent.change(screen.getByLabelText(/Arrived at/), { target: { value: "2026-10-15T09:30" } });

    await user.click(screen.getByRole("button", { name: /^file message$/i }));

    const [, body] = mockPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(body.received_at).toBe(new Date("2026-10-15T09:30").toISOString());
    expect(String(body.received_at)).toMatch(/Z$/);
  });

  it("renders a refusal by name (code + how_to_fix), writes nothing visible, and re-arms the submit", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockRejectedValueOnce(
      new ApiError(
        422,
        "support inbox item requires a non-empty provider_message_id",
        "E_VALIDATION",
        "provide every required field; nothing was written",
      ),
    );
    const user = userEvent.setup();

    render(<SupportPage />);
    await screen.findByText(/No messages in the inbox/);
    await fillRequiredFields(user);
    await user.click(screen.getByRole("button", { name: /^file message$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("non-empty provider_message_id");
    expect(alert).toHaveTextContent("nothing was written");

    // A refusal created nothing — the same submit is armed again (and no refetch was issued).
    expect(screen.getByRole("button", { name: /^file message$/i })).toBeEnabled();
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("keeps the submit disabled until the four required fields are filled (no dead button)", async () => {
    mockGet.mockResolvedValue([]);
    const user = userEvent.setup();

    render(<SupportPage />);
    await screen.findByText(/No messages in the inbox/);

    const submit = screen.getByRole("button", { name: /^file message$/i });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/Provider message id/), "pmid-3");
    await user.type(screen.getByLabelText(/From address/), "buyer@example.com");
    await user.type(screen.getByLabelText(/^Subject/), "Cannot log in");
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/^Body/), "Password reset does nothing.");
    expect(screen.getByRole("button", { name: /^file message$/i })).toBeEnabled();
  });
});
